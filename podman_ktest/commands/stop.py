# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import podman

from ..config import IMAGES
from ..utils import get_podman_socket


def cmd_stop(args, podman_socket=None):
    """Stop all running ktest-related containers."""
    socket_url = get_podman_socket(podman_socket)

    # Build set of image name prefixes from IMAGES list
    # Extract base names without :latest suffix for matching
    image_prefixes = {img["name"] for img in IMAGES}

    with podman.PodmanClient(base_url=socket_url) as client:
        # Get all running containers
        containers = client.containers.list(all=False)

        stopped_count = 0
        skipped_count = 0

        for container in containers:
            container_name = container.name

            # Skip lustre-ci container unless --all flag is provided
            if container_name == "lustre-ci" and not args.all:
                print(f"Skipping {container_name} (use --all to stop it)")
                skipped_count += 1
                continue

            # Stop containers that match ktest patterns or are named lustre-ci
            # This includes containers with names like "ktest_*" or using ktest images
            try:
                image_tags = container.image.attrs.get("RepoTags", [])
                is_ktest = (
                    container_name.startswith("ktest_")
                    or container_name == "lustre-ci"
                    or any(
                        img_prefix in tag
                        for tag in image_tags
                        for img_prefix in image_prefixes
                    )
                )

                if is_ktest:
                    print(f"Stopping {container_name}...")
                    container.stop(timeout=10)
                    stopped_count += 1
            except Exception as e:
                print(f"Warning: Failed to stop {container_name}: {e}")

        print()
        print(f"Stopped {stopped_count} container(s)")
        if skipped_count > 0:
            print(f"Skipped {skipped_count} container(s)")

        return 0
