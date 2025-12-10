# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..jobs import load_job_file, topological_sort, run_job_config
from ..utils import get_ccache_dir, create_source_tarballs, get_git_hash, get_task_name


class MetadataStore:
    """
    Key-value store for tracking test result metadata.
    Writes metadata to a JSON file compatible with ci-lustre format.
    """

    def __init__(self, store_path):
        self.store_path = store_path
        self.data = {}
        self._load()

    def _load(self):
        """Load metadata from JSON file if it exists"""
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, "r") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.data = {}
        else:
            self.data = {}

    def save(self):
        """Save metadata to JSON file"""
        try:
            temp_path = self.store_path + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(self.data, f, indent=2)
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, self.store_path)
        except IOError as e:
            print(f"Failed to save metadata store: {e}")

    def set(self, git_hash, key, value):
        """Set metadata for a git hash"""
        if git_hash not in self.data:
            self.data[git_hash] = {}
        self.data[git_hash][key] = value

    def get(self, git_hash, key, default=None):
        """Get metadata for a git hash"""
        if git_hash in self.data:
            return self.data[git_hash].get(key, default)
        return default

    def cleanup_orphaned_files(self):
        """
        Delete log files in the output directory that aren't tracked in metadata.
        Only .log files are cleaned up; other files are left alone.
        """
        output_dir = Path(self.store_path).parent

        # Get all log files in output directory
        try:
            all_log_files = {f for f in os.listdir(output_dir) if f.endswith(".log")}
        except OSError as e:
            print(f"Failed to list output directory for cleanup: {e}")
            return

        # Get all tracked files from metadata
        tracked_files = set()

        for git_hash, _ in self.data.items():
            if git_hash:
                # Construct the prefix for this change
                prefix = git_hash + "_"

                # Add all log files with this prefix
                for log_file in all_log_files:
                    if log_file.startswith(prefix):
                        tracked_files.add(log_file)

        # Determine orphaned files
        orphaned_files = all_log_files - tracked_files

        # Delete orphaned files
        deleted_count = 0
        for orphan in orphaned_files:
            orphan_path = output_dir / orphan
            try:
                orphan_path.unlink()
                print(f"Deleted orphaned log file: {orphan}")
                deleted_count += 1
            except OSError as e:
                print(f"Failed to delete {orphan}: {e}")

        if deleted_count > 0:
            print(f"Cleanup complete: deleted {deleted_count} orphaned log files")


def finalize_job_run(
    job_names,
    job_map,
    completed_jobs,
    total_start_time,
    metadata_store,
    git_hash,
    change_id,
    subject,
    execution_log_path,
    enforced_failure,
):
    """Print final summary and save metadata store.

    This function is called in the finally block to ensure output is always produced,
    even if the process is killed or times out.
    """
    # Print final summary
    print()
    print("=" * 60)
    print("Final Results:")
    for job_name in job_names:
        if job_name in completed_jobs:
            return_code, log_path, job_runtime = completed_jobs[job_name]
            status = "PASS" if return_code == 0 else "FAIL"
            log_info = f" (log: {log_path})" if log_path else ""
            print(f"  {job_name}: {status} ({job_runtime}s){log_info}")
        else:
            print(f"  {job_name}: SKIPPED (due to dependency failure)")

    # Print total execution time
    total_runtime = int(time.time() - total_start_time)
    print()
    print(f"Total execution time: {total_runtime}s")

    # Flush stdout to ensure output is captured by parent subprocess
    sys.stdout.flush()

    # Use completed_jobs data to populate metadata_store
    for job_name in job_names:
        if job_name in completed_jobs:
            return_code, _, job_runtime = completed_jobs[job_name]
            job_config = job_map.get(job_name, {})
            optional = job_config.get("optional", False)
            description = job_config.get("description", f"Job: {job_name}")
            metadata_store.set(git_hash, f"result{job_name}", str(return_code))
            metadata_store.set(git_hash, f"enforced{job_name}", str(not optional))
            metadata_store.set(git_hash, f"runtime{job_name}", str(job_runtime))
            metadata_store.set(git_hash, f"description{job_name}", description)

    # Set overall metadata
    metadata_store.set(git_hash, "patch_revision", git_hash)
    metadata_store.set(git_hash, "time_stamp", str(int(time.time())))
    metadata_store.set(git_hash, "total_runtime", str(total_runtime))

    # Extract raw change_id by removing the _{git_hash} suffix
    if change_id:
        raw_change_id = change_id
        if change_id.endswith(f"_{git_hash}"):
            raw_change_id = change_id[: -len(f"_{git_hash}")]
        metadata_store.set(git_hash, "change_id", raw_change_id)

    if subject:
        metadata_store.set(git_hash, "subject", subject)

    all_jobs_ran = len(completed_jobs) == len(job_names)

    # Add execution log as a subtest result (like any other job)
    if execution_log_path:
        # Compute overall return code for execution_log subtest
        execution_return_code = 0 if (not enforced_failure and all_jobs_ran) else 1
        metadata_store.set(git_hash, "resultexecution", str(execution_return_code))
        metadata_store.set(git_hash, "enforcedexecution", "True")
        metadata_store.set(git_hash, "runtimeexecution", str(total_runtime))
        metadata_store.set(
            git_hash, "descriptionexecution", "Execution log for podman-ktest"
        )

    metadata_store.save()

    # Clean up orphaned log files
    metadata_store.cleanup_orphaned_files()

    return all_jobs_ran


