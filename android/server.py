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

"""Worker machine web server."""

import argparse
import BaseHTTPServer
import collections
import json
import logging
import os
import re
import SocketServer
import subprocess
import sys
import traceback
import urllib
import urlparse

import worker

_CHOICE_START = 'start'
_CHOICES = [
    _CHOICE_START
]
_CLIENT_JS_PATH = os.path.join(worker.ROOT_PATH, 'client.js')
_DEFAULT_HOST = subprocess.check_output(['hostname']).strip()
_DEFAULT_PORT = 8080
_DEFAULT_LOG_PATH = os.path.join(worker.ROOT_PATH, 'server.log')
_INDEX_HTML_PATH = os.path.join(worker.ROOT_PATH, 'index.html')
_LOG = logging.getLogger('android.server')
_STATUS = 'status'
# Keep _STATUS_* in sync with _ExternalTask.STATUSES.
_STATUS_COMPLETE = 'complete'
_STATUS_CREATED = 'created'
_STATUS_DELETED = 'deleted'
_STATUS_FAILED = 'failed'
_STATUS_RUNNING = 'running'
# Translates from worker statuses to fe statuses.
_STATUS_MAP = {
    worker.TestRun.BUILD_FAILED: _STATUS_FAILED,
    worker.TestRun.BUILD_SUCCEEDED: _STATUS_RUNNING,
    worker.TestRun.CONTENTS_MALFORMED: _STATUS_FAILED,
    worker.TestRun.NOT_FOUND: _STATUS_FAILED,
    worker.TestRun.PROJECT_MISCONFIGURED: _STATUS_FAILED,
    worker.TestRun.RUNTIME_MISCONFIGURED: _STATUS_FAILED,
    worker.TestRun.RUNTIME_NOT_RUNNING: _STATUS_FAILED,
    worker.TestRun.TESTS_FAILED: _STATUS_FAILED,
    worker.TestRun.TESTS_RUNNING: _STATUS_RUNNING,
    worker.TestRun.TESTS_SUCCEEDED: _STATUS_COMPLETE,
    worker.TestRun.UNAVAILABLE: _STATUS_FAILED,
}
assert len(worker.TestRun.STATUSES) == len(_STATUS_MAP)
_TICKET = 'ticket'
_WORKER_ID = 'worker_id'

_PARSER = argparse.ArgumentParser()
_PARSER.add_argument(
    '--log_file', type=str, default=_DEFAULT_LOG_PATH,
    help='Absolute path of the file used for logging')
_PARSER.add_argument(
    '--log_level', type=str, choices=worker.LOG_LEVEL_CHOICES,
    default=worker.LOG_INFO,
    help='Display log messages at or above this level')
_PARSER.add_argument(
    '--host', type=str, default=_DEFAULT_HOST, help='Host to run on')
_PARSER.add_argument(
    '--port', type=int, default=_DEFAULT_PORT, help='Port to run on')


_SystemState = collections.namedtuple(
    '_SystemState',
    ['success', 'project_name', 'request_args', 'config', 'project', 'runtime'])


class _Environment(object):

    HOST = None
    PORT = None

    @classmethod
    def get_worker_id(cls):
        assert cls.HOST is not None and cls.PORT is not None
        return 'http://%s:%s' % (cls.HOST, cls.PORT)

    @classmethod
    def set(cls, host, port):
        cls.HOST = host
        cls.PORT = port


