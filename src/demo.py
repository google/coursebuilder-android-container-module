# Copyright 2015 Google Inc. All Rights Reserved.
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

"""Demo client for the Android container module."""

__author__ = [
    'johncox@google.com (John Cox)',
]

import os

from common import jinja_utils
from controllers import utils
from models import custom_modules

_BASE_PATH = os.path.dirname(__file__)
_RESOURCES_PATH = os.path.join(_BASE_PATH, 'resources')
_TEMPLATES_PATH = os.path.join(_BASE_PATH, 'templates')


class _DemoHandler(utils.BaseHandler):

    def get(self):
        template = jinja_utils.get_template('index.html', [_TEMPLATES_PATH])
        self.response.write(template.render({}))


class _ResourceHandler(utils.BaseHandler):

    def get(self):
        self.response.headers['Content-Type'] = 'text/javascript'
        with open(os.path.join(_RESOURCES_PATH, 'client.js')) as f:
            self.response.write(f.read())


custom_module = None


def register_module():

    global custom_module

    global_handlers = [
        ('/demo.*', _DemoHandler),
        ('/client.js', _ResourceHandler),
    ]
    namespaced_handlers = []
    custom_module = custom_modules.Module(
        'Android Container Demo', 'Android Container Demo', global_handlers,
        namespaced_handlers)

    return custom_module
