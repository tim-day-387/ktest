# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any

import podman

from .utils import put_archive, get_archive, log_container, get_podman_socket

# Serialize container creation to avoid podman user namespace race condition
# when multiple keep-id containers are created concurrently
_create_lock = threading.Lock()

# Timeout for container creation API calls. Needs to absorb the
# first-create chown of the image storage layer under userns=keep-id
# on podman versions without idmapped-mount support (<4.5), which can
# take minutes for multi-GB images.
_CREATE_TIMEOUT = 600


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
    dirs: Optional[Dict[str, str]] = None
    use_tarball_input: bool = False
    ccache_dir: Optional[str] = None
    log_path: Optional[str] = None
    get_ktest_out_archive: bool = False
    mount_ktest_out: bool = False
    package_dir: Optional[str] = None
    is_vm_run: bool = False
    no_cleanup: bool = False

    # State fields (managed internally)
    _started: bool = field(default=False, init=False)
    _container: Any = field(default=None, init=False)

    def _execute(self, client, podman_socket=None) -> int:
        """Internal execution logic - runs container and manages lifecycle."""
        ktest_out_host = self.ktest_out_dir if self.ktest_out_dir else "/tmp/ktest-out"
        ccache_dir = self.ccache_dir if self.ccache_dir else "/tmp/ccache"
        # Bind-mount source for built packages, resolved on the host by podman.
        # On a plain host run, create it here (best effort). Under a shared
        # filesystem podman-ktest runs inside the ci-lustre container, where this
        # mkdir would only touch the container's private /tmp -- the real host
        # dir is created during validation -- so failures are ignored.
        package_dir = self.package_dir if self.package_dir else "/tmp/ktest-packages"
        if self.mount_ktest_out:
            try:
                pkg_path = Path(package_dir)
                pkg_path.mkdir(parents=True, exist_ok=True)
                pkg_path.chmod(0o777)
            except OSError:
                pass

        # Build overlay volume list.
        # userns_mode="keep-id" maps host UID to container UID, so files
        # appear with correct ownership without per-mount idmap options.
        overlay_volumes = []

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
            },
        ]

        # VM runs need the host root images as a backing file for qemu.
        # Read-only is fine: qemu's snapshot=on provides per-run COW.
        if self.is_vm_run:
            mounts.append(
                {
                    "type": "bind",
                    "source": "/var/lib/ktest",
                    "target": "/var/lib/ktest",
                    "read_only": True,
                }
            )

        # Mount output directory for direct output file access (RPMs/DEBs)
        # Use /tmp/ktest-output to avoid conflicting with /tmp/ktest-out tarball extraction
        if self.mount_ktest_out:
            mounts.append(
                {
                    "type": "bind",
                    "source": package_dir,
                    "target": "/tmp/ktest-output",
                    "read_only": False,
                }
            )

        # Create container (but don't start it yet)
        # Remove auto-remove so we can extract archives after completion
        # Serialize creation to avoid podman keep-id user namespace race,
        # and retry on transient socket errors (e.g. timeout under load).
        # Uses a short-timeout client so we detect deadlocks quickly
        # instead of hanging forever.
        command = self.command
        if (
            self.no_cleanup
            and len(command) == 3
            and command[0] == "bash"
            and command[1] == "-c"
        ):
            command = ["bash", "-c", command[2] + "; sleep infinity"]

        create_kwargs = dict(
            image=self.image,
            command=command,
            stdin_open=False,
            devices=["/dev/kvm", "/dev/net/tun"],
            cap_add=["NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE"],
            sysctls={"net.ipv4.ip_forward": "1"},
            userns_mode="keep-id",
            pids_limit=100000,
            overlay_volumes=overlay_volumes,
            mounts=mounts,
            remove=False,
            working_dir=self.working_dir,
        )
        socket_url = get_podman_socket(podman_socket)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with _create_lock:
                    with podman.PodmanClient(
                        base_url=socket_url, timeout=_CREATE_TIMEOUT
                    ) as create_client:
                        container = create_client.containers.create(**create_kwargs)
                        container_id = container.id
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2**attempt)
                else:
                    raise
        # Look up the container via the caller's client (which has no timeout)
        # so that long-running operations like wait() and logs() work normally
        self._container = client.containers.get(container_id)
        container = self._container

        try:
            # Only sync tarballs if using tarball input mode
            if self.use_tarball_input:
                if (
                    self.sync_kernel
                    and self.tarball_paths
                    and "kernel" in self.tarball_paths
                ):
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

            if Path(tarball_path).exists():
                put_archive(container, tarball_path, "/tmp")

            container.start()

            # Stream logs in a background thread so container.wait() always
            # unblocks when the container exits, even if podman's log stream
            # fails to send EOF (a known podman bug on some versions).
            log_thread = threading.Thread(
                target=log_container, args=(self.log_path, container), daemon=True
            )
            log_thread.start()

            return_code = container.wait()

            # Drain any remaining log output (30s is generous; stream should
            # already be at EOF since the container just exited).
            log_thread.join(timeout=30)

            if self.get_ktest_out_archive:
                get_archive(container, tarball_path, "/tmp/ktest-out")

            return return_code

        finally:
            if self.no_cleanup:
                print(f"Skipping container cleanup (--no-cleanup): {container.id}")
            else:
                try:
                    container.stop(timeout=5)
                except:
                    pass
                try:
                    container.remove()
                except:
                    pass

            self._container = None

    def run(self, client, podman_socket=None) -> int:
        """Synchronous execution - runs container and returns exit code."""
        if self._started:
            raise RuntimeError("Job already started")
        self._started = True

        try:
            return_code = self._execute(client, podman_socket=podman_socket)
            return return_code
        except Exception as e:
            raise

    def cleanup(self) -> None:
        """Stop and remove container if still running."""
        container = self._container
        if container is not None:
            if self.no_cleanup:
                print(f"Skipping container cleanup (--no-cleanup): {container.id}")
                return
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
