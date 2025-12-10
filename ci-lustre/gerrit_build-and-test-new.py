#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

#
# Gerrit Universal Reviewer Daemon
# ~~~~~~ ~~~~~~~~~~ ~~~~~~~~ ~~~~~~
#
# * Watch for new change revisions in a gerrit instance.
# * Pass new revisions through builders and testers
# * POST reviews back to gerrit based on results
#
# Copyright (c) 2014, Intel Corporation.
#
# Copyright (c) 2025, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#
# Author: John L. Hammond <john.hammond@intel.com>
# Modified for ktest: Timothy Day <timday@amazon.com>
#

import logging
import json
import os
import sys
import requests
import time
import getpass
import urllib.request, urllib.parse, urllib.error
import threading
import random
from datetime import datetime
import dateutil.parser
import shutil
import subprocess
import resource
from pathlib import Path
from threading import Lock

GERRIT_HOST = os.getenv("GERRIT_HOST", "review.whamcloud.com")
GERRIT_PROJECT = os.getenv("GERRIT_PROJECT", "fs/lustre-release")
GERRIT_BRANCH = ["master"]
GERRIT_USERNAME = os.getenv("GERRIT_USERNAME")
GERRIT_PASSWORD = os.getenv("GERRIT_PASSWORD")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/var/www/ci-lustre/upstream-patch-review")
KTEST_DIR = "/home/ktest/ktest"
LUSTRE_SOURCE = "/home/ktest/git/lustre-release"
HOSTING_MODE = os.getenv("HOSTING_MODE", "github-pages")
IGNORE_OLDER_THAN_DAYS = 60
BRANCHES = [
    {
        "Branch": "master",
        "Subject": "Main development branch",
    },
    {
        "Branch": "master-next",
        "Subject": "Staging branch for new changes",
    },
]


class MetadataStore:
    """
    Key-value store for tracking patch review metadata.
    Stores metadata in a JSON file in OUTPUT_DIR.
    Replaces xattr-based storage.
    """

    def __init__(self, store_path):
        self.store_path = store_path
        self.lock = Lock()
        self.data = {}
        self._load()

    def _load(self):
        """Load metadata from JSON file"""
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, "r") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logging.warning(f"Failed to load metadata store: {e}, starting fresh")
                self.data = {}
        else:
            self.data = {}

    def _save(self):
        """Save metadata to JSON file"""
        try:
            # Write to temporary file first, then rename for atomicity
            temp_path = self.store_path + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(self.data, f, indent=2)
            os.chmod(temp_path, 0o644)  # Make readable by nginx
            os.replace(temp_path, self.store_path)
        except IOError as e:
            logging.error(f"Failed to save metadata store: {e}")

    def set(self, git_hash, key, value):
        """Set metadata for a git hash"""
        with self.lock:
            if git_hash not in self.data:
                self.data[git_hash] = {}
            self.data[git_hash][key] = value
            self._save()

    def get(self, git_hash, key, default=None):
        """Get metadata for a git hash"""
        with self.lock:
            if git_hash in self.data:
                return self.data[git_hash].get(key, default)
            return default

    def list_hashes(self):
        """List all git hashes with metadata"""
        with self.lock:
            return list(self.data.keys())


def _now():
    """_"""
    return int(time.time())


