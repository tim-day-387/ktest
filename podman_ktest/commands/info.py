# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import podman

from ..utils import get_podman_socket


def cmd_info(args, podman_socket=None):
    """Display podman info."""
    socket_url = get_podman_socket(podman_socket)
    with podman.PodmanClient(base_url=socket_url) as client:
        print(client.info())
