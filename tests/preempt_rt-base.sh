#!/usr/bin/env bash

. $(dirname $(readlink -e ${BASH_SOURCE[0]}))/test-libs.sh

require-expert-kernel-config
require-kernel-config PREEMPT_RT
require-kernel-config DEBUG_PREEMPT

call_base_test preempt_rt "$@"
