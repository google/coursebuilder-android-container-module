# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Android code execution worker.

A deployment will have n >= 1 worker machines. This worker runs on each of them,
along with server.py. server.py is a webserver; this script handles all Android
operations (build, execution, etc.). It also handles setup and configuration of
the worker machine's environment.

Currently n > 1 workers is not supported.

This is a proof-of-concept implementation and it has many shortcomings:

1. Full support for multiple running emulators is not done yet, and attempting
   to use different emulators will cause hangs. For now, the fix is to use the
   same emulator settings for each entry in runtimes/config.json, which limits
   max concurrent requests to 1, and we don't do any locking so requests running
   at the same time will race. Long term fix is to delegate the emulator name
   and port to all commands, including gradle, then to make the server
   multithreaded and to dispatch concurrent requests to different emulators.
2. Starting emulators happens serially, which is very slow, and we should
   parallelize this.
3. Only 64-bit Linux is currently supported, and only running the 32-bit
   Android toolchain. This is weird; the reason is that the 64-bit toolchain
   requires x86 emulation, which in turn requires KVM support.
4. We run Android code in a VM, so the host machine must support nested VMs or
   performance will be abysmal. The fix for this, and for the 64-bit issue, is
   to run under KVM.
5. Output in the success case is a screenshot. Ideally, it would be an
   interactive emulator session.
6. We do a full compile, apk load, and test run on each test invocation. This
   takes ~30s when running natively and ~45s under emulation. This could be
   improved substantially by being more incremental, and the emulation penalty
   could be decreased with KVM.
7. The test patch implementation assumes only one file is being edited. It could
   be trivially extended to support n >= 0 patches.
8. In headless mode we still rely on the shell having a DISPLAY var set and we
   die if it is missing. We should remove this dependency and only require
   DISPLAY when headed.

Steps for running a worker on AWS:

1. Launch Ubuntu Server 14.04 LTS 64-bit (ami-3d50120d) via
   http://console.aws.amazon.com. Set type to something with 40+ GB of storage.
   You may wish to use other storage options (e.g. for durability between
   instance restarts) in which case you will need to mount them to /mnt or use a
   different mount point in step 5, below.
2. Create a security group permitting custom TCP traffic on port 8080 and source
   Anywhere. Apply it to your instance.
3. ssh -X -i <key.pem> ubuntu@<public_dns> where
   * key.pem is the key file you downloaded from AWS
   * public_dns is the Public DNS of the instance in the EC2 console.
   Be sure to use -X; this sets the shell's DISPLAY var correctly.
4. sudo dpkg --add-architecture i386 && \
       sudo apt-get update && \
       sudo apt-get install \
           git \
           libgcc1:i386 \
           lib32ncurses5 \
           lib32stdc++6 \
           lib32z1 \
           openjdk-7-jdk \
           openjdk-7-jre \
           unzip
5. sudo mkdir -p /usr/local/cacm && \
       sudo chown ubuntu /usr/local/cacm && \
       cd /usr/local/cacm
6. git clone https://github.com/google/coursebuilder-android-container-module \
       && cd coursebuilder-android-container-module
7. python android/worker.py
8. sh android/get_aws_public_hostname.sh
8. python android/server.py --host $(cat android/.aws_public_hostname)

