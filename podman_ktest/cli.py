# SPDX-License-Identifier: GPL-2.0-only

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
import textwrap
from pathlib import Path

from .commands import (
    cmd_build,
    cmd_deploy,
    cmd_info,
    cmd_job,
    cmd_patch_status,
    cmd_run,
    cmd_setup,
    cmd_stop,
)
from .commands.patch_status import BRANCH, GERRIT, PROJECT
from .utils import get_ktest_dirs, get_git_hash, is_on_lustre, TeeWriter
from .validation import valid_env


class _SubParsersAction(argparse._SubParsersAction):
    """Reuse a subcommand's one-line help as its own description."""

    def add_parser(self, name, **kwargs):
        kwargs.setdefault("description", kwargs.get("help"))
        return super().add_parser(name, **kwargs)


class PodmanHelpParser(argparse.ArgumentParser):
    """ArgumentParser with podman-style help output."""

    HELP_WIDTH = 100

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.register("action", "parsers", _SubParsersAction)

    def _subparser_action(self):
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        return None

    def _usage_line(self):
        usage = f"  {self.prog} [options]"
        if self._subparser_action():
            return usage + " COMMAND [ARG...]"
        for action in self._actions:
            if action.option_strings:
                continue
            metavar = (action.metavar or action.dest).upper()
            if action.nargs in (argparse.REMAINDER, "*"):
                usage += f" [{metavar}...]"
            elif action.nargs == "+":
                usage += f" {metavar} [{metavar}...]"
            else:
                usage += f" {metavar}"
        return usage

    def _format_entries(self, entries):
        """Render (name, help) pairs as aligned, wrapped lines."""
        col = max(len(name) for name, _ in entries) + 3
        lines = []
        for name, help_text in entries:
            wrapped = textwrap.wrap(help_text or "", self.HELP_WIDTH - 2 - col) or [""]
            lines.append(f"  {name:<{col}}{wrapped[0]}".rstrip())
            lines.extend(f"  {'':<{col}}{extra}" for extra in wrapped[1:])
        return lines

    def format_usage(self):
        return f"Usage:\n{self._usage_line()}\n"

    def error(self, message):
        self.print_help(sys.stderr)
        self.exit(2, f"\nError: {message}\n")

    def format_help(self):
        lines = []
        if self.description:
            lines += [self.description, ""]
        lines += ["Usage:", self._usage_line(), ""]

        subparsers = self._subparser_action()
        if subparsers:
            lines.append("Available Commands:")
            lines += self._format_entries(
                [(c.metavar or c.dest, c.help) for c in subparsers._choices_actions]
            )
            lines.append("")

        positionals = [
            a
            for a in self._actions
            if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
        ]
        if positionals:
            lines.append("Arguments:")
            lines += self._format_entries(
                [((a.metavar or a.dest).upper(), a.help) for a in positionals]
            )
            lines.append("")

        options = [a for a in self._actions if a.option_strings]
        if options:
            lines.append("Options:")
            entries = []
            for action in options:
                name = ", ".join(action.option_strings)
                if action.nargs != 0:
                    name += " string"
                entries.append((name, action.help))
            lines += self._format_entries(entries)
            lines.append("")

        return "\n".join(lines)


def main():
    """Main entry point for pk CLI."""
    # Determine the ktest directory (where the package is installed)
    # This is typically the parent of the podman_ktest package
    ktest_dir = Path(__file__).resolve().parent.parent

    parser = PodmanHelpParser(
        prog="pk", description="Run generic virtual machine tests"
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
    build_parser.add_argument(
        "--local-only",
        action="store_true",
        help="Only build ktest-runner container (this is the default)",
    )
    build_parser.add_argument(
        "--all",
        action="store_true",
        help="Build all container images (default: only ktest-runner)",
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
    job_parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Do not remove containers after jobs complete (useful for debugging)",
    )
    job_parser.add_argument(
        "--plugin-args",
        default=None,
        help="Extra CLI options forwarded to the CC plugin (mainline_ccplugin jobs)",
    )
    job_parser.add_argument(
        "--custom-llvm",
        action="store_true",
        help="Mount the LLVM source tree into build containers so kernel "
        "builds use a custom toolchain instead of the packaged clang",
    )

    # Patch-status command - scrape open Gerrit changes
    patch_status_parser = subparsers.add_parser(
        "patch-status",
        help="Show open Gerrit changes in the spirit of the Whamcloud Patch Status page",
    )
    patch_status_parser.add_argument(
        "--gerrit",
        default=GERRIT,
        help=f"Gerrit URL (default: {GERRIT})",
    )
    patch_status_parser.add_argument(
        "--project",
        default=PROJECT,
        help=f"Gerrit project (default: {PROJECT})",
    )
    patch_status_parser.add_argument(
        "--branch",
        default=BRANCH,
        help=f"Target branch (default: {BRANCH})",
    )
    patch_status_parser.add_argument(
        "--author",
        action="append",
        default=[],
        help="Only show changes by this author (name, username, or email; "
        "repeatable, or a comma-separated list); filtered views skip "
        "writing gerrit_changes.json",
    )
    patch_status_parser.add_argument(
        "--output",
        default="/tmp/ktest-results",
        help="Results directory to write gerrit_changes.json for the "
        "static site (default: /tmp/ktest-results)",
    )
    patch_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON to stdout instead of a text table",
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

        # Set up execution log to capture pk's own output
        execution_log_path = results_dir / f"{git_hash}_execution.log"
        execution_log_file = open(execution_log_path, "w")
        sys.stdout = TeeWriter(original_stdout, execution_log_file)
        sys.stderr = TeeWriter(original_stderr, execution_log_file)

    try:
        # Keep stdout clean for patch-status, which may stream JSON
        if args.cmd != "patch-status":
            print(f"CLI: {' '.join(sys.argv)}")
            print()

        # Setup command doesn't need dirs
        if args.cmd == "setup":
            result = cmd_setup(args)
            sys.exit(result)
        elif args.cmd == "patch-status":
            result = cmd_patch_status(args, str(ktest_dir))
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

        # Auto-enable tarball input when source directories are on Lustre,
        # since Lustre does not support the overlay filesystem used for mounts.
        if not args.tarball_input:
            check_path = dirs.get("ktest_lustre_source") or dirs.get(
                "ktest_kernel_source"
            )
            if check_path and is_on_lustre(check_path):
                print("Lustre filesystem detected: defaulting to tarball input mode")
                args.tarball_input = True

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
