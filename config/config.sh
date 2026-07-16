#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

#
# Global kernel config requirements, shared across all tests.
#
# Source this after test-libs.sh (for require-kernel-config). It pulls in the
# per-subsystem config libraries in this directory; add new ones below.
#

. "$ktest_dir/config/lustre.sh"