Your worker can now be reached by a FE for REST operations. If you want to put
it behind a balancer like ELB, do an HTTP health check against /health and
expect a 200 if the instance can accept requests to start running new jobs.
Determining the number of workers you need is straightforward: each worker can
handle many concurrent requests for past results, but only one request at a time
for executing a new job.
"""

import argparse
import base64
import datetime
import json
import logging
import md5
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time

ROOT_PATH = os.path.abspath(os.path.dirname(__file__))

_ACCEPT_LICENSE_NEEDLE = 'Do you accept the license'
_ANDROID_HOME = 'ANDROID_HOME'
_ANDROID_SDK_HOME = 'ANDROID_SDK_HOME'
_BOOT_ANIMATION_STOPPED = 'stopped\r'
_BOOT_ANIMATION_PROPERTY = 'init.svc.bootanim'
_CLEAN_ALL = 'all'
_CLEAN_EMULATORS = 'emulators'
_CLEAN_LOCAL = 'local'
_CLEAN_PYC = 'pyc'
_CLEAN_RESOURCES = 'resources'
_CLEAN_RESULTS = 'results'
_CLEAN_RUNTIMES = 'runtimes'
_CLEAN_CHOICES = [
    _CLEAN_ALL,
    _CLEAN_EMULATORS,
    _CLEAN_LOCAL,  # All but resources.
    _CLEAN_PYC,
    _CLEAN_RESOURCES,
    _CLEAN_RESULTS,
    _CLEAN_RUNTIMES,
]
_DISPLAY = 'DISPLAY'
_EMULATOR = 'emulator'
_GRADLEW_INSTALL_SUCCESS_NEEDLE = 'BUILD SUCCESSFUL'
LOG_DEBUG = 'DEBUG'
LOG_ERROR = 'ERROR'
LOG_INFO = 'INFO'
LOG_WARNING = 'WARNING'
LOG_LEVEL_CHOICES = [
    LOG_DEBUG,
    LOG_ERROR,
    LOG_INFO,
    LOG_WARNING,
]
_LOG = logging.getLogger('android.worker')
_TEST_FAILURE_NEEDLE = 'FAILURES!!!\r'

_PROJECTS_PATH = os.path.join(ROOT_PATH, 'projects')
_PROJECTS_CONFIG = os.path.join(_PROJECTS_PATH, 'config.json')
_RESOURCES_PATH = os.path.join(ROOT_PATH, 'resources')
_RESOURCES_TMP_PATH = os.path.join(_RESOURCES_PATH, 'tmp')
_RESULT_IMAGE_NAME = 'result.jpg'
_RESULT_JSON_NAME = 'result.json'
_RESULTS_PATH = os.path.join(ROOT_PATH, 'results')
_RESULTS_TTL_SEC = 60 * 30
_RUNTIMES_PATH = os.path.join(ROOT_PATH, 'runtimes')
_RUNTIMES_CONFIG = os.path.join(_RUNTIMES_PATH, 'config.json')

_PARSER = argparse.ArgumentParser()
_PARSER.add_argument(
    '--clean', type=str, choices=_CLEAN_CHOICES,
    help='Remove entities created by worker.py')
_PARSER.add_argument(
    '--log_level', type=str, choices=LOG_LEVEL_CHOICES, default=LOG_INFO,
    help='Display log messages at or above this level')
_PARSER.add_argument(
    '--show_emulator', action='store_true',
    help='Pass to display the emulator rather than run it headless')
_PARSER.add_argument(
    '--stop', action='store_true', help='Stop running emulators')
_PARSER.add_argument(
    '--test', type=str, help='Name of the project to run the tests for')


def configure_logger(log_level, log_file=None):
    logging.basicConfig(filename=log_file, level=log_level)


def main(args):
    configure_logger(args.log_level)
    config = Config.load()

    if args.clean:
        _clean(args.clean, config.projects, config.runtimes)
    elif args.stop:
        _stop(config.runtimes)
    elif args.test:
        _test(
            args.test, config.projects.get(args.test),
            config.runtimes.get(args.test), strict=True)
    else:
        _ensure_resources_dirs_exist()
        _ensure_sdk_installed()
        _ensure_runtimes_exist(config.runtimes)
        _ensure_projects_exist(config.projects)
        _build_all(config.projects)  # Warm the build.
        _ensure_emulators_running_and_ready(
            config.runtimes, headless=not args.show_emulator)
        _install_packages(config.projects)


def fork_test(config, project_name, ticket, patches=None):
    # Runs a test in a fork; returns PID if test starts else None.
    child = multiprocessing.Process(
        target=run_test, args=(config, project_name, ticket),
        kwargs={'patches': patches})
    child.daemon = True
    child.start()
    return child.pid


def run_test(config, project_name, ticket, patches=None):
    patches = patches if patches else []
    test_env = _TestEnvironment(ticket)
    test_env.set_up()  # All exit points from this fn must call tear_down().
    test_run = TestRun()

    if not patches:
        return _run_test_failure(
            test_env, test_run, ticket, 'Must specify test patches',
            TestRun.CONTENTS_MALFORMED)

    src_project = config.get_project(project_name)
    if not src_project:
        return _run_test_failure(
            test_env, test_run, ticket,
            'Unable to find project named ' + project_name,
            TestRun.PROJECT_MISCONFIGURED)

    runtime = config.get_runtime(project_name)
    if not runtime:
        return _run_test_failure(
            test_env, test_run, ticket,
            'Unable to find runtime for project named ' + project_name,
            TestRun.RUNTIME_MISCONFIGURED)

    try:
        Lock.get(ticket)
        test_env.set_up_projects(patches, src_project)
        _LOG.info('Begin test run of project ' + test_env.test_project.name)
        test_run = TestRun()
        test_run.set_status(TestRun.TESTS_RUNNING)
        test_env.save(test_run)
        test_run = _test(
            test_env.test_project.name, test_env.test_project, runtime,
            strict=False)
        _LOG.info('End test run of project ' + test_env.test_project.name)
        test_env.save(test_run)
        return ticket
    except LockError:
        return _run_test_failure(
            test_env, test_run, ticket, 'Worker busy', TestRun.UNAVAILABLE)
    finally:
        test_env.tear_down()
        # Since we unlock after tear_down, which restores the logger, result dir
        # logs will not contain an entry for the lock release. However, the main
        # server log will.
        Lock.release()


def _run_test_failure(test_env, test_run, ticket, payload, status):
    test_run.set_payload(payload)
    test_run.set_status(status)
    test_env.save(test_run)
    test_env.tear_down()
    return ticket


class Config(object):

    def __init__(self, projects, runtimes):
        self.projects = projects
        self.runtimes = runtimes

    def get_project(self, name):
        return self.projects.get(name)

    def get_runtime(self, project_name):
        return self.runtimes.get(project_name)

    @classmethod
    def load(cls):
        projects = _read_json(_PROJECTS_CONFIG)
        runtimes = _read_json(_RUNTIMES_CONFIG)
        return cls(
            {k: _Project.from_config(k, v) for k, v in projects.iteritems()},
            {k: _Runtime.from_config(k, v) for k, v in runtimes.iteritems()})


class Error(Exception):
    """Base error class."""


class LockError(Error):
    """Raised when a lock operation fails."""


class Lock(object):
    """Persistent lock to prevent concurrent requests on one worker."""

    _PATH = os.path.join(ROOT_PATH, '.lock')

    def __init__(self):
        super(Lock, self).__init__()
        assert False, 'Instantiation not supported'

    @classmethod
    def active(cls):
        return os.path.exists(cls._PATH)

    @classmethod
    def get(cls, ticket):
        if cls.active():
            raise LockError('Lock already active')

        contents = str(ticket)

        with open(cls._PATH, 'w') as f:
            f.write(contents)

        _LOG.info('Acquired execution lock with ticket ' + contents)

    @classmethod
    def release(cls):
        if not cls.active():
            raise LockError('Lock not active')

        contents = str(cls.value())
        os.remove(cls._PATH)
        _LOG.info('Released execution lock with ticket ' + contents)

    @classmethod
    def value(cls):
        if not cls.active():
            return None

        with open(cls._PATH) as f:
            return f.read().strip()


class Patch(object):

    def __init__(self, filename, contents):
        self.contents = contents
        self.filename = filename


class TestRun(object):

    BUILD_FAILED = 'build_failed'
    BUILD_SUCCEEDED = 'build_succeeded'
    CONTENTS_MALFORMED = 'contents_malformed'
    NOT_FOUND = 'not_found'
    PROJECT_MISCONFIGURED = 'project_misconfigured'
    RUNTIME_MISCONFIGURED = 'runtime_misconfigured'
    RUNTIME_NOT_RUNNING = 'runtime_not_running'
    TESTS_FAILED = 'tests_failed'
    TESTS_RUNNING = 'tests_running'
    TESTS_SUCCEEDED = 'tests_succeeded'
    UNAVAILABLE = 'unavailable'
    STATUSES = frozenset((
        BUILD_FAILED,
        BUILD_SUCCEEDED,
        CONTENTS_MALFORMED,
        NOT_FOUND,
        PROJECT_MISCONFIGURED,
        RUNTIME_MISCONFIGURED,
        RUNTIME_NOT_RUNNING,
        TESTS_FAILED,
        TESTS_RUNNING,
        TESTS_SUCCEEDED,
        UNAVAILABLE,
    ))

    def __init__(self):
        self._payload = None
        self._status = None

    def get_payload(self):
        return self._payload

    def get_status(self):
        return self._status

    def set_payload(self, value):
        self._payload = value

    def set_status(self, value):
        if value not in self.STATUSES:
            raise ValueError(
                'Value %s invalid; choices are %s' % (
                value, ', '.join(sorted(self.STATUSES))))

        self._status = value

    def to_dict(self):
        return {
            'payload': self.get_payload(),
            'status': self.get_status(),
        }


def _build_all(projects):
    for project in projects.values():
        project.build()


def _clean(clean, projects, runtimes):
    # Emulators depend on projects and runtimes; do them first.
    if clean in (_CLEAN_ALL, _CLEAN_EMULATORS, _CLEAN_LOCAL):
        _clean_emulators(projects, runtimes)

    if clean in (_CLEAN_ALL, _CLEAN_LOCAL, _CLEAN_RESULTS):
        _clean_results()

    if clean in (_CLEAN_ALL, _CLEAN_LOCAL, _CLEAN_RUNTIMES):
        _clean_runtimes(runtimes)

    # We can clean most accurately if we still have the SDK, so save cleaning it
    # up for the end.
    if clean in (_CLEAN_ALL, _CLEAN_RESOURCES):
        _clean_resources()

    # Finally, .pyc files because they could be created by other cleanup code.
    if clean in (_CLEAN_ALL, _CLEAN_LOCAL, _CLEAN_PYC):
        _clean_pyc()


def _clean_emulators(projects, strict=False):
    for project in projects.values():
        project.uninstall(strict=strict)


def _clean_pyc():
    count = 0
    for root, _, files in os.walk(ROOT_PATH):
        for path in files:
            if os.path.splitext(path)[1] == '.pyc':
                os.remove(os.path.join(root, path))
                count += 1

    _LOG.info('Removed %s .pyc file%s', count, 's' if count != 1 else '')


def _clean_results():
    if os.path.exists(_RESULTS_PATH):
        shutil.rmtree(_RESULTS_PATH)
        _LOG.info('Removed results directory %s', _RESULTS_PATH)


def _clean_resources():
    if os.path.exists(_RESOURCES_PATH):
        shutil.rmtree(_RESOURCES_PATH)
        _LOG.info('Removed resources directory %s', _RESOURCES_PATH)


def _clean_runtimes(runtimes):
    for runtime in runtimes.values():
        runtime.clean()


def _die(message):
    _LOG.critical(message)
    sys.exit(1)


def _ensure_emulators_running_and_ready(runtimes, headless=True):
    # TODO(johncox): serial and slow; parallelize if we have many runtimes.
    for runtime in runtimes.values():
        if runtime.ready():
            _LOG.info(
                    'Emulator for runtime %s already ready on port %s; reusing',
                    runtime.project_name, runtime.port)
        else:
            runtime.start(headless=headless)
            _LOG.info(
                'Emulator for runtime %s not ready; waiting',
                runtime.project_name)
            runtime.block_until_ready()
            _LOG.info('Runtime %s emulator ready', runtime.project_name)


def _ensure_projects_exist(projects):
    for project in projects.values():
        if not project.exists():
            _die(
                'Project %s does not exist at %s; aborting', project.name,
                project_path)


def _ensure_runtimes_exist(runtimes):
    for runtime in runtimes.values():
        if runtime.exists():
            _LOG.info(
                'Runtime %s exists at %s; skipping', runtime.project_name,
                runtime.path)
        else:
            _LOG.info(
                'Runtime %s missing or in inconsistent state; re-creating',
                runtime.project_name)
            runtime.clean()
            runtime.create()


def _ensure_resources_dirs_exist():
    if not os.path.exists(_RESOURCES_PATH):
        os.mkdir(_RESOURCES_PATH)
        _LOG.info('Created resources directory %s', _RESOURCES_PATH)
    else:
        _LOG.info('Using existing resources directory %s', _RESOURCES_PATH)

    if not os.path.exists(_RESOURCES_TMP_PATH):
        os.mkdir(_RESOURCES_TMP_PATH)
        _LOG.info('Created resources temp directory %s', _RESOURCES_TMP_PATH)
    else:
        _LOG.info(
            'Using existing resources temp directory %s', _RESOURCES_TMP_PATH)


def _ensure_sdk_installed():
    if not _Sdk.is_installed():
        _Sdk.install()
    else:
        _LOG.info('Using existing SDK at %s', _Sdk.PATH)


def _get_fingerprint(value):
    return md5.new(value).hexdigest()


def _get_project_runtime_iter(projects, runtimes):
    """Gets iterator over (project, runtime) pairs ordered by project name."""
    assert len(projects) == len(runtimes)
    projects = sorted(projects.values(), key=lambda p: p.name)
    runtimes = sorted(runtimes.values(), key=lambda r: r.project_name)

    return ((project, runtime) for project, runtime in zip(projects, runtimes))


def _get_strict_handler(strict):
    return _die if strict else _LOG.info


def _install_packages(projects):
    for project in projects.values():
        project.install()


def _read_json(path):
    with open(path) as f:
        try:
            return json.loads(f.read())
        except:  # Treat all errors the same. pylint: disable=bare-except
            _LOG.error(
                'Unable to load json from %s; file broken or json malformed',
                path)


def _run(command_line, cwd=None, env=None, proc_fn=None, strict=True):
    env = env if env is not None else {}

    result = []
    _LOG.debug('Running command: ' + ' '.join(command_line))
    proc = subprocess.Popen(
        command_line, cwd=cwd, env=env, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    if proc_fn:
        proc_fn(proc)

    got_stdout, got_stderr = proc.communicate()

    if got_stdout:
        for line in got_stdout.split('\n'):
            result.append(line)

    if got_stderr:
        for line in got_stderr.split('\n'):
            result.append(line)

    if proc.returncode != 0 and strict:
        _die(
            'Error running command "%s":\n%s' % (
                ' '.join(command_line), '\n'.join(result)))

    return proc.returncode, result


def _stop(runtimes):
    for runtime in runtimes.values():
        runtime.stop()


def _test(name, project, runtime, strict=False):
    """Run a project's tests, either under worker.py or under a web caller."""

    handler = _get_strict_handler(strict)
    test_run = TestRun()

    if not project:
        handler('Unable to find project named %s; aborting' % name)
        test_run.set_status(TestRun.PROJECT_MISCONFIGURED)
        return test_run

    if not runtime:
        handler('Unable to find runtime named %s; aborting' % name)
        test_run.set_status(TestRun.RUNTIME_MISCONFIGURED)
        return test_run

    if not runtime.ready():
        handler('Runtime %s not running; aborting' % name)
        test_run.set_status(TestRun.RUNTIME_NOT_RUNNING)
        return test_run

    build_succeeded, build_result = project.install()

    if not build_succeeded:
        test_run.set_status(TestRun.BUILD_FAILED)
        test_run.set_payload('\n'.join(build_result))
        return test_run

    test_run.set_status(TestRun.BUILD_SUCCEEDED)
    test_succeeded, test_result = project.test()

    if not test_succeeded:
        test_run.set_status(TestRun.TESTS_FAILED)
        test_run.set_payload('\n'.join(test_result))
        return test_run

    test_run.set_status(TestRun.TESTS_SUCCEEDED)
    test_run.set_payload(test_result)
    _LOG.info('Tests succeeded for project %s', name)
    return test_run


