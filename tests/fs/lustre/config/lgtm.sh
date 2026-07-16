#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

#
# Shared "LGTM" base test for Lustre Kconfig coverage.
#
# Every tests/fs/lustre/config/*.ktest enables exactly one Lustre/LNet
# Kconfig option (on top of the in-kernel Lustre baseline) and then runs
# this same modprobe + llmount smoke test. If Lustre still loads and mounts
# with the option enabled, the config Looks Good To Me.
#
# The Kconfig options only take effect for the in-tree Lustre build (native
# platform, copy-builtin, ktest_lustre_intree=1); require-lustre-kernel-config
# pulls in the matching in-kernel modules there.
#

. "$(dirname "$(readlink -e "${BASH_SOURCE[0]}")")/../lustre-libs.sh"

config-mem 10G
config-timeout 60

#
# require-lgtm-kernel-config (the baseline kernel config shared by every LGTM
# run) lives in the top-level config/lustre.sh, alongside the other
# require-lustre-*-kernel-config helpers.
#
#
# The LGTM base test: load the Lustre modules (modprobe) and mount a
# co-located client/server filesystem (llmount), then tear it back down.
#
test_llmount()
{
    modprobe lnet
    modprobe lustre

    setup_lustrefs

    sleep 10

    cleanup_lustrefs
}
