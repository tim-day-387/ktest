# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import podman

from .config import CONFIGS
from .models import ContainerJob
from .utils import get_podman_socket, get_task_name


VALID_BACKING_STORAGE = ["wbcfs", "zfs"]


def valid_job_config(job_config):
    """Validate a job configuration."""
    required_fields = ["name", "platform"]

    for field in required_fields:
        if field not in job_config:
            print(f"Error: Missing required field '{field}' in job config")
            return False

    # Validate backing_storage if provided
    backing_storage = job_config.get("backing_storage", "wbcfs")
    if backing_storage not in VALID_BACKING_STORAGE:
        print(
            f"Error: Invalid backing_storage '{backing_storage}'. "
            f"Valid options: {', '.join(VALID_BACKING_STORAGE)}"
        )
        return False

    return True


def load_job_file(job_name, ktest_dir):
    """Load a job file by name.

    Args:
        job_name: Name of the job (without .json extension)
        ktest_dir: Path to the ktest directory

    Returns:
        List of job configurations
    """
    job_path = Path(ktest_dir) / "jobs" / f"{job_name}.json"

    if not job_path.exists():
        print(f"Error: Job file {job_path} does not exist")
        sys.exit(1)

    try:
        with open(job_path, "r") as f:
            job_config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {job_path}: {e}")
        sys.exit(1)

    if "jobs" not in job_config:
        if not valid_job_config(job_config):
            print("Invalid job configuration")
            sys.exit(1)

        return [job_config]

    jobs = job_config["jobs"]
    if not isinstance(jobs, list):
        print(f"Error: 'jobs' must be an array in {job_path}")
        sys.exit(1)

    for _, job in enumerate(jobs):
        if not valid_job_config(job):
            print("Invalid job configuration")
            sys.exit(1)

    return jobs


def topological_sort(jobs):
    """Perform topological sort on jobs based on dependencies.

    Returns a list of job "levels" where each level contains jobs
    that can run in parallel.
    """
    # Build dependency graph
    job_map = {job["name"]: job for job in jobs}

    # Validate dependencies exist
    for job in jobs:
        depends_on = job.get("depends_on", [])
        for dep in depends_on:
            if dep not in job_map:
                print(f"Error: Job '{job['name']}' depends on unknown job '{dep}'")
                sys.exit(1)

    # Track in-degree (number of dependencies) for each job
    in_degree = {job["name"]: len(job.get("depends_on", [])) for job in jobs}

    # Build reverse dependency graph (who depends on me)
    dependents = {job["name"]: [] for job in jobs}
    for job in jobs:
        for dep in job.get("depends_on", []):
            dependents[dep].append(job["name"])

    # Detect cycles using DFS
    visited = set()
    rec_stack = set()

    def has_cycle(node):
        visited.add(node)
        rec_stack.add(node)

        for dependent in dependents.get(node, []):
            if dependent not in visited:
                if has_cycle(dependent):
                    return True
            elif dependent in rec_stack:
                return True

        rec_stack.remove(node)
        return False

    for job_name in job_map:
        if job_name not in visited:
            if has_cycle(job_name):
                print(f"Error: Circular dependency detected in job dependencies")
                sys.exit(1)

    # Perform topological sort by levels
    levels = []
    remaining = set(job_map.keys())

    while remaining:
        # Find all jobs with no remaining dependencies
        ready = [name for name in remaining if in_degree[name] == 0]

        if not ready:
            print(f"Error: Circular dependency detected (no jobs ready to run)")
            sys.exit(1)

        levels.append([job_map[name] for name in ready])

        # Remove ready jobs and update in-degrees
        for name in ready:
            remaining.remove(name)
            for dependent in dependents[name]:
                in_degree[dependent] -= 1

    return levels


def get_build_config(platform):
    """Get build configuration for a platform."""
    if platform not in CONFIGS:
        print(
            f"Error: Unknown platform '{platform}'. Valid platforms: {', '.join(CONFIGS.keys())}"
        )
        sys.exit(1)

    return CONFIGS[platform]


def run_ktest(
    job_config,
    build_config,
    dirs,
    log_path,
    ktest_out_dir=None,
    tarball_paths=None,
    podman_socket=None,
    use_tarball_input=False,
    ccache_dir=None,
):
    """Run a ktest job."""
    task_name = get_task_name(job_config, "run")
    command = build_config["run_script"] + " " + job_config.get("run", "")

    start_time = time.time()
    socket_url = get_podman_socket(podman_socket)
    with podman.PodmanClient(base_url=socket_url) as client:
        job = ContainerJob(
            image=build_config["image"],
            command=["bash", "-c", command],
            working_dir=build_config["working_dir"],
            ktest_out_dir=ktest_out_dir,
            tarball_paths=tarball_paths,
            sync_kernel=False,
            sync_lustre=False,
            sync_ktest_out=True,
            podman_socket=None,
            dirs=dirs,
            use_tarball_input=use_tarball_input,
            ccache_dir=ccache_dir,
            log_path=log_path,
        )
        with job:
            return_code = job.run(client)

    runtime = int(time.time() - start_time)
    return return_code, runtime, task_name