class _Project(object):

    def __init__(
            self, name, editor_file, package, path, test_class, test_package):
        self.editor_file = editor_file
        self.name = name
        self.package = package
        self.path = path
        self.test_class = test_class
        self.test_package = test_package

    @classmethod
    def from_config(cls, key, value):
        return cls(
            key, os.path.join(_PROJECTS_PATH, key, value['editorFile']),
            value['package'], os.path.join(_PROJECTS_PATH, key),
            value['testClass'], value['testPackage'])

    def build(self, strict=False):
        handler = _get_strict_handler(strict)
        code, result = _run(
            [self._get_gradlew(), 'build'], cwd=self.path,
            env=_Sdk.get_shell_env(), strict=False)

        if code:
            handler(
                'Build for project %s failed; result: %s' % (
                    self.name, '\n'.join(result)))
            return False, result

        _LOG.info('Project %s built', self.name)
        return True, result

    def exists(self):
        return os.path.exists(self.path)

    def install(self):
        """Install packages under worker.py and external callers."""

        _, result = _run(
            [self._get_gradlew(), 'installDebug'], cwd=self.path,
            env=_Sdk.get_shell_env(), strict=False)

        if self._gradlew_failed(result):
            message = (
                'Unable to build and install debug package from Project %s; '
                'error:\n%s') % (self.name, '\n'.join(result))
            _LOG.error(message)
            return False, result

        else:
            _LOG.info('Installed debug package from Project %s', self.name)

        _, result = _run(
            [self._get_gradlew(), 'installDebugTest'], cwd=self.path,
            env=_Sdk.get_shell_env(), strict=False)

        if self._gradlew_failed(result):
            message = (
                'Unable to build and install debug test package from Project '
                '%s; error:\n%s') % (self.name, '\n'.join(result))
            _LOG.error(message)
            return False, result

        else:
            _LOG.info('Installed debug test package from Project %s', self.name)

        return (
            True,
            [('Debug and test debug packages installed from Project '
              '%s') % self.name])

    def patch(self, patch):
        """Apply a patch to the project's filesystem."""

        if not os.path.exists(patch.filename):
            _die('Unable to apply patch; no file named ' + patch.filename)

        with open(patch.filename, 'w') as f:
            f.write(patch.contents)

        _LOG.debug(
            'Patched file %s with contents fingerprint %s',
            patch.filename, _get_fingerprint(patch.contents))

    def test(self):
        """Runs tests under worker.py and external callers."""

        _, result = _run([
            _Sdk.get_adb(), 'shell', 'am', 'instrument', '-w', '-e', 'class',
            self.test_class,
            '%s/android.test.InstrumentationTestRunner' % self.test_package])

        if self._tests_failed(result):
            message = 'Tests failed for project %s; result:\n%s' % (
                self.name, '\n'.join(result))
            _LOG.error(message)
            return False, result

        else:
            _LOG.info('Tests passed for project %s', self.name)

        return True, self._get_b64encoded_image()

    def uninstall(self, strict=False):
        """Uninstall packages under worker.py only."""

        handler = _get_strict_handler(strict)
        _, result = _run(
            [self._get_gradlew(), 'uninstallDebugTest'], cwd=self.path,
            env=_Sdk.get_shell_env(), strict=False)

        if self._gradlew_failed(result):
            handler(
                ('Unable to uninstall debug test package from Project '
                 '%s') % self.name)
        else:
            _LOG.info(
                'Uninstalled debug test package from Project %s', self.name)

        _, result = _run(
            [self._get_gradlew(), 'uninstallDebug'], cwd=self.path,
            env=_Sdk.get_shell_env(), strict=False)

        if self._gradlew_failed(result):
            handler(
                'Unable to uninstall debug package for Project %s' % self.name)
        else:
            _LOG.info('Uninstalled debug package from Project %s', self.name)

    def _get_b64encoded_image(self):
        local_path = os.path.join(self.path, _RESULT_IMAGE_NAME)
        _run([
            _Sdk.get_adb(), 'pull',
            os.path.join('/sdcard/Robotium-screenshots/', _RESULT_IMAGE_NAME),
            os.path.join(self.path, _RESULT_IMAGE_NAME)])

        with open(local_path) as f:
            return base64.b64encode(f.read())

    def _get_gradlew(self):
        return os.path.join(self.path, 'gradlew')

    def _gradlew_failed(self, result):
        return _GRADLEW_INSTALL_SUCCESS_NEEDLE not in result

    def _tests_failed(self, result):
        return _TEST_FAILURE_NEEDLE in result


