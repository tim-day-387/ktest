# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any

from .utils import put_archive, get_archive, log_container


@dataclass
class ValidationError:
    """Represents a validation error with details and remediation steps."""

    check_name: str
    message: str
    remediation: Optional[str] = None


@dataclass
class ContainerJob:
    """Container job configuration and execution."""

    # Configuration fields (set at creation)
    image: str
    command: List[str]
    working_dir: str
    ktest_out_dir: Optional[str] = None
    tarball_paths: Optional[Dict[str, str]] = None
    sync_kernel: bool = False
    sync_lustre: bool = False
    sync_zfs: bool = False
    sync_ktest_out: bool = False
    podman_socket: Optional[str] = None
    dirs: Optional[Dict[str, str]] = None
    use_tarball_input: bool = False
    ccache_dir: Optional[str] = None
    log_path: Optional[str] = None
    get_ktest_out_archive: bool = False
    mount_ktest_out: bool = False

    # State fields (managed internally)
    _started: bool = field(default=False, init=False)
    _container: Any = field(default=None, init=False)

    def _execute(self, client) -> int:
        """Internal execution logic - runs container and manages lifecycle."""
        ktest_out_host = self.ktest_out_dir if self.ktest_out_dir else "/tmp/ktest-out"
        ccache_dir = self.ccache_dir if self.ccache_dir else "/tmp/ccache"

        Path("/tmp/ktest-packages").mkdir(exist_ok=True)
        os.chmod("/tmp/ktest-packages", 0o777)

        # Only mount /var/lib/ktest as overlay_volume
        overlay_volumes = [
            {
                "source": "/var/lib/ktest",
                "destination": "/var/lib/ktest",
            },
        ]

        # Add kernel and lustre as overlay volumes if not using tarballs
        if not self.use_tarball_input and self.dirs:
            if self.sync_kernel:
                overlay_volumes.append(
                    {
                        "source": self.dirs["ktest_kernel_source"],
                        "destination": "/home/ktest/git/linux",
                    }
                )
            if self.sync_lustre:
                overlay_volumes.append(
                    {
                        "source": self.dirs["ktest_lustre_source"],
                        "destination": "/home/ktest/git/lustre-release",
                    }
                )
            if self.sync_zfs and self.dirs.get("ktest_zfs_source"):
                overlay_volumes.append(
                    {
                        "source": self.dirs["ktest_zfs_source"],
                        "destination": "/home/ktest/git/zfs",
                    }
                )

        mounts = [
            {
                "type": "bind",
                "source": ccache_dir,
                "target": "/tmp/ccache",
                "read_only": False,
                "options": ["U"],  # chown to match container user
            },
        ]

        # Mount output directory for direct output file access (RPMs/DEBs)
        # Use /tmp/ktest-output to avoid conflicting with /tmp/ktest-out tarball extraction
        if self.mount_ktest_out:
            mounts.append(
                {
                    "type": "bind",
                    "source": "/tmp/ktest-packages",
                    "target": "/tmp/ktest-output",
                    "read_only": False,
                    "options": ["U"],  # chown to match container user
                }
            )

        # Mount podman socket if provided (for CI container to spawn job containers)
        if self.podman_socket:
            socket_path = self.podman_socket.replace("unix://", "")
            mounts.append(
                {
                    "type": "bind",
                    "source": socket_path,
                    "target": socket_path,
                    "read_only": False,
                }
            )

        # Create container (but don't start it yet)
        # Remove auto-remove so we can extract archives after completion
        self._container = client.containers.create(
            image=self.image,
            command=self.command,
            stdin_open=False,
            devices=["/dev/kvm"],
            pids_limit=100000,
            overlay_volumes=overlay_volumes,
            mounts=mounts,
            remove=False,
            working_dir=self.working_dir,
        )
        container = self._container

        try:
            # Only sync tarballs if using tarball input mode
            if self.use_tarball_input:
                if self.sync_kernel and self.tarball_paths:
                    put_archive(
                        container, self.tarball_paths["kernel"], "/home/ktest/git"
                    )
                if self.sync_lustre and self.tarball_paths:
                    put_archive(
                        container, self.tarball_paths["lustre"], "/home/ktest/git"
                    )
                if self.sync_zfs and self.tarball_paths and "zfs" in self.tarball_paths:
                    put_archive(container, self.tarball_paths["zfs"], "/home/ktest/git")

            tarball_path = f"{ktest_out_host}.tar"

            if Path(tarball_path).exists() and self.sync_ktest_out:
                put_archive(container, tarball_path, "/tmp")

            container.start()

            log_container(self.log_path, container)

            return_code = container.wait()

            if self.get_ktest_out_archive:
                get_archive(container, tarball_path, "/tmp/ktest-out")

            return return_code

        finally:
            # Clean up container
            try:
                container.stop(timeout=5)
            except:
                pass
            try:
                container.remove()
            except:
                pass

            self._container = None

    def run(self, client) -> int:
        """Synchronous execution - runs container and returns exit code."""
        if self._started:
            raise RuntimeError("Job already started")
        self._started = True

        try:
            return_code = self._execute(client)
            return return_code
        except Exception as e:
            raise

    def cleanup(self) -> None:
        """Stop and remove container if still running."""
        container = self._container
        if container is not None:
            try:
                container.stop(timeout=5)
            except:
                pass
            try:
                container.remove()
            except:
                pass

    def __enter__(self) -> "ContainerJob":
        return self

    def __exit__(self, _exc_tb, _exc_type, _exc_val) -> bool:
        self.cleanup()
        return False