class _Handler(BaseHTTPServer.BaseHTTPRequestHandler):

    _POST_DELETE = re.compile('^/.*/delete$')

    def _dispatch_rest_post(self):
        if self._POST_DELETE.match(self.path):
            self._do_rest_POST_delete()
        else:
            self._do_rest_POST_create()

    def _do_404_response(self):
        self.send_response(404)
        self._set_headers({
            'Content-Length': 0,
            'Content-Type': 'text/html',
        })

    def _do_GET_health(self):
        # 'Healthy' means 'can work on new tasks'. 'Unhealthy' workers can still
        # answer get requests for projects or task results -- probably. This
        # health check could be made more robust.
        self.send_response(500 if worker.Lock.active() else 200)
        self._set_headers({'Content-Type': 'text/html'})

    def _do_json_response(self, response, code=200):
        full_response = {'payload': response}
        self.send_response(code)
        self._set_headers({'Content-Type': 'text/javascript'})
        self.wfile.write(json.dumps(full_response))

    def _do_rest_GET_project(self):
        state = self._get_system_state_or_record_error(
            get_request_args_fn=self._get_get_args)
        if not state.success:
            return

        contents = None
        with open(state.project.editor_file) as f:
            contents = f.read()

        self._do_json_response({
            'contents': contents,
            'filename': state.project.editor_file,
            'projectName': state.project_name,
        })

    def _do_rest_GET_test_run(self):
        request_args = self._get_get_args()
        ticket = request_args.get(_TICKET)
        worker_id = request_args.get(_WORKER_ID)

        if worker_id != _Environment.get_worker_id():
            self._do_json_response('Request sent to wrong worker', code=500)
            return

        # Treat as module-protected. pylint: disable=protected-access
        test_run = worker._TestEnvironment.get_test_run(ticket)
        code = 200

        status = test_run.get_status()
        if status == worker.TestRun.NOT_FOUND:
            code = 404

        result = test_run.to_dict()
        result[_STATUS] = _STATUS_MAP.get(status)
        self._do_json_response(result, code=code)

    def _do_rest_POST_create(self):
        if worker.Lock.active():
            self._do_json_response('Worker locked', code=500)
            return

        state = self._get_system_state_or_record_error(
            get_request_args_fn=self._get_post_args)
        if not state.success:
            return

        patches = []
        for patch in state.request_args.get('payload', {}).get('patches', []):
            patches.append(worker.Patch(patch['filename'], patch['contents']))

        ticket = state.request_args.get('ticket')
        pid = worker.fork_test(
            state.config, state.project_name, ticket, patches=patches)

        if pid is None:
            self._do_json_response('Unable to start worker process', code=500)

        self._do_json_response({
            _TICKET: ticket,
            _WORKER_ID: _Environment.get_worker_id(),
        })

    def _do_rest_POST_delete(self):
        _LOG.info('TODO: implement rest POST delete')

    def _get_get_args(self):
        encoded = urlparse.urlparse(self.path).query.lstrip('request=')
        return json.loads(urllib.unquote_plus(encoded))

    def _get_post_args(self):
        data = self.rfile.read(int(self.headers.getheader('content-length')))
        return json.loads(data)

    def _get_project_name(self, path):
        return path.split('=')[1]

    def _get_system_state_or_record_error(self, get_request_args_fn=None):
        config = worker.Config.load()
        request_args = get_request_args_fn()
        payload = request_args.get('payload', {})
        project_name = payload.get('project', None)

        if not project_name:
            self._do_json_response('Must specify project', code=400)
            return _SystemState(False, None, None, None, None, None)

        project = config.projects.get(project_name)
        runtime = config.runtimes.get(project_name)

        if not (project and runtime):
            self._do_json_response('Environment not configured', code=500)
            return _SystemState(False, None, None, None, None, None)

        if not os.path.exists(project.editor_file):
            self._do_json_response('Projects misconfigured', code=500)
            return _SystemState(False, None, None, None, None, None)

        return _SystemState(
            True, project_name, request_args, config, project, runtime)

    def _set_headers(self, headers):
        for key, value in headers.iteritems():
            self.send_header(key, value)

        self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            self._do_GET_health()
        elif self.path.startswith('/rest/v1/project'):
            self._do_rest_GET_project()
        elif self.path.startswith('/rest/v1'):
            self._do_rest_GET_test_run()
        else:
            self._do_404_response()

    def do_POST(self):
        if self.path.startswith('/rest/v1'):
            self._dispatch_rest_post()
        else:
            self._do_404_response()

    def log_message(self, format_template, *args):
        _LOG.info('%(address)s - - [%(timestamp)s] %(rest)s', {
            'address': self.address_string(),
            'timestamp': self.log_date_time_string(),
            'rest': format_template % args,
        })


class _HttpServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    # Allow address reuse immediately after a server has stopped so we don't get
    # spurious errors during dev.
    allow_reuse_address = True


def _get_last_exception_str():
    return ''.join(traceback.format_exception(*sys.exc_info()))


def _get_server(host, port):
    return _HttpServer((host, port), _Handler)


def main(args):
    worker.configure_logger(args.log_level, log_file=args.log_file)
    _start(args.host, args.port)


def _start(host, port):
    server = _get_server(host, port)
    try:
        _LOG.info('Starting server at http://%(host)s:%(port)s', {
            'host': host,
            'port': port,
        })
        server.serve_forever()
    except:  # Treat all errors the same. pylint: disable=bare-except
        _LOG.info('Stopping server; reason:\n' + _get_last_exception_str())
        server.socket.close()


if __name__ == '__main__':
    parsed_args = _PARSER.parse_args()
    _Environment.set(parsed_args.host, parsed_args.port)
    main(parsed_args)
