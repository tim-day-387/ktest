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
        Reads the JSON file directly to determine which patches have been reviewed.
        """
        metadata_store_path = os.path.join(OUTPUT_DIR, "metadata_store.json")
        metadata = {}

        if os.path.exists(metadata_store_path):
            try:
                with open(metadata_store_path, "r") as f:
                    metadata = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self._debug(f"Failed to load metadata store: {e}")
                metadata = {}

        # Derive history from metadata
        for git_hash, hash_data in metadata.items():
            change_id = hash_data.get("change_id")
            time_stamp = hash_data.get("time_stamp")

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
            out = e.stdout if e.stdout else b""
            returncode = -1

        elapsed_time = int(time.time() - start_time)
        out = out.decode("utf8", errors="strict").strip()

        if returncode != 0:
            logging.error(f"Command failed with return code {returncode}: {command}")
            if out:
                logging.error(f"Output:\n{out}")

        return out, returncode, elapsed_time

    def run_tests(self, branchwide, change_id, git_hash, subject):
        # Build podman-ktest command with socket parameter and output flags
        # podman-ktest writes directly to OUTPUT_DIR with metadata_store.json
        # Note: change_id already contains git_hash (format: {raw_change_id}_{git_hash})
        socket_arg = "--podman-socket unix:///run/podman/podman.sock"
        job_name = "lustre-ci-branch" if branchwide else "lustre-ci"
        # Escape subject for shell (replace quotes)
        escaped_subject = subject.replace("'", "'\\''") if subject else ""
        output_args = f"--output {OUTPUT_DIR} --git-hash {git_hash} --change-id {change_id} --subject '{escaped_subject}'"
        command = f"cd {KTEST_DIR} && ./podman-ktest {socket_arg} --shared-filesystem /tmp --tarball-input job {job_name} {output_args}"

        Reviewer.run_script(command, timeout_seconds=800)

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
        revision = change.get("current_revision")

        try:
            raw_change_id = change["id"].split("~", 1)[1]
        except:
            raw_change_id = change["id"]

        change_id = raw_change_id + "_" + revision
        subject = change.get("subject", "")
        subject = subject[:90]

        self.checkout_patch(change)
        self.run_tests(change.get("branchwide", False), change_id, revision, subject)

        copy_static_site()

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
        current_revision = change.get("current_revision")
        if not current_revision:
            self._debug("Can't review change with missing revision")
            return

        user = getpass.getuser()
        pid = os.getpid()

        self._debug(
            f'analyze patch as {user}/{pid}: change = {change["id"]}, subject = "{change.get("subject", "")}", current_revision = "{current_revision}"'
        )

        self.podman_reset()
        self.analyze_patch(change)
        self.write_history(change["id"], current_revision, 0)

    def update(self):
        self.check_for_branches()

        new_timestamp = _now()
        age = 48
        open_changes = self.get_changes(
            {"status": "open", "-age": str(age) + "h", "-label": "Code-Review=-2"}
        )

        # Sort the list backwards so we get newer changes first.
        # Useful if there's a patchset so we start with the tail end of it
        # to get a quicker reading of the health of the entire thing
        for change in sorted(open_changes, key=lambda x: x["_number"], reverse=True):
            if self.change_needs_review(change):
                self.review_change(change)

                copy_static_site()

                self.git_commit_and_push()

                # Don't POST more than every post_interval seconds.
                time.sleep(self.post_interval)

        self.git_commit_and_push()
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
            copy_static_site()


def print_Status_to_HTML():
    command = "cd /home/ktest/ktest ; ./ci-lustre/generate-status"

    log_str, _, _ = Reviewer.run_script(command)
    if not log_str:
        log_str = ""

    status_path = os.path.join(OUTPUT_DIR, "status.txt")

    with open(status_path, "w", encoding="utf-8") as f:
        f.write(log_str)
    os.chmod(status_path, 0o644)  # Make readable by nginx

    print(f"Wrote {status_path}")


def copy_static_site():
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

    copy_static_site()

    try:
        reviewer.run()
    except KeyboardInterrupt:
        copy_static_site()
        sys.exit(1)