class _Runtime(object):

    _DEVICE_TMP = '/data/local/tmp'

    def __init__(self, project_name, path, avd, port, sdcard, sdcard_size):
        self.avd = avd
        self.path = path
        self.port = port
        self.project_name = project_name
        self.sdcard = sdcard
        self.sdcard_size = sdcard_size

    @classmethod
    def from_config(cls, key, value):
        return cls(
            key, os.path.join(_RUNTIMES_PATH, key),
            os.path.join(_RUNTIMES_PATH, key, value['avd']),
            str(value['port']), os.path.join(_RUNTIMES_PATH, key,
            value['sdcard']), value['sdcardSize'])

    def block_until_ready(self, interval_msec=1000, timeout_sec=60*10):
        start = datetime.datetime.utcnow()

        while not self.ready():
            now = datetime.datetime.utcnow()
            delta_sec = (now - start).total_seconds()

            if delta_sec > timeout_sec:
                _die(
                    'Runtime %s timed out at %ss; aborting' % (
                        self.project_name, delta_sec))

            _LOG.debug(
                'Waiting %sms for runtime %s', interval_msec, self.project_name)
            time.sleep(interval_msec / 1000.0)

    def clean(self):
        self._avd_delete()
        self._sdcard_delete()
        self._dir_delete()

    def create(self):
        self._dir_create()
        self._sdcard_create()
        self._avd_create()

    def exists(self):
        return (
            self._dir_exists() and self._sdcard_exists() and self._avd_exists())

    def ready(self):
        return self._emulator_ready()

    def start(self, headless=True):
        self._emulator_start(headless=headless)

    def stop(self):
        self._emulator_stop()

    def _avd_create(self, strict=False):
        name = self._avd_name_get()
        path = self._avd_path_get()
        handler = _get_strict_handler(strict)

        if self._avd_exists():
            handler('Unable to create AVD at %s; already exists' % path)
            return

        code, result = _run([
            _Sdk.get_android(), 'create', 'avd', '-n', name, '-t', 'android-19',
            '--abi', 'default/armeabi-v7a', '-p', path],
            proc_fn=self._avd_create_proc_fn)

        if code:
            _die('Unable to create avd %s; error was: %s' % (path, result))

        _LOG.info('Created AVD named %s at %s', name, path)

    def _avd_create_proc_fn(self, process):
        process.stdin.write('\n')
        process.stdin.flush()

    def _avd_delete(self, strict=False):
        handler = _get_strict_handler(strict)
        name = self._avd_name_get()
        path = self._avd_path_get()

        if not self._avd_exists():
            handler(
                'Unable to delete AVD named %s from %s; does not exist' % (
                    name, path))
            return

        code, _ = _run(
            [_Sdk.get_android(), 'delete', 'avd', '-n', name], strict=False)

        if code:
            _LOG.warning(
                'Unable to remove AVD via Android SDK; falling back to manual '
                'cleanup. This may not be entirely accurate.')
            self._avd_delete_manually()
        else:
            _LOG.info('Deleted AVD named %s from %s', name, path)

    def _avd_delete_manually(self):
        name = self._avd_name_get()
        path = self._avd_path_get()

        message = 'Unable to remove AVD named %s from %s; does not exist' % (
            name, path)
        if os.path.exists(path):
            shutil.rmtree(path)
            message = 'Removed AVD named %s at %s' % (name, path)

        _LOG.info(message)

        # Path created by Android in addition to the path we specify when making
        # the AVD.
        internal_path = os.path.join(
            '~', '.android', 'avd', self._avd_name_get().lower())

        message = (
            'Unable to remove internal AVD named %s from %s; does not '
            'exist') % (name, internal_path)
        if os.path.exists(internal_path):
            shutil.rmtree(internal_path)
            message = 'Removed internal AVD named %s at %s' % (
                name, internal_path)

        _LOG.info(message)

    def _avd_exists(self):
        return os.path.exists(self._avd_path_get())

    def _avd_name_get(self):
        return ('%s_avd' % self.project_name).lower()

    def _avd_path_get(self):
        return os.path.join(self._dir_get(), self._avd_name_get())

    def _device_tmp_path_get(self, filename):
        return os.path.join(self._DEVICE_TMP, filename)

    def _dir_create(self, strict=False):
        handler = _get_strict_handler(strict)
        path = self._dir_get()

        if self._dir_exists():
            handler(
                'Unable to create runtime directory %s; already exists' % path)
            return

        os.makedirs(path)
        _LOG.info('Created runtime directory %s', path)

    def _dir_delete(self, strict=False):
        handler = _get_strict_handler(strict)
        path = self._dir_get()

        if not self._dir_exists():
            handler(
                'Unable to delete runtime directory %s; does not exist' % path)
            return

        shutil.rmtree(path)
        _LOG.info('Removed runtime directory %s', path)

    def _dir_get(self):
        return os.path.join(_RUNTIMES_PATH, self.project_name)

    def _dir_exists(self):
        return os.path.exists(self._dir_get())

    def _emulator_name_get(self):
        return '%s-%s' % (_EMULATOR, self.port)

    def _emulator_ready(self):
        if not self._emulator_running():
            return False

        code, result = _run(
            [_Sdk.get_adb(), 'shell', 'getprop', _BOOT_ANIMATION_PROPERTY],
            strict=False)

        if not code and result[0] == _BOOT_ANIMATION_STOPPED:
            return True

        return False

    def _emulator_running(self):
        _, result = _run([_Sdk.get_adb(), 'devices'])

        for line in result:
            if line.startswith(self._emulator_name_get()):
                return True

        return False

    def _emulator_start(self, headless):
        """Start an emulator in a child process."""

        def emulator(project_name, headless=True):
            headless_args = ['-no-audio', '-no-window'] if headless else []
            code, result = _run([
                _Sdk.get_emulator(), '-avd', os.path.basename(self.avd),
                '-sdcard', self.sdcard, '-port', self.port,
                '-force-32bit'] + headless_args, env=_Sdk.get_shell_env())

            if code:
                _die(
                    'Error starting emulator for runtime %s; reason: %s' % (
                        project_name, '\n'.join(result)))

        child = multiprocessing.Process(
            target=emulator, args=(self.project_name, headless))
        child.daemon = True
        child.start()
        _LOG.info(
            'Emulator for runtime %s started on port %s',
            self.project_name, self.port)

    def _emulator_stop(self, strict=False):
        handler = _get_strict_handler(strict)

        if not self._emulator_running():
            handler(
                'Cannot stop emulator for runtime %s; not running' % (
                    self.project_name))
            return

        _run([
            _Sdk.get_adb(), '-s', self._emulator_name_get(), 'emu', 'kill'])
        _LOG.info('Emulator for runtime %s stopped', self.project_name)

    def _sdcard_create(self, strict=False):
        handler = _get_strict_handler(strict)

        if self._sdcard_exists():
            handler('Unable to create sdcard %s; already exists' % self.sdcard)
            return

        size = '%sM' % self.sdcard_size
        code, result = _run([_Sdk.get_mksdcard(), size, self.sdcard])

        if code:
            _die(
                'Unable to create sdcard %s; error was: %s' % (
                    self.sdcard, result))

        _LOG.info('Created %s sdcard: %s', size, self.sdcard)

    def _sdcard_delete(self, strict=False):
        handler = _get_strict_handler(strict)

        if not os.path.exists(self.sdcard):
            handler('Unable to remove sdcard %s; does not exist' % self.sdcard)
            return

        os.remove(self.sdcard)
        _LOG.info('Removed sdcard %s', self.sdcard)

    def _sdcard_exists(self):
        return os.path.exists(self.sdcard)


