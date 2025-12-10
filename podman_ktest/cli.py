# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import argparse
import os
import sys
from pathlib import Path

from .commands import (
    cmd_build,
    cmd_deploy,
    cmd_info,
    cmd_job,
    cmd_run,
    cmd_setup,
    cmd_stop,
)
from .utils import get_ktest_dirs, get_git_hash, TeeWriter
from .validation import valid_env


def main():
    """Main entry point for podman-ktest CLI."""
    # Determine the ktest directory (where the package is installed)
    # This is typically the parent of the podman_ktest package
    ktest_dir = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(
        description="podman-ktest: Run generic virtual machine tests"
    )
    parser.add_argument(
        "--podman-socket",
        default=None,
        help=f"Podman socket URL (default: unix:///run/user/{os.getuid()}/podman/podman.sock)",
    )
    parser.add_argument(
        "--shared-filesystem",
        default=None,
        help="Path to shared filesystem for ccache (default: ~/.cache/ktest). If a path like /tmp is provided, /tmp/ktest/ccache will be used",
    )
    parser.add_argument(
        "--tarball-input",
        action="store_true",
        help="Use tarballs for source input (default: use overlay mounts)",
    )
    subparsers = parser.add_subparsers(dest="cmd", help="Command to run", required=True)

    # Commands
    subparsers.add_parser("info", help="Display podman info")
    subparsers.add_parser("setup", help="Interactive setup to create ~/.ktestrc")

    # Build command
    build_parser = subparsers.add_parser("build", help="Build container images")
    build_parser.add_argument(
        "--ci-only",
        action="store_true",
        help="Only build ktest-runner and ci-lustre containers",
    )

    # Stop command - stop running containers
    stop_parser = subparsers.add_parser(
        "stop", help="Stop all running ktest-related containers"
    )
    stop_parser.add_argument(
        "--all",
        action="store_true",
        help="Also stop the lustre-ci container (default: skip it)",
    )

    # Run command - accepts all remaining arguments
    run_parser = subparsers.add_parser("run", help="Run ktest in container")
    run_parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="Command to run in container"
    )

    # Job command - run one or more jobs from JSON files
    job_parser = subparsers.add_parser(
        "job", help="Run one or more jobs from JSON files"
    )
    job_parser.add_argument(
        "job_names", nargs="+", help="Name(s) of job(s) (without .json extension)"
    )
    job_parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print output to stdout instead of log files (even for multiple jobs)",
    )
    job_parser.add_argument(
        "--output",
        default=None,
        help="Output directory for test results and metadata_store.json",
    )
    job_parser.add_argument(
        "--git-hash",
        default=None,
        help="Git hash to use as key in metadata_store.json",
    )
    job_parser.add_argument(
        "--change-id",
        default=None,
        help="Change ID prefix for log file names (e.g., 'changeid_hash')",
    )
    job_parser.add_argument(
        "--subject",
        default=None,
        help="Commit subject for metadata_store.json",
    )

    # Deploy command - deploy the CI container
    deploy_parser = subparsers.add_parser(
        "deploy", help="Deploy the Lustre CI container"
    )
    deploy_parser.add_argument(
        "--hosting",
        choices=["nginx", "github-pages"],
        default="nginx",
        help="Hosting mode: nginx (serve from container) or github-pages (push to GitHub)",
    )
    deploy_parser.add_argument(
        "--gerrit-auth",
        required=True,
        help="Path to Gerrit authentication JSON file",
    )
    deploy_parser.add_argument(
        "--github-token",
        help="GitHub personal access token for github-pages mode (required for github-pages hosting)",
    )
    deploy_parser.add_argument(
        "--ci-container-socket",
        default=None,
        help="Podman socket path to use inside the CI container (default: same as --podman-socket)",
    )

    args = parser.parse_args()

    # Set up execution logging early for job command to capture ALL output
    execution_log_file = None
    execution_log_path = None
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    if args.cmd == "job":
        # Get directory configuration early for git hash
        dirs = get_ktest_dirs(ktest_dir)

        # Determine output directory and git hash for log naming
        output_dir = args.output if args.output else "/tmp/ktest-results"
        results_dir = Path(output_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        git_hash = args.git_hash
        if not git_hash:
            git_hash = get_git_hash(dirs["ktest_lustre_source"])

        # Set up execution log to capture podman-ktest's own output
        execution_log_path = results_dir / f"{git_hash}_execution.log"
        execution_log_file = open(execution_log_path, "w")
        sys.stdout = TeeWriter(original_stdout, execution_log_file)
        sys.stderr = TeeWriter(original_stderr, execution_log_file)

    try:
        print(f"CLI: {' '.join(sys.argv)}")
        print()

        # Setup command doesn't need dirs
        if args.cmd == "setup":
            result = cmd_setup(args)
            sys.exit(result)
        elif args.cmd == "build":
            result = cmd_build(args, str(ktest_dir), args.podman_socket)
            sys.exit(result)
        elif args.cmd == "info":
            result = cmd_info(args, args.podman_socket)
            sys.exit(result)
        elif args.cmd == "deploy":
            result = cmd_deploy(args, args.podman_socket)
            sys.exit(result)
        elif args.cmd == "stop":
            result = cmd_stop(args, args.podman_socket)
            sys.exit(result)

        # Get directory configuration (may already be loaded for job command)
        if args.cmd != "job" or args.stdout:
            dirs = get_ktest_dirs(ktest_dir)

        # Validate environment before running jobs
        if not valid_env(args.podman_socket, args.shared_filesystem):
            sys.exit(1)

        # Execute the command with appropriate parameters
        if args.cmd == "job":
            result = cmd_job(
                args,
                dirs,
                args.podman_socket,
                args.shared_filesystem,
                execution_log_path,
            )
        elif args.cmd == "run":
            result = cmd_run(
                args,
                dirs,
                args.podman_socket,
                args.shared_filesystem,
                args.tarball_input,
            )
        else:
            print(f"Unknown command: {args.cmd}")
            sys.exit(1)

        sys.exit(result)
    finally:
        # Close execution log if it was opened
        if execution_log_file:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            execution_log_file.close()


if __name__ == "__main__":
    main()
