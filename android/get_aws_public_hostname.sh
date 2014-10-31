#!/bin/bash

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
#
# author: johncox@google.com (John Cox)
#
# Writes AWS public hostname to .aws_public_hostname
#
# See Amazon docs at
# http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-metadata.html.

set -e

# AWS_HOSTNAME_URL is the API endpoint that allows you to fetch information
# about your current EC2 instance, which we use here to get the publicly-
# visible DNS name to relay along to the balancer for later polling operations.
export AWS_HOSTNAME_URL=http://169.254.169.254/latest/meta-data/public-hostname

export ROOT_DIR="$( cd "$( dirname "$0" )" && cd .. && pwd )"
export RESULT_PATH=$ROOT_DIR/android/.aws_public_hostname

curl -s $AWS_HOSTNAME_URL > $RESULT_PATH
