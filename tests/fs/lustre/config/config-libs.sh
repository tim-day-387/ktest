#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

#
# Shared body for the Lustre kernel config tests.
#
# Each tests/fs/lustre/config/*.ktest builds the in-tree Lustre (native
# platform, copy-builtin) under one kernel configuration: everything
# modular, everything built in, client only, a single Lustre option
# toggled, or a kernel feature Lustre leans on switched off. Every test
# then runs the same smoke test: load the modules and mount a co-located
# client/server filesystem on the wbcfs OSD.
#
# require-config-base-kernel-config gives every test what Lustre needs to
# build plus module support. The Lustre options themselves are left to the
# individual test, so a test can turn an option off as well as on.
#

. "$(dirname "$(readlink -e "${BASH_SOURCE[0]}")")/../lustre-libs.sh"

config-mem 10G
config-timeout 60

function require-config-base-kernel-config()
{
    require-lustre-base-kernel-config
    require-lustre-modules-kernel-config
}

#
# Modular in-tree Lustre: the baseline the single-option tests build on.
#
function require-config-modular-kernel-config()
{
    require-config-base-kernel-config
    require-lustre-inkernel-kernel-config
}

#
# True if the running kernel was built with the given CONFIG_ option.
#
function kernel_has_config()
{
    zcat /proc/config.gz | grep -q "^CONFIG_$1=[ym]$"
}

test_llmount()
{
    modprobe lnet
    modprobe lustre

    # A client-only kernel has nothing to mount: loading the modules
    # is the whole test.
    if ! kernel_has_config LUSTRE_FS_SERVER; then
	lsmod
	"$LUSTRE_RMMOD"
	return
    fi

    setup_lustrefs

    sleep 10

    cleanup_lustrefs
}