def run_build_lustre(
    job_config,
    build_config,
    dirs,
    log_path,
    ktest_out_dir=None,
    tarball_paths=None,
    podman_socket=None,
    use_tarball_input=False,
    ccache_dir=None,
):
    """Run a Lustre build job."""
    task_name = get_task_name(job_config, "build")

    # Get backing storage (default: wbcfs)
    backing_storage = job_config.get("backing_storage", "wbcfs")

    if job_config["platform"] in ("mainline", "kernel_deb", "kernel_rpm"):
        command = build_config["build_script"] + " " + job_config.get("build", "")
    else:
        command = build_config["build_script"]

    # Set backing storage environment variable for build scripts
    command = f"export BACKING_STORAGE={backing_storage} && {command}"

    start_time = time.time()
    socket_url = get_podman_socket(podman_socket)
    with podman.PodmanClient(base_url=socket_url) as client:
        should_get_archive = (
            job_config["platform"] == "mainline"
            or job_config["platform"] == "native_1"
            or job_config["platform"] == "native_2"
            or job_config["platform"] == "zfs_patch"
            or job_config["platform"] == "kernel_rpm"
        )

        # Use direct mount for RPM/DEB output instead of tarball
        should_mount_output = (
            job_config["platform"] == "kernel_rpm"
            or job_config["platform"] == "kernel_deb"
        )

        # Check if this platform needs ZFS source
        sync_zfs = build_config.get("sync_zfs", False)

        job = ContainerJob(
            image=build_config["image"],
            command=["bash", "-c", command],
            working_dir=build_config["working_dir"],
            ktest_out_dir=ktest_out_dir,
            tarball_paths=tarball_paths,
            sync_kernel=True,
            sync_lustre=True,
            sync_zfs=sync_zfs,
            sync_ktest_out=True,
            podman_socket=None,
            dirs=dirs,
            use_tarball_input=use_tarball_input,
            ccache_dir=ccache_dir,
            log_path=log_path,
            get_ktest_out_archive=should_get_archive,
            mount_ktest_out=should_mount_output,
        )
        with job:
            return_code = job.run(client)

    runtime = int(time.time() - start_time)
    return return_code, runtime, task_name


def run_tool(
    job_config,
    build_config,
    dirs,
    log_path,
    ktest_out_dir=None,
    tarball_paths=None,
    podman_socket=None,
    use_tarball_input=False,
    ccache_dir=None,
):
    """Run a tool job."""
    task_name = get_task_name(job_config, "tool")
    tool_name = job_config["tool"]
    command = "/home/ktest/ktest/tools/" + tool_name

    start_time = time.time()
    socket_url = get_podman_socket(podman_socket)
    with podman.PodmanClient(base_url=socket_url) as client:
        job = ContainerJob(
            image=build_config["image"],
            command=[command],
            working_dir="/home/ktest/git/lustre-release/",
            ktest_out_dir=ktest_out_dir,
            tarball_paths=tarball_paths,
            sync_kernel=True,
            sync_lustre=True,
            sync_ktest_out=True,
            podman_socket=None,
            dirs=dirs,
            use_tarball_input=use_tarball_input,
            ccache_dir=ccache_dir,
            log_path=log_path,
        )
        with job:
            return_code = job.run(client)

    runtime = int(time.time() - start_time)
    return return_code, runtime, task_name


def run_job_config(
    job_config,
    dirs,
    log_path=None,
    ktest_out_dir=None,
    tarball_paths=None,
    podman_socket=None,
    use_tarball_input=False,
    ccache_dir=None,
):
    """Run a job from a job configuration object."""
    platform = job_config["platform"]
    build_config = get_build_config(platform)

    return_code = 0
    runtime = 0
    task_name = job_config["name"]

    if job_config.get("run", False):
        return_code, runtime, task_name = run_ktest(
            job_config,
            build_config,
            dirs,
            log_path,
            ktest_out_dir,
            tarball_paths,
            podman_socket,
            use_tarball_input,
            ccache_dir,
        )
    elif job_config.get("build", False):
        return_code, runtime, task_name = run_build_lustre(
            job_config,
            build_config,
            dirs,
            log_path,
            ktest_out_dir,
            tarball_paths,
            podman_socket,
            use_tarball_input,
            ccache_dir,
        )
    elif job_config.get("tool", False):
        return_code, runtime, task_name = run_tool(
            job_config,
            build_config,
            dirs,
            log_path,
            ktest_out_dir,
            tarball_paths,
            podman_socket,
            use_tarball_input,
            ccache_dir,
        )

    return return_code, runtime, task_name