def get_branch_head_hash(branch_name, repo_path=LUSTRE_SOURCE):
    """
    Get the latest commit hash for a given branch.

    Args:
        branch_name: Name of the branch (e.g., 'master', 'master-next')
        repo_path: Path to the git repository

    Returns:
        The commit hash as a string, or None if the fetch fails
    """
    try:
        # Fetch the latest from remote
        fetch_cmd = f"cd {repo_path} && git fetch origin {branch_name}"
        subprocess.run(fetch_cmd, shell=True, check=True, capture_output=True)

        # Get the hash of the remote branch
        rev_parse_cmd = f"cd {repo_path} && git rev-parse origin/{branch_name}"
        result = subprocess.run(
            rev_parse_cmd, shell=True, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to get branch head for {branch_name}: {e}")
        return None


def make_change_from_hash(githash, subject, branch):
    change = {
        "branch": branch,
        "_number": str(random.randint(1, 10000000)),
        "branchwide": True,
        "id": branch,
        "subject": subject,
        "current_revision": githash,
    }

    return change


class Reviewer(object):
    """
    * Poll gerrit instance for updates to changes matching project and branch.
    * Pipe new patches through checkpatch.
    * Convert checkpatch output to gerrit ReviewInput().
    * Post ReviewInput() to gerrit instance.
    * Track reviewed revisions in metadata_store.json.
    """

    def __init__(self, host, project, branch, username, password):
        self.host = host
        self.project = project
        self.branch = branch
        self.auth = requests.auth.HTTPBasicAuth(username, password)
        self.logger = logging.getLogger(__name__)
        self.history = {}
        self.timestamp = 0
        self.request_timeout = 30
        self.post_interval = 30
        self.update_interval = 30
        self.push_interval = 240
        self.last_push = 0
        self.branch_review_interval = 60 * 60 * 12
        self.last_branch_review = 0
        # Initialize metadata store
        metadata_store_path = os.path.join(OUTPUT_DIR, "metadata_store.json")
        self.metadata_store = MetadataStore(metadata_store_path)

    def _debug(self, msg, *args):
        """_"""
        self.logger.debug(msg, *args)

    def _error(self, msg, *args):
        """_"""
        self.logger.error(msg, *args)

    def _url(self, path):
        """_"""
        return "https://" + self.host + "/a" + path

    def _get(self, path):
        """
        GET path return Response.
        """
        url = self._url(path)
        try:
            res = requests.get(url, auth=self.auth, timeout=self.request_timeout)
        except Exception as exc:
            self._error("cannot GET '%s': exception = %s", url, str(exc))
            return None

        if res.status_code != requests.codes.ok:
            self._error(
                "cannot GET '%s': reason = %s, status_code = %d",
                url,
                res.reason,
                res.status_code,
            )
            return None

        return res

    def load_history(self):
        """
        Load review history from metadata_store.json.
        The metadata store contains all the information needed to reconstruct
        the review history.
        """
        # Derive history from metadata_store
        for git_hash in self.metadata_store.list_hashes():
            change_id = self.metadata_store.get(git_hash, "change_id")
            time_stamp = self.metadata_store.get(git_hash, "time_stamp")

            if change_id and git_hash:
                # Add to in-memory history
                self.history[change_id + " " + git_hash] = "0"

                # Update timestamp to the most recent review
                if time_stamp:
                    try:
                        ts = int(time_stamp)
                        if ts > self.timestamp:
                            self.timestamp = ts
                    except (ValueError, TypeError):
                        pass

        self._debug(
            "load_history: history size = %d, timestamp = %d",
            len(self.history),
            self.timestamp,
        )

    def write_history(self, change_id, revision, score):
        """
        Add review record to in-memory history dict.
        All persistent data is stored in metadata_store.json.
        """
        if change_id != "-":
            self.history[change_id + " " + revision] = score

    def in_history(self, change_id, revision):
        """
        Return True if change_id/revision was already reviewed.
        """
        return change_id + " " + revision in self.history

    def get_changes(self, query, Absolute=False):
        """
        GET a list of ChangeInfo()s for all changes matching query.

        {'status':'open', '-age':'60m'} =>
          GET /changes/?q=project:...+status:open+-age:60m&o=CURRENT_REVISION =>
            [ChangeInfo()...]
        """
        query = dict(query)
        branches = ""
        if not Absolute:
            project = query.get("project", self.project)
            query["project"] = urllib.parse.quote(project, safe="")
            if isinstance(self.branch, list):
                b2 = []
                for tmp in self.branch:
                    b2.append("branch:" + urllib.parse.quote(tmp, safe=""))
                branches = "(" + "+OR+".join(b2) + ")+"
            else:
                branch = query.get("branch", self.branch)
                query["branch"] = urllib.parse.quote(branch, safe="")

        path = (
            "/changes/?q="
            + branches
            + "+".join(k + ":" + v for k, v in query.items())
            + "&o=CURRENT_REVISION&o=CURRENT_COMMIT&o=CURRENT_FILES"
        )
        res = self._get(path)
        if not res:
            return []

        # Gerrit uses " )]}'" to guard against XSSI.
        return json.loads(res.content[5:])

    def generate_log_page(
        self, output, page_path, logs, name, git_hash, rc, enforced, time, description
    ):
        # Write log to .log file instead of HTML
        log_path = page_path.replace(".html", ".log")
        out_path = os.path.join(output, log_path)

        with open(out_path, "w") as outfile:
            outfile.write(logs)
        os.chmod(out_path, 0o644)  # Make readable by nginx

        self.metadata_store.set(git_hash, "result" + name, str(rc))
        self.metadata_store.set(git_hash, "enforced" + name, str(enforced))
        self.metadata_store.set(git_hash, "runtime" + name, str(time))
        self.metadata_store.set(git_hash, "description" + name, description)

    @staticmethod
    def run_script(command, timeout_seconds=500):
        start_time = time.time()

        try:
            pipe = subprocess.run(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
                timeout=timeout_seconds,
            )

            out = pipe.stdout
            returncode = pipe.returncode
        except subprocess.TimeoutExpired as e:
            out = e.stdout
            returncode = -1

        elapsed_time = int(time.time() - start_time)
        out = out.decode("utf8", errors="strict").strip()

        return out, returncode, elapsed_time

    def parse_ktest_results(self, change_id, git_hash):
        """Parse test results from /tmp/ktest-results directory"""
        results_dir = Path("/tmp/ktest-results")

        if not results_dir.exists():
            self._error("ktest-results directory does not exist")
            return

        # Get all JSON result files
        json_files = sorted(results_dir.glob("*.json"))

        for json_file in json_files:
            try:
                with open(json_file, "r") as f:
                    result_data = json.load(f)

                job_name = result_data.get("job_name", json_file.stem)
                return_code = result_data.get("return_code", -1)
                runtime = result_data.get("runtime_seconds", 0)
                optional = result_data.get("optional", False)
                description = result_data.get("description", f"Job: {job_name}")

                # Read corresponding log file
                log_file = json_file.with_suffix(".log")
                log_content = ""
                if log_file.exists():
                    with open(log_file, "r", errors="replace") as f:
                        log_content = f.read()

                # Generate log page
                page_path = change_id + "_" + job_name.replace(" ", "_") + ".html"
                self.generate_log_page(
                    OUTPUT_DIR,
                    page_path,
                    log_content,
                    job_name,
                    git_hash,
                    return_code,
                    not optional,  # enforced is the opposite of optional
                    runtime,
                    description,
                )

            except Exception as e:
                self._error(f"Error parsing result file {json_file}: {e}")
                continue

    def run_tests(self, branchwide, change_id, git_hash):
        # Build podman-ktest command with socket parameter if specified
        socket_arg = "--podman-socket unix:///run/podman/podman.sock"
        job_name = "lustre-ci-branch" if branchwide else "lustre-ci"
        command = f"cd {KTEST_DIR} && ./podman-ktest {socket_arg} --shared-filesystem /tmp job --tarball-input {job_name}"

        log_str, rc, elapsed_time = Reviewer.run_script(command, timeout_seconds=800)

        # Generate a row for the podman-ktest job invocation itself
        self.generate_log_page(
            OUTPUT_DIR,
            change_id + "_ci_cli.html",
            log_str if log_str else "",
            "CI CLI",
            git_hash,
            rc,
            True,
            elapsed_time,
            "Run podman-ktest",
        )

        # Parse individual test results from ktest-results
        self.parse_ktest_results(change_id, git_hash)

    def checkout_patch(self, change):
        revision = change.get("current_revision")

        try:
            ref = change["revisions"][revision]["ref"]
        except:
            ref = change["id"]

        command = (
            f"cd {LUSTRE_SOURCE} ; git fetch https://"
            + GERRIT_HOST
            + "/"
            + GERRIT_PROJECT
            + " "
            + ref
            + " && git checkout FETCH_HEAD"
        )
        return Reviewer.run_script(command)

    def analyze_patch(self, change):
        runtime = _now()

        revision = change.get("current_revision")

        try:
            raw_change_id = change["id"].split("~", 1)[1]
        except:
            raw_change_id = change["id"]

        change_id = raw_change_id + "_" + revision
        subject = change.get("subject", "")
        subject = subject[:90]

        try:
            commit_message = change["revisions"][str(revision)]["commit"]["message"]
        except:
            commit_message = ""

        # Generate log for patch (commit message)
        self.generate_log_page(
            OUTPUT_DIR,
            change_id + "_patch.html",
            commit_message,
            "Patch",
            revision,
            0,
            True,
            0,
            "Fetch patch commit message",
        )

        self.checkout_patch(change)

        # Run tests and generate logs
        self.run_tests(change.get("branchwide", False), change_id, revision)

        runtime = _now() - runtime

        # Store metadata for this test run indexed by git hash (revision)
        self.metadata_store.set(revision, "patch_revision", revision)
        self.metadata_store.set(revision, "change_id", raw_change_id)
        self.metadata_store.set(revision, "subject", subject)
        self.metadata_store.set(revision, "time_stamp", str(int(time.time())))
        self.metadata_store.set(revision, "total_runtime", str(runtime))

        copy_static_site(self.metadata_store)

    def change_needs_review(self, change):
        """
        * Bail if the change isn't open (status is not 'NEW').
        * Bail if we've already reviewed the current revision.
        """
        status = change.get("status")
        if status != "NEW":
            self._debug("change_needs_review: status = %s", status)
            return False

        current_revision = change.get("current_revision")
        if not current_revision:
            return False

        # Reject too old ones
        date_created = dateutil.parser.parse(
            change["revisions"][str(current_revision)]["created"]
        )
        if abs(datetime.now() - date_created).days > IGNORE_OLDER_THAN_DAYS:
            self._debug(
                "change_needs_review: current_revision = '%s' created too long ago",
                current_revision,
            )
            return False

        # Have we already checked this revision?
        if self.in_history(change["id"], current_revision):
            return False

        self._debug("change_needs_review: current_revision = '%s'", current_revision)

        return True

    def review_change(self, change):
        user = getpass.getuser()
        pid = os.getpid()
        self._debug(
            f'review_change as {user}/{pid}: change = {change["id"]}, subject = "{change.get("subject", "")}"'
        )

        self.podman_reset()

        current_revision = change.get("current_revision")
        self._debug("change_needs_review: current_revision = '%s'", current_revision)
        if not current_revision:
            return

        try:
            commit_message = change["revisions"][str(current_revision)]["commit"][
                "message"
            ]
        except:
            commit_message = ""

        self._debug("analyze patch")
        self.analyze_patch(change)
        self.write_history(change["id"], current_revision, 0)

    def update(self):
        self.check_for_branches()

        new_timestamp = _now()
        age = 48

        self._debug("start update")

        open_changes = self.get_changes(
            {"status": "open", "-age": str(age) + "h", "-label": "Code-Review=-2"}
        )

        # Sort the list backwards so we get newer changes first.
        # Useful if there's a patchset so we start with the tail end of it
        # to get a quicker reading of the health of the entire thing
        for change in sorted(open_changes, key=lambda x: x["_number"], reverse=True):
            if self.change_needs_review(change):
                self._debug("start review")
                self.review_change(change)
                self._debug("end review")

                copy_static_site(self.metadata_store)

                self.git_commit_and_push()

                # Don't POST more than every post_interval seconds.
                time.sleep(self.post_interval)

        self.git_commit_and_push()

        self._debug("end update")

        self.timestamp = new_timestamp
        self.write_history("-", "-", 0)

    def check_for_branches(self):
        if _now() - self.last_branch_review < self.branch_review_interval:
            return

        for branch in BRANCHES:
            branch_name = branch["Branch"]
            # Get the latest commit hash for this branch
            git_hash = get_branch_head_hash(branch_name)

            if git_hash is None:
                self._error(
                    f"Failed to get git hash for branch {branch_name}, skipping"
                )
                continue

            # Check if we've already reviewed this hash
            if self.in_history(branch_name, git_hash):
                self._debug(f"Branch {branch_name} at {git_hash} already reviewed")
                continue

            self._debug(f"Reviewing branch {branch_name} at commit {git_hash}")
            change = make_change_from_hash(git_hash, branch["Subject"], branch_name)
            self.review_change(change)
            self.write_history(branch_name, git_hash, 0)
            self.git_commit_and_push()

        self.last_branch_review = _now()

        self._debug("successfully reviewed branches")

    def podman_reset(self):
        socket_arg = "--podman-socket unix:///run/podman/podman.sock"
        command = f"cd {KTEST_DIR} && ./podman-ktest {socket_arg} stop"
        subprocess.run(command, shell=True)

    def git_commit_and_push(self):
        """
        Commit and push all uncommitted changes in the given git repository directory.

        Args:
        output_dir (str): Path to the git repository.
        message (str, optional): Commit message. If not provided, a timestamped message is used.
        """
        # Only push to GitHub if in github-pages mode
        if HOSTING_MODE != "github-pages":
            return

        if _now() - self.last_push < self.push_interval:
            return

        message = "Update review status"

        try:
            # Ensure we're in the right directory
            cwd = os.getcwd()
            os.chdir(OUTPUT_DIR)

            # Check if this is a git repo
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                check=True,
                capture_output=True,
            )

            # Stage all changes
            subprocess.run(["git", "add", "-A"], check=True)

            # Check if there’s anything to commit
            diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"])
            if diff_result.returncode == 0:
                self._debug("no changes to commit")
                return

            # Get the list of staged files
            staged_files = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], text=True
            ).splitlines()

            # Skip if the only staged file is status.txt
            skip_only = {"status.txt"}

            if set(staged_files).issubset(skip_only):
                self._debug("only status.txt changed, skipping commit")
                return

            # Commit
            subprocess.run(
                [
                    "git",
                    "commit",
                    "--author",
                    "John Ktest <john@ktest.com>",
                    "-m",
                    message,
                ],
                check=True,
            )

            # Push to origin/main
            subprocess.run(["git", "push", "origin", "main"], check=True)

            self.last_push = _now()

            self._debug("changes committed and pushed successfully")

        except subprocess.CalledProcessError as e:
            self._debug(f"git command failed: {e}")
        finally:
            os.chdir(cwd)

    def run(self):
        if self.timestamp <= 0:
            self.load_history()

        while True:
            self.update()
            time.sleep(self.update_interval)
            copy_static_site(self.metadata_store)


