# SPDX-License-Identifier: Apache-2.0
#
# lit configuration for the ccplugin test suite. Run with
#
#   make check
#
# or directly via
#
#   lit -sv test
#
# Each test builds a tiny fake lustre tree (the lints only report findings
# for paths containing "lustre-release") plus a compilation database in the
# test's temp dir, runs the built xunused on it and FileChecks the output.

import glob
import os
import re
import shutil

import lit.formats

config.name = 'ccplugin'
config.test_format = lit.formats.ShTest()
config.suffixes = ['.c', '.cpp', '.test']
config.test_source_root = os.path.dirname(__file__)

# Where the per-test Output/ directories go. Defaults to the test tree;
# set CCPLUGIN_TEST_OUT when that is not writable (the cplugin-tests job
# runs from the read-only ktest checkout in the container). The lints
# report paths relative to the first "lustre-release" component, so the
# fake tree each test builds must be the only one in the path.
exec_root = os.environ.get('CCPLUGIN_TEST_OUT', config.test_source_root)
if 'lustre-release' in exec_root:
    lit_config.fatal('test output path contains "lustre-release", which '
                     'breaks the lints\' path reporting: %s' % exec_root)
config.test_exec_root = exec_root

plugin_dir = os.path.dirname(config.test_source_root)

# The tool under test; set XUNUSED to test a binary elsewhere.
xunused = os.environ.get('XUNUSED', os.path.join(plugin_dir, 'build', 'xunused'))
if not os.path.exists(xunused):
    lit_config.fatal('%s not found: run make first or set XUNUSED' % xunused)

# FileCheck, split-file and not. Debian/Ubuntu ship only versioned names
# (FileCheck-18, ...) in /usr/bin, so when the bare names are not on PATH
# fall back to the newest /usr/lib/llvm-*/bin that has all of them.
tools = ['FileCheck', 'split-file', 'not']
if not all(shutil.which(t) for t in tools):
    best = None
    best_ver = -1
    for d in glob.glob('/usr/lib/llvm-*/bin'):
        m = re.search(r'llvm-(\d+)', d)
        if not m or int(m.group(1)) <= best_ver:
            continue
        if all(os.path.exists(os.path.join(d, t)) for t in tools):
            best = d
            best_ver = int(m.group(1))
    if best is None:
        lit_config.fatal('FileCheck/split-file/not missing: install LLVM '
                         'dev tools or add them to PATH')
    config.environment['PATH'] = os.pathsep.join(
        (best, config.environment.get('PATH', '')))

config.substitutions.append(('%xunused', xunused))
config.substitutions.append(
    ('%gen_db', 'sh ' + os.path.join(config.test_source_root, 'gen_db.sh')))
