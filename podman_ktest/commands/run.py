# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import podman

from ..models import ContainerJob
from ..utils import get_podman_socket, get_ccache_dir, create_source_tarballs


def cmd_run(
    args, dirs, podman_socket=None, shared_filesystem=None, tarball_input=False
):
    """Run a command in a ktest container."""
    # Only create tarballs if using tarball input mode
    tarball_paths = None
    if tarball_input:
        tarball_paths = create_source_tarballs(dirs)

    # Resolve ccache directory
    ccache_dir = get_ccache_dir(shared_filesystem)

    socket_url = get_podman_socket(podman_socket)
    with podman.PodmanClient(base_url=socket_url) as client:
        job = ContainerJob(
            image="ktest-runner:latest",
            command=args.command,
            working_dir="/home/ktest/ktest",
            ktest_out_dir=None,
            tarball_paths=tarball_paths,
            sync_kernel=False,
            sync_lustre=False,
            sync_ktest_out=False,
            dirs=dirs,
            use_tarball_input=tarball_input,
            ccache_dir=ccache_dir,
            log_path=None,
        )
        with job:
            return_code = job.run(client)

        return return_code
