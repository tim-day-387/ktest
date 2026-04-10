# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import IMAGES
from ..utils import get_git_version_info, get_podman_client


def _build_image(dockerfile, tag, name, ktest_dir, podman_socket=None, buildargs=None):
    """Build a single container image."""
    with get_podman_client(podman_socket) as client:
        start_time = time.time()
        print(f"START building {name}")
        client.images.build(
            path=ktest_dir,
            dockerfile=dockerfile,
            tag=tag,
            layers=True,
            outputformat="application/vnd.oci.image.manifest.v1+json",
            rm=False,
            buildargs=buildargs or {},
        )
        runtime = int(time.time() - start_time)
        print(f"END building {name} {runtime}s")
        return name


def cmd_build(args, ktest_dir, podman_socket=None):
    """Build container images in parallel."""
    # Determine which images to build
    if args.ci_only:
        # Only build ktest-runner and ci-lustre
        images_to_build = [
            img for img in IMAGES if img["name"] in ("ktest-runner", "ci-lustre")
        ]
    else:
        images_to_build = IMAGES

    # Fetch git version info from the host repo for the ci-lustre build arg
    git_tag, git_commit = get_git_version_info(ktest_dir)
    ci_buildargs = {"GIT_TAG": git_tag, "GIT_COMMIT": git_commit}

    # Build ktest-runner first since ci-lustre depends on it
    base_images = [img for img in images_to_build if img["name"] == "ktest-runner"]
    dependent_images = [img for img in images_to_build if img["name"] != "ktest-runner"]

    # Build base image(s) first
    for base_image in base_images:
        _build_image(
            base_image["dockerfile"],
            base_image["tag"],
            base_image["name"],
            ktest_dir,
            podman_socket,
        )

    # Now build dependent images in parallel
    if dependent_images:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    _build_image,
                    img["dockerfile"],
                    img["tag"],
                    img["name"],
                    ktest_dir,
                    podman_socket,
                    ci_buildargs if img["name"] == "ci-lustre" else None,
                ): img["name"]
                for img in dependent_images
            }

            for future in as_completed(futures):
                img_name = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"Error building {img_name}: {e}")
                    raise