def print_Status_to_HTML():
    command = "cd /home/ktest/ktest ; ./ci-lustre/generate-status"

    log_str, rc, runtime = Reviewer.run_script(command)
    if not log_str:
        log_str = ""

    status_path = os.path.join(OUTPUT_DIR, "status.txt")

    with open(status_path, "w", encoding="utf-8") as f:
        f.write(log_str)
    os.chmod(status_path, 0o644)  # Make readable by nginx

    print(f"Wrote {status_path}")


def cleanup_orphaned_files(metadata_store):
    """
    Delete log files in OUTPUT_DIR that aren't tracked in metadata_store.
    Only .log files are cleaned up; other files (index.html, favicon.ico, etc.) are left alone.
    """
    # Get all log files in OUTPUT_DIR
    try:
        all_log_files = {f for f in os.listdir(OUTPUT_DIR) if f.endswith(".log")}
    except OSError as e:
        logging.error(f"Failed to list OUTPUT_DIR: {e}")
        return

    # Get all tracked files from metadata_store
    tracked_files = set()

    # Collect referenced test result log files from metadata
    for git_hash in metadata_store.list_hashes():
        hash_data = metadata_store.data.get(git_hash, {})
        change_id = hash_data.get("change_id", "")

        if change_id and git_hash:
            # Construct the prefix for this change
            prefix = change_id + "_" + git_hash + "_"

            # Add all log files with this prefix
            for log_file in all_log_files:
                if log_file.startswith(prefix):
                    tracked_files.add(log_file)

    # Determine orphaned files (log files not tracked and not sitewide)
    orphaned_files = all_log_files - tracked_files

    # Delete orphaned files
    deleted_count = 0
    for orphan in orphaned_files:
        orphan_path = os.path.join(OUTPUT_DIR, orphan)
        try:
            os.remove(orphan_path)
            logging.info(f"Deleted orphaned log file: {orphan}")
            deleted_count += 1
        except OSError as e:
            logging.error(f"Failed to delete {orphan}: {e}")

    if deleted_count > 0:
        logging.info(f"Cleanup complete: deleted {deleted_count} orphaned log files")
    else:
        logging.debug("No orphaned log files to delete")


