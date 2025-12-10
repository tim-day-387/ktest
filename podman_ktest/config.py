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
        "dockerfile": "containers/Containerfile.ktest.u22",
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
        "working_dir": "/home/ktest/ktest/",
        "run_script": "./qlkbuild run_native",
    },
    "mainline": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild build",
        "working_dir": "/home/ktest/ktest/",
        "run_script": "./qlkbuild run",
    },
    "u24": {
        "image": "lustre-u24:latest",
        "build_script": """
./autogen.sh
./configure --disable-server
make --quiet -j$(nproc)
""",
        "working_dir": "/home/ktest/git/lustre-release/",
    },
    "al2023": {
        "image": "lustre-al2023:latest",
        "build_script": """
./autogen.sh
./configure --enable-efa
make --quiet -j$(nproc)
""",
        "working_dir": "/home/ktest/git/lustre-release/",
    },
    "al2": {
        "image": "lustre-al2:latest",
        "build_script": """
./autogen.sh
./configure
make --quiet -j$(nproc)
""",
        "working_dir": "/home/ktest/git/lustre-release/",
    },
    "rocky9": {
        "image": "lustre-rocky9:latest",
        "build_script": """
./autogen.sh
./configure --enable-server
make --quiet -j$(nproc) rpms
""",
        "working_dir": "/home/ktest/git/lustre-release/",
    },
    "kernel_rpm": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild build_rpm",
        "working_dir": "/home/ktest/ktest/",
        "output_files": ["*.rpm"],
    },
    "kernel_deb": {
        "image": "ktest-runner:latest",
        "build_script": "./qlkbuild build_deb",
        "working_dir": "/home/ktest/ktest/",
        "output_files": ["*.deb"],
    },
}