class _Sdk(object):

    PATH = os.path.join(_RESOURCES_PATH, 'sdk')
    _VERSION = 'adt-bundle-linux-x86_64-20140702'
    _URL = 'https://dl.google.com/android/adt/%s.zip' % _VERSION

    @classmethod
    def delete(cls, strict=False):
        handler = _get_strict_handler(strict)

        if not cls._is_installed():
            handler('Android SDK not installed')
            return

        shutil.rmtree(cls.PATH)
        _LOG.info('Android SDK deleted from %s', cls.PATH)

    @classmethod
    def get_adb(cls):
        return cls._get_tool('adb', directory='platform-tools')

    @classmethod
    def get_android(cls):
        return cls._get_tool('android')

    @classmethod
    def get_emulator(cls):
        return cls._get_tool('emulator')

    @classmethod
    def get_shell_env(cls):
        display = os.environ.get('DISPLAY')
        if not display:
            _die('Could not get shell variable DISPLAY')

        return {
            _ANDROID_HOME: cls.PATH,
            _ANDROID_SDK_HOME: os.path.expanduser('~'),
            _DISPLAY: display,
        }

    @classmethod
    def get_mksdcard(cls):
        return cls._get_tool('mksdcard')

    @classmethod
    def install(cls):
        cls._download()
        cls._install_from_download()
        cls._update()

    @classmethod
    def is_installed(cls):
        return os.path.exists(cls.PATH)

    @classmethod
    def _accept_licenses(cls, process):
        """Scan through android sdk update output and accept licenses."""
        seen = set()

        for line in iter(process.stdout.readline, ''):
            if _ACCEPT_LICENSE_NEEDLE in line and not process.poll():
                license_name = re.findall(r"'(.+)'", line)[0]

                if license_name not in seen:
                    seen.add(license_name)
                    process.stdin.write('y\n')
                    process.stdin.flush()
                    _LOG.info('Accepted license %s', license_name)

            # TODO(johncox): figure out why this is needed to keep the process
            # from hanging.
            process.stdin.write('\n')
            process.stdin.flush()

    @classmethod
    def _download(cls):
        path = cls._get_download_path()
        _LOG.info('Downloading Android SDK from %s to %s', cls._URL, path)
        _run(['curl', '-o', path, cls._URL])

    @classmethod
    def _get_download_path(cls):
        return os.path.join(_RESOURCES_TMP_PATH, 'android-sdk.zip')

    @classmethod
    def _get_tool(cls, name, directory='tools'):
        path = os.path.join(cls.PATH, directory, name)

        if not os.path.exists(path):
            _die('SDK tool %s not found at %s' % (name, path))

        return path

    @classmethod
    def _install_from_download(cls):
        _run([
            'unzip', cls._get_download_path(), '-d',
            os.path.join(_RESOURCES_TMP_PATH,)])
        shutil.move(
            os.path.join(_RESOURCES_TMP_PATH, cls._VERSION, 'sdk'), cls.PATH)
        _LOG.info('Android SDK installed in %s', cls.PATH)

    @classmethod
    def _update(cls):
        _LOG.info('Updating SDK. This takes a long time; please be patient')
        _run(
            [cls.get_android(), 'update', 'sdk', '-a', '--no-ui'],
            proc_fn=cls._accept_licenses)


