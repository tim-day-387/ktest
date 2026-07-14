# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

DEPLOY_CONTAINER_NAME = "lustre-ci"
DEPLOY_NGINX_PORT = 8080

IMAGES = [
    {
        "dockerfile": "containers/Containerfile.ktest.u26",
        "tag": "ktest-runner:latest",
        "name": "ktest-runner",
    },
    {
        "dockerfile": "containers/Containerfile.lustre.u24",
        "tag": "lustre-u24:latest",
        "name": "lustre-u24",
    },
    {
        "dockerfile": "containers/Containerfile.lustre.al2023",
        "tag": "lustre-al2023:latest",
        "name": "lustre-al2023",
    },
    {
        "dockerfile": "containers/Containerfile.lustre.al2",
        "tag": "lustre-al2:latest",
        "name": "lustre-al2",
    },
    {
        "dockerfile": "containers/Containerfile.lustre.rocky9",
        "tag": "lustre-rocky9:latest",
        "name": "lustre-rocky9",
    },
    {
        "dockerfile": "containers/Containerfile.lustre.rocky9.coverity",
        "tag": "lustre-rocky9-coverity:latest",
        "name": "lustre-rocky9-coverity",
        "requires": "lustre-rocky9",
    },
    {
        "dockerfile": "containers/Containerfile.lustre.rocky10",
        "tag": "lustre-rocky10:latest",
        "name": "lustre-rocky10",
    },
    {
        "dockerfile": "containers/Containerfile.ci-lustre",
        "tag": "ci-lustre:latest",
        "name": "ci-lustre",
    },
]

CONFIGS = {
    "native_1": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild generate_lustre_patch",
        "working_dir": "/home/ktest/ktest/",
    },
    "zfs_patch": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild generate_zfs_patch",
        "working_dir": "/home/ktest/ktest/",
        "sync_zfs": True,
    },
    "native_2": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild build_native",
        "package_script": "./qlkbuild package_native",
        "working_dir": "/home/ktest/ktest/",
        "run_script": "./qlkbuild run_native",
    },
    "mainline": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild build",
        "working_dir": "/home/ktest/ktest/",
        "run_script": "./qlkbuild run",
        "sync_zfs": True,
    },
    "mainline_ccplugin": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild ccplugin",
        "working_dir": "/home/ktest/ktest/",
        "sync_zfs": True,
    },
    # Kernel-only build with the compiler output visible in the job log. No
    # lustre/zfs, so no ZFS source sync and no build artifact archive needed.
    "mainline_kbuild": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild kbuild",
        "working_dir": "/home/ktest/ktest/",
    },
    "u24": {
        "image": "lustre-u24:latest",
        "build_script": """
export HOME=/tmp
./autogen.sh
./configure --disable-server
make --quiet -j$(nproc)
""",
        "package_script": """
export HOME=/tmp
./autogen.sh
./configure --disable-server
make --quiet -j$(nproc) debs
""",
        "working_dir": "/home/ktest/git/lustre-release/",
        "distro_platform": True,
    },
    "al2023": {
        "image": "lustre-al2023:latest",
        "build_script": """
./autogen.sh
./configure --enable-efa
make --quiet -j$(nproc)
""",
        "package_script": """
./autogen.sh
./configure --enable-efa
make --quiet -j$(nproc) rpms
""",
        "working_dir": "/home/ktest/git/lustre-release/",
        "distro_platform": True,
    },
    "al2": {
        "image": "lustre-al2:latest",
        # The default gcc 7.3 cannot build against the amzn2 kernel, which
        # was configured with a newer compiler (CONFIG_CC_HAS_ASM_INLINE).
        "build_script": """
./autogen.sh
CC=gcc10-gcc ./configure
make --quiet -j$(nproc)
""",
        "package_script": """
./autogen.sh
CC=gcc10-gcc ./configure
make --quiet -j$(nproc) rpms
""",
        "working_dir": "/home/ktest/git/lustre-release/",
        "distro_platform": True,
    },
    "rocky9": {
        "image": "lustre-rocky9:latest",
        "build_script": """
O2IB_PATH=$(ls -d /usr/src/ofa_kernel/x86_64/*)
./autogen.sh
./configure --enable-server
make --quiet -j$(nproc)
""",
        "package_script": """
O2IB_PATH=$(ls -d /usr/src/ofa_kernel/x86_64/*)
./autogen.sh
./configure --disable-server
make --quiet -j$(nproc) rpms
""",
        "working_dir": "/home/ktest/git/lustre-release/",
        "distro_platform": True,
    },
    "rocky9_coverity": {
        "image": "lustre-rocky9-coverity:latest",
        "package_script": "/usr/local/bin/coverity-run",
        "working_dir": "/home/ktest/git/lustre-release/",
        "distro_platform": True,
        "mount_ktest_lib": True,
    },
    "rocky10": {
        "image": "lustre-rocky10:latest",
        "build_script": """
O2IB_PATH=$(ls -d /usr/src/ofa_kernel/x86_64/*)
./autogen.sh
./configure --enable-server
make --quiet -j$(nproc)
""",
        "package_script": """
O2IB_PATH=$(ls -d /usr/src/ofa_kernel/x86_64/*)
./autogen.sh
./configure --disable-server
make --quiet -j$(nproc) rpms
""",
        "working_dir": "/home/ktest/git/lustre-release/",
        "distro_platform": True,
    },
}
