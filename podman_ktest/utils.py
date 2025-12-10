# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import os
import subprocess
import threading
import time
from pathlib import Path


# Lock for thread-safe printing from worker threads
_print_lock = threading.Lock()


def _safe_print(msg):
    """Thread-safe print that ensures atomic line output."""
    with _print_lock:
        print(msg)


def get_podman_socket(custom_socket=None):
    """Get podman socket URL, preferring custom socket if provided."""
    if custom_socket:
        return custom_socket
    return f"unix:///run/user/{os.getuid()}/podman/podman.sock"


def put_archive(container, tarball_path, archive_path):
    """Put a tarball archive into a container."""
    start_time = time.time()
    with open(tarball_path, "rb") as f:
        container.put_archive(archive_path, f)
    elapsed = int(time.time() - start_time)
    _safe_print(f"PUT {tarball_path} {elapsed}s")


def get_archive(container, tarball_path, archive_path):
    """Get an archive from a container."""
    archive_start_time = time.time()
    archive_stream, _ = container.get_archive(archive_path)
    with open(tarball_path, "wb") as f:
        for chunk in archive_stream:
            f.write(chunk)
    elapsed = int(time.time() - archive_start_time)
    _safe_print(f"GET {tarball_path} {elapsed}s")


def log_container(log_path, container):
    """Write container logs to file or stdout."""
    if log_path:
        with open(log_path, "wb") as log_file:
            for line in container.logs(stream=True, stdout=True, stderr=True):
                log_file.write(line)

        # I can't fix this any other way...
        subprocess.run(["sed", "-i", "s/\r\r/\r/g", log_path], check=True)
    else:
        for line in container.logs(stream=True, stdout=True, stderr=True):
            print(line.decode("utf-8"), end="")


def get_ccache_dir(shared_filesystem_path=None):
    """Resolve the ccache directory path based on shared filesystem argument.

    Args:
        shared_filesystem_path: Optional path from --shared-filesystem argument

    Returns:
        Path to the ccache directory on the host
    """
    if shared_filesystem_path is None:
        # Default: ~/.cache/ktest/ccache
        ccache_dir = Path.home() / ".cache" / "ktest" / "ccache"
    else:
        # User provided a path - append /ktest/ccache to it
        ccache_dir = Path(shared_filesystem_path) / "ktest" / "ccache"

    return str(ccache_dir.resolve())


def is_in_home(path):
    """Check if a path is within the home directory."""
    home = Path.home().resolve()
    try:
        return home in Path(path).resolve().parents or Path(path).resolve() == home
    except FileNotFoundError:
        # resolve() can fail if path doesn't exist
        return False


def create_source_tarballs(dirs):
    """Create tarballs for kernel, lustre, and optionally zfs sources in /tmp/.

    Returns dict with paths to the created tarballs.
    """
    tarball_paths = {}

    # Create kernel tarball using git archive
    kernel_tarball = "/tmp/ktest-kernel.tar.gz"
    print(f"Creating kernel tarball at {kernel_tarball}...")
    subprocess.run(
        [
            "tar",
            "-czf",
            kernel_tarball,
            "--transform",
            "s,^./,linux/,",
            ".",
        ],
        cwd=dirs["ktest_kernel_source"],
        check=True,
    )
    tarball_paths["kernel"] = kernel_tarball

    # Create lustre tarball using regular tar
    lustre_tarball = "/tmp/ktest-lustre.tar.gz"
    print(f"Creating lustre tarball at {lustre_tarball}...")
    subprocess.run(
        [
            "tar",
            "-czf",
            lustre_tarball,
            "--transform",
            "s,^./,lustre-release/,",
            ".",
        ],
        cwd=dirs["ktest_lustre_source"],
        check=True,
    )
    tarball_paths["lustre"] = lustre_tarball

    # Create ZFS tarball if ZFS source exists
    if dirs.get("ktest_zfs_source") and Path(dirs["ktest_zfs_source"]).exists():
        zfs_tarball = "/tmp/ktest-zfs.tar.gz"
        print(f"Creating ZFS tarball at {zfs_tarball}...")
        subprocess.run(
            [
                "tar",
                "-czf",
                zfs_tarball,
                "--transform",
                "s,^./,zfs/,",
                ".",
            ],
            cwd=dirs["ktest_zfs_source"],
            check=True,
        )
        tarball_paths["zfs"] = zfs_tarball

    return tarball_paths


def load_ktestrc():
    """Load environment variables from ~/.ktestrc if it exists."""
    ktestrc_path = Path.home() / ".ktestrc"
    env_vars = {}

    if ktestrc_path.exists():
        try:
            # Execute the shell script and capture exported variables
            result = os.popen(f"bash -c 'source {ktestrc_path} && env'").read()
            for line in result.strip().split("\n"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key] = value
        except Exception as e:
            print(f"Warning: Failed to load ~/.ktestrc: {e}")

    return env_vars


def get_ktest_dirs(ktest_dir):
    """Get ktest directory paths.

    Args:
        ktest_dir: Path to the ktest directory (where podman-ktest lives)

    Returns:
        Dictionary with ktest_dir, ktest_kernel_source, ktest_lustre_source, ktest_zfs_source
    """
    import sys

    # Load variables from ~/.ktestrc
    ktestrc_vars = load_ktestrc()

    # Get kernel source from environment, ktestrc, or default location
    ktest_kernel_source = os.environ.get("ktest_kernel_source")
    if not ktest_kernel_source:
        ktest_kernel_source = ktestrc_vars.get("ktest_kernel_source")
    if not ktest_kernel_source:
        linux_path = Path.home() / "git" / "linux"
        if linux_path.exists():
            ktest_kernel_source = str(linux_path.resolve())

    if not ktest_kernel_source or not Path(ktest_kernel_source).exists():
        ktestrc_path = Path.home() / ".ktestrc"
        if not ktestrc_path.exists():
            print(f"Error: kernel source directory not found")
            print(f"")
            print(f"Please run 'podman-ktest setup' to configure paths,")
            print(f"or set the ktest_kernel_source environment variable")
        else:
            print(
                f"Error: kernel source directory {ktest_kernel_source} does not exist"
            )
        sys.exit(1)

    ktest_lustre_source = ktestrc_vars.get("ktest_lustre_source")
    if not ktest_lustre_source:
        ktest_lustre_source = str(Path(ktest_kernel_source).parent / "lustre-release")

    # Get ZFS source from environment, ktestrc, or default location
    ktest_zfs_source = os.environ.get("ktest_zfs_source")
    if not ktest_zfs_source:
        ktest_zfs_source = ktestrc_vars.get("ktest_zfs_source")
    if not ktest_zfs_source:
        zfs_path = Path(ktest_kernel_source).parent / "zfs"
        if zfs_path.exists():
            ktest_zfs_source = str(zfs_path.resolve())

    return {
        "ktest_dir": str(ktest_dir),
        "ktest_kernel_source": ktest_kernel_source,
        "ktest_lustre_source": ktest_lustre_source,
        "ktest_zfs_source": ktest_zfs_source,
    }


def get_task_name(job_config, goal):
    """Get the task name for a job."""
    job_name = job_config["name"]
    run_description = job_config.get(goal, "").strip()
    if run_description:
        task_name = f"{job_name} {goal} {run_description}"
    else:
        task_name = f"{job_name} {goal}"

    return task_name


def get_git_hash(repo_path):
    """Get the current git commit hash from a repository.

    Args:
        repo_path: Path to the git repository

    Returns:
        The full git commit hash as a string, or None if not a git repo
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


class TeeWriter:
    """Write to multiple streams simultaneously."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()