def copy_static_site(metadata_store):
    """
    Copy the static site files to OUTPUT_DIR.
    The static site is a single-page application that reads metadata_store.json
    and .log files to render the test results.
    """
    # Copy all files from static-site directory
    static_site_dir = os.path.join(KTEST_DIR, "ci-lustre/static-site")

    if os.path.exists(static_site_dir):
        for filename in os.listdir(static_site_dir):
            src_path = os.path.join(static_site_dir, filename)
            if os.path.isfile(src_path):
                dest_path = os.path.join(OUTPUT_DIR, filename)
                shutil.copy(src_path, dest_path)
                os.chmod(dest_path, 0o644)  # Make readable by nginx
                print(f"Copied {filename} to {OUTPUT_DIR}")
    else:
        logging.warning(f"Static site directory not found: {static_site_dir}")

    # Generate status.txt
    print_Status_to_HTML()

    # Clean up orphaned log files
    cleanup_orphaned_files(metadata_store)


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_NOFILE, (131072, 131072))
    logging.basicConfig(format="%(asctime)s %(message)s", level=logging.DEBUG)

    # Read credentials from environment variables
    if not GERRIT_USERNAME or not GERRIT_PASSWORD:
        logging.error(
            "Missing Gerrit credentials. Set GERRIT_USERNAME and GERRIT_PASSWORD environment variables"
        )
        sys.exit(1)

    logging.info("Using Gerrit credentials from environment variables")

    reviewer = Reviewer(
        GERRIT_HOST,
        GERRIT_PROJECT,
        GERRIT_BRANCH,
        GERRIT_USERNAME,
        GERRIT_PASSWORD,
    )

    copy_static_site(reviewer.metadata_store)

    try:
        reviewer.run()
    except KeyboardInterrupt:
        copy_static_site(reviewer.metadata_store)
        sys.exit(1)