class _TestEnvironment(object):
    """An environment for test execution.

    Manages creation of filesystem for result storage, scratch space for copying
    and patching over the golden project, and log redirection.

    Lifecycle is:

        * Initialize environment.
        * Call set_up() to configure external dependencies (filesystem, loggers,
          etc.).
        * If operating on a project is desired, call set_up_projects().
        * Do work.
        * Always call tear_down().
    """

    _OUT = 'out'

    def __init__(self, ticket):
        self._handler = None
        self.path = self._get_path(ticket)
        self._projects_set_up = False
        self.out_path = os.path.join(self.path, self._OUT)
        self.src_project = None
        self.test_project = None
        self.ticket = ticket

    @classmethod
    def get_test_run(cls, ticket):
        root_path = cls._get_path(ticket)
        json_path = cls._get_result_json_path(ticket)
        test_run = TestRun()

        if not (os.path.exists(root_path) and os.path.exists(json_path)):
            test_run.set_status(TestRun.NOT_FOUND)
            test_run.set_payload('No test results found')
            return test_run

        try:
            with open(json_path) as f:
                result = json.loads(f.read())
                test_run.set_payload(result['payload'])
                test_run.set_status(result['status'])
        except:  # Treat all errors the same. pylint: disable=bare-except
            test_run.set_status(TestRun.CONTENTS_MALFORMED)
            test_run.set_payload('Test result malformed')

        return test_run

    @classmethod
    def _get_path(cls, ticket):
        return os.path.join(_RESULTS_PATH, str(ticket))

    @classmethod
    def _get_result_json_path(cls, ticket):
        return os.path.join(cls._get_path(ticket), cls._OUT, _RESULT_JSON_NAME)

    def save(self, test_run):
        json_path = os.path.join(self.out_path, _RESULT_JSON_NAME)
        with open(json_path, 'w') as f:
            f.write(json.dumps(test_run.to_dict()))

        _LOG.info('Result saved to ' + json_path)

    def set_up(self):
        """Sets up everything but projects."""

        self._configure_filesystem()
        self._configure_logging()
        self._clean_old()

    def set_up_projects(self, patches, src_project):
        """Sets up projects and applies patches."""

        self._configure_projects(src_project)
        self._copy_project()

        for patch in patches:
            self.test_project.patch(self._get_test_patch(patch))

        self._projects_set_up = True

    def tear_down(self):
        """Tears down both set_up() and set_up_projects()."""

        self._copy_result()
        self._remove_test_project()
        self._revert_logging()

    def _clean_old(self):
        """Cleans old test invocations from the filesystem.

        The _TestEnvironment code path is hit on each web worker test
        invocation, so we do cleanup here to keep the test results on the
        filesystem from filling disk.
        """
        now_sec = time.time()
        for path in os.listdir(_RESULTS_PATH):
            result_dir = os.path.join(_RESULTS_PATH, path)
            delta_sec = now_sec - os.path.getmtime(result_dir)
            if delta_sec >= _RESULTS_TTL_SEC:
                shutil.rmtree(result_dir)
                _LOG.info(
                    ('Result directory %s too old (delta: %ssec; TTL: %ssec); '
                     'removed'), result_dir, delta_sec, _RESULTS_TTL_SEC)

    def _configure_logging(self):
        """Also send log info to test project dir."""

        self._handler = logging.FileHandler(os.path.join(self.path, 'log'))
        self._handler.setLevel(_LOG.level)
        _LOG.addHandler(self._handler)

    def _configure_projects(self, src_project):
        relative_editor_file = src_project.editor_file.split(
            self._get_project_name_infix(src_project.name))[1]
        test_project_path = os.path.join(self.path, src_project.name)

        self.src_project = src_project
        self.test_project = _Project(
            src_project.name,
            os.path.join(test_project_path, relative_editor_file),
            src_project.package, test_project_path, src_project.test_class,
            src_project.test_package)

    def _copy_project(self):
        shutil.copytree(self.src_project.path, self.test_project.path)
        git_path = os.path.join(self.test_project.path, '.git')
        gradle_path = os.path.join(self.test_project.path, '.gradle')

        # Clean up files not needed for test builds/runs.
        for prune_dir in [git_path, gradle_path]:
            if os.path.exists(prune_dir):
                shutil.rmtree(prune_dir)

        _LOG.info(
            'Project %s staged into %s',
            self.test_project.name, self.test_project.path)

    def _copy_result(self):
        if not self._projects_set_up:
            return

        copy_from = os.path.join(self.test_project.path, _RESULT_IMAGE_NAME)
        # When build/test fails there may not be a result image.
        if not os.path.exists(copy_from):
            _LOG.info('No result image found at ' + copy_from)
        else:
            copy_to = os.path.join(self.out_path, _RESULT_IMAGE_NAME)
            shutil.copyfile(copy_from, copy_to)
            _LOG.info('Result image saved to ' + copy_to)

    def _configure_filesystem(self):
        os.makedirs(self.path)
        os.makedirs(self.out_path)

    def _get_project_name_infix(self, project_name):
        return '/%s/' % project_name

    def _get_test_patch(self, patch):
        # Rehome patch under test project.
        _, suffix = patch.filename.split(
            self._get_project_name_infix(self.src_project.name))
        return Patch(
            os.path.join(self.test_project.path, suffix), patch.contents)

    def _remove_test_project(self):
        if not self._projects_set_up:
            return

        # Entire test filesystem tree may have been removed already due to age.
        if os.path.exists(self.test_project.path):
            shutil.rmtree(self.test_project.path)
            _LOG.info(
                'Project %s unstaged from %s ',
                self.test_project.name, self.test_project.path)

    def _revert_logging(self):
        if self._handler is not None:
            _LOG.removeHandler(self._handler)

        self._handler = None


if __name__ == '__main__':
    main(_PARSER.parse_args())
