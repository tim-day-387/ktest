# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

from ..utils import get_podman_client


def cmd_info(args, podman_socket=None):
    """Display podman info."""
    with get_podman_client(podman_socket) as client:
        print(client.info())
