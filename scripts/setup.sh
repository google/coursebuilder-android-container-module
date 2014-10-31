#!/bin/bash

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
#
# author: johncox@google.com (John Cox)
#
# Android Container Demo Course Builder module setup script.
#

set -e
shopt -s nullglob

export SOURCE_DIR="$( cd "$( dirname "$0" )" && cd .. && pwd )"
export MODULE_NAME=android_container
export MODULE_SRC_DIR=$SOURCE_DIR/src


function check_directory() {
    # Die if the given directory does not exist.
    local target=$1 && shift

    if [ ! -d $target ]; then
        echo "$target does not exist; aborting"
        exit 1
    fi
}


function link_module() {
    # Symlinks module files into Course Builder directory.
    local target=$1 && shift
    _link $MODULE_SRC_DIR $target/modules/$MODULE_NAME
}


function _link() {
    local from=$1 && shift
    local to=$1 && shift

    if [ ! -h $to ]; then
        ln -s $from $to
    fi
}


function usage() { cat <<EOF

Usage: $0 [-d <directory>]

-d  Required argument. Absolute path to the directory containing your Course
    Builder installation.
-h  Show this message

EOF
}


while getopts d:h option
do
    case $option
    in
        d)  TARGET="$OPTARG" ;;
        h)  usage; exit 0;;
        *)  usage; exit 1;;
    esac
done

if [ ! $TARGET ]; then
  usage
  exit 1
fi

check_directory $TARGET
link_module $TARGET