def cmd_job(
    args, dirs, podman_socket=None, shared_filesystem=None, execution_log_path=None
):
    """Run one or more jobs from JSON files."""
    # Track total execution time
    total_start_time = time.time()

    # Determine output directory
    output_dir = args.output if args.output else "/tmp/ktest-results"
    results_dir = Path(output_dir)

    # Create results directory if it doesn't exist (don't clean up - may contain existing data)
    results_dir.mkdir(parents=True, exist_ok=True)

    # Initialize metadata store - always emit metadata_store.json
    git_hash = getattr(args, "git_hash", None)
    change_id = getattr(args, "change_id", None)
    subject = getattr(args, "subject", None)

    # Derive git hash from Lustre repo if not provided
    if not git_hash:
        git_hash = get_git_hash(dirs["ktest_lustre_source"])

    metadata_store_path = results_dir / "metadata_store.json"
    metadata_store = MetadataStore(str(metadata_store_path))

    # Resolve ccache directory
    ccache_dir = get_ccache_dir(shared_filesystem)

    # Create source tarballs only if using tarball input mode
    use_tarball_input = args.tarball_input
    tarball_paths = None
    if use_tarball_input:
        tarball_paths = create_source_tarballs(dirs)

    # Ensure tarballs are cleaned up when the function exits
    def cleanup_tarballs(ktest_dirs=None):
        # Clean up source tarballs (only if using tarball input)
        if tarball_paths:
            for tarball in tarball_paths.values():
                try:
                    if Path(tarball).exists():
                        Path(tarball).unlink()
                except Exception as e:
                    print(f"Warning: Failed to delete {tarball}: {e}")

        # Clean up ktest-out tarballs
        if ktest_dirs:
            for ktest_out in ktest_dirs.values():
                ktest_out_tarball = f"{ktest_out}.tar"
                try:
                    if Path(ktest_out_tarball).exists():
                        Path(ktest_out_tarball).unlink()
                except Exception as e:
                    print(f"Warning: Failed to delete {ktest_out_tarball}: {e}")

    job_args = args.job_names
    ktest_dir = dirs["ktest_dir"]

    # Separate single-job names from multi-job file paths
    all_jobs = []

    for arg in job_args:
        jobs = load_job_file(arg, ktest_dir)
        all_jobs.extend(jobs)

    # Check for duplicate job names
    job_names = [job["name"] for job in all_jobs]
    duplicates = [name for name in job_names if job_names.count(name) > 1]
    if duplicates:
        print(f"Error: Duplicate job names found: {', '.join(set(duplicates))}")
        sys.exit(1)

    # Build dependency structures
    job_map = {job["name"]: job for job in all_jobs}

    # Validate dependencies exist
    for job in all_jobs:
        depends_on = job.get("depends_on", [])
        for dep in depends_on:
            if dep not in job_map:
                print(f"Error: Job '{job['name']}' depends on unknown job '{dep}'")
                sys.exit(1)

    # Check for cycles using topological sort (reuse existing validation)
    levels = topological_sort(all_jobs)

    print(f"Executing {len(all_jobs)} job{'s' if len(all_jobs) > 1 else ''}")
    if len(all_jobs) > 1:
        print("Jobs will start as soon as their dependencies are satisfied")
    if use_tarball_input:
        print("Using tarball input mode")
    print()

    pending_jobs = set(job_map.keys())
    running_jobs = set()
    completed_jobs = {}  # job_name -> (return_code, log_path, runtime)
    job_ktest_dirs = {}  # job_name -> ktest_out_dir
    lock = threading.Lock()

    def get_log_path(job_name):
        """Generate log path for a job using output_dir and optional git_hash prefix."""
        if args.stdout:
            return None
        return str(results_dir / f"{git_hash}_{job_name}.log")

    # Build reverse dependency map (which jobs depend on me)
    dependents = defaultdict(list)
    for job in all_jobs:
        for dep in job.get("depends_on", []):
            dependents[dep].append(job["name"])

    def get_ktest_out_for_job(job_name):
        """Get or create ktest_out directory for a job.
        Jobs with dependencies reuse their dependency's ktest_out directory."""
        with lock:
            if job_name in job_ktest_dirs:
                return job_ktest_dirs[job_name]

            job = job_map[job_name]
            deps = job.get("depends_on", [])

            # If job has dependencies, reuse the first dependency's ktest_out
            if deps:
                dep_ktest_out = job_ktest_dirs.get(deps[0])
                if dep_ktest_out:
                    job_ktest_dirs[job_name] = dep_ktest_out
                    return dep_ktest_out

            # Create new temp directory for this job
            ktest_out = tempfile.mkdtemp(prefix=f"ktest_{job_name}_")
            os.chmod(ktest_out, 0o777)  # Allow container user to write
            job_ktest_dirs[job_name] = ktest_out
            return ktest_out

    def can_start_job(job_name):
        """Check if all dependencies for a job are satisfied"""
        job = job_map[job_name]
        deps = job.get("depends_on", [])
        for dep in deps:
            if dep not in completed_jobs:
                return False
            # Check if dependency failed
            if completed_jobs[dep][0] != 0:
                return False
        return True

    def get_ready_jobs():
        """Get list of jobs that can start now"""
        with lock:
            ready = [name for name in pending_jobs if can_start_job(name)]
            return ready

    def mark_job_running(job_name):
        """Mark a job as running"""
        with lock:
            if job_name in pending_jobs:
                pending_jobs.remove(job_name)
                running_jobs.add(job_name)
                return True
            return False

    def mark_job_completed(job_name, return_code, log_path, runtime):
        """Mark a job as completed and return newly ready jobs"""
        with lock:
            if job_name in running_jobs:
                running_jobs.remove(job_name)
            completed_jobs[job_name] = (return_code, log_path, runtime)

            # Find jobs that can now start
            newly_ready = []
            for dependent in dependents.get(job_name, []):
                if dependent in pending_jobs and can_start_job(dependent):
                    newly_ready.append(dependent)

            return newly_ready, return_code != 0

    def get_job_task_name(job):
        """Get the task name for logging based on job type."""
        if job.get("run", False):
            return get_task_name(job, "run")
        elif job.get("build", False):
            return get_task_name(job, "build")
        elif job.get("tool", False):
            return get_task_name(job, "tool")
        return job["name"]

    def submit_job(job_name):
        """Submit a job and print START message."""
        job = job_map[job_name]
        ktest_out = get_ktest_out_for_job(job_name)
        log_path = get_log_path(job_name)
        task_name = get_job_task_name(job)
        print(f"START {task_name}")
        future = executor.submit(
            run_job_config,
            job,
            dirs,
            log_path,
            ktest_out,
            tarball_paths,
            podman_socket,
            use_tarball_input,
            ccache_dir,
        )
        return future

    def print_end_message(task_name, return_code, runtime):
        """Print END message for a completed job."""
        status = "PASS" if return_code == 0 else "FAIL"
        end_msg = f"END {task_name} {status} {runtime}s"
        if return_code != 0:
            end_msg += f" (exit code: {return_code})"
        print(end_msg)

    # Multi-job execution with dynamic scheduling
    max_workers = min(len(all_jobs), 10)  # Reasonable limit on parallelism
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {}  # future -> job_name
    enforced_failure = False  # Track failures in enforced (non-optional) tests

    try:
        # Start initial jobs (those with no dependencies)
        ready_jobs = get_ready_jobs()
        for job_name in ready_jobs:
            if mark_job_running(job_name):
                future = submit_job(job_name)
                futures[future] = job_name

        # Process completed jobs and start new ones
        while futures or pending_jobs:
            if not futures:
                # No jobs running but jobs pending - must be blocked by failures
                break

            # Wait for next job to complete
            done_futures = as_completed(futures)
            for future in done_futures:
                job_name = futures[future]
                del futures[future]

                try:
                    return_code, job_runtime, task_name = future.result()
                    log_path = get_log_path(job_name)
                except Exception as e:
                    print(f"Error running job {job_name}: {e}")
                    return_code = -1
                    job_runtime = 0
                    task_name = get_job_task_name(job_map[job_name])
                    log_path = get_log_path(job_name)

                # Print END message
                print_end_message(task_name, return_code, job_runtime)

                # Mark completed and get newly ready jobs
                newly_ready, failed = mark_job_completed(
                    job_name, return_code, log_path, job_runtime
                )

                # Track failures in enforced (non-optional) tests
                if failed and not job_map[job_name].get("optional", False):
                    enforced_failure = True

                # Start newly ready jobs
                for ready_job_name in newly_ready:
                    if mark_job_running(ready_job_name):
                        future = submit_job(ready_job_name)
                        futures[future] = ready_job_name

                # Only process one completion at a time to immediately start dependencies
                break

    finally:
        executor.shutdown(wait=True)

        # Print summary and save metadata BEFORE cleanup to ensure it always happens
        all_jobs_ran = finalize_job_run(
            job_names,
            job_map,
            completed_jobs,
            total_start_time,
            metadata_store,
            git_hash,
            change_id,
            subject,
            execution_log_path,
            enforced_failure,
        )

        # Now do cleanup of temp directories and tarballs
        for _, ktest_out in job_ktest_dirs.items():
            shutil.rmtree(ktest_out, ignore_errors=True)

        # Clean up tarballs
        cleanup_tarballs(job_ktest_dirs)

    # Return non-zero if:
    # 1. An enforced (non-optional) test failed, OR
    # 2. Not all jobs ran (some were skipped due to dependency failures)
    return 0 if (not enforced_failure and all_jobs_ran) else 1
