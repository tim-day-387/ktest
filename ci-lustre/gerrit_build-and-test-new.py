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
import html as pyhtml
from datetime import datetime
from zoneinfo import ZoneInfo
import dateutil.parser
import shutil
import subprocess
import resource
from pathlib import Path

# GERRIT_AUTH should contain a single JSON dictionary of the form:
# {
#     "review.example.com": {
#         "gerrit/http": {
#             "username": "example-checkpatch",
#             "password": "1234"
#         }
#     }
#     ...
# }

GERRIT_HOST = os.getenv("GERRIT_HOST", "review.whamcloud.com")
GERRIT_PROJECT = os.getenv("GERRIT_PROJECT", "fs/lustre-release")
GERRIT_BRANCH = ["master"]
GERRIT_AUTH_PATH = os.getenv("GERRIT_AUTH_PATH", "GERRIT_AUTH")

OUTPUT_DIR = "/home/timothy/git-big/upstream-patch-review"
STYLESHEET = "styles.css"
REVIEW_HISTORY_PATH = os.getenv("REVIEW_HISTORY_PATH", OUTPUT_DIR + "/REVIEW_HISTORY")
IGNORE_OLDER_THAN_DAYS = 60

page_template = """\
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="icon" href="/favicon.ico">
    <link rel="stylesheet" href="/upstream-patch-review/{style}">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            margin: 0;
            padding: 0;
        }}
        header {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 60px;
            background-color: #333;
            color: white;
            display: flex;
            align-items: center;
            gap: 1em;
            padding: 0 1em;
            box-shadow: 0 2px 5px rgba(0,0,0,0.2);
            z-index: 1000;
        }}
        header img {{
            height: 32px;
            width: 32px;
        }}
        header h1 {{
            font-size: 1.5em;
            margin: 0;
        }}
        header a {{
            color: #fff;
            text-decoration: none;
            margin-left: 1em;
            font-weight: 500;
        }}
        header a:hover {{
            text-decoration: underline;
        }}
        main {{
            margin-top: 80px;
            padding: 0 1em 3em;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 1em;
        }}
        th, td {{
            border: 1px solid #ccc;
            padding: 0.5em;
            text-align: left;
            vertical-align: top;
        }}
        th {{
            background-color: #f2f2f2;
        }}
        tr:nth-child(even) {{
            background-color: #fafafa;
        }}
    </style>
</head>

<body>
<header>
    <img src="/favicon.ico" alt="Site icon">
    <h1>KTEST</h1>
    <a href="/upstream-patch-review/">Home</a>
    <a href="/upstream-patch-review/status.html">Status</a>
    <a href="/">Who am I?</a>
</header>

<main>
{html}
</main>
</body>
</html>
"""
table_template = """\
<caption>{title}</caption>
<table style="margin-bottom: 2em;">
<colgroup>
    <col style="width: 30%;">
    <col style="width: 30%;">
    <col style="width: 15%;">
    <col style="width: 15%;">
    <col style="width: 10%;">
</colgroup>
<thead>
<tr><th>Test</th><th>Description</th><th>Type</th><th>Runtime</th><th>Status</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
"""

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


def make_change_from_hash(githash, subject, branch):
    """Also works for tags and branch names"""

    url = "https://git.whamcloud.com/fs/lustre-release.git/patch/" + githash
    try:
        r = requests.get(url)
        revision = r.text.split(" ", 2)[1]
        changenum = int(revision[:8], 16)
    except requests.exceptions.RequestException:
        revision = githash
        changenum = str(random.randint(1, 10000000))
    except ValueError:  # some garbage from gitweb?
        revision = githash
        # This happens when we have merge commit at the top
        changenum = random.randint(1, 10000000)
    change = {
        "branch": branch,
        "_number": changenum,
        "branchwide": True,
        "id": githash,
        "subject": subject,
        "current_revision": revision,
    }

    return change


class Reviewer(object):
    """
    * Poll gerrit instance for updates to changes matching project and branch.
    * Pipe new patches through checkpatch.
    * Convert checkpatch output to gerrit ReviewInput().
    * Post ReviewInput() to gerrit instance.
    * Track reviewed revisions in history_path.
    """

    def __init__(self, host, project, branch, username, password, history_path):
        self.host = host
        self.project = project
        self.branch = branch
        self.auth = requests.auth.HTTPBasicAuth(username, password)
        self.logger = logging.getLogger(__name__)
        self.history_path = history_path
        self.history_mode = "rw"
        self.history = {}
        self.timestamp = 0
        self.request_timeout = 30
        self.post_enabled = False
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

    def _post(self, path, obj):
        """
        POST json(obj) to path, return True on success.
        """
        url = self._url(path)
        data = json.dumps(obj)
        if not self.post_enabled:
            self._debug("_post: disabled: url = '%s', data = '%s'", url, data)
            return False

        try:
            res = requests.post(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                auth=self.auth,
                timeout=self.request_timeout,
            )
        except Exception as exc:
            self._error("cannot POST '%s': exception = %s", url, str(exc))
            return False

        if res.status_code != requests.codes.ok:
            self._error(
                "cannot POST '%s': reason = %s, status_code = %d",
                url,
                res.reason,
                res.status_code,
            )
            return False

        return True

    def load_history(self):
        """
        Load review history from history_path containing lines of the form:
        EPOCH      FULL_CHANGE_ID                         REVISION    SCORE
        1394536722 fs%2Flustre-release~master~I5cc6c23... 00e2cc75... 1
        1394536721 -                                      -           0
        1394537033 fs%2Flustre-release~master~I10be8e9... 44f7b504... 1
        1394537032 -                                      -           0
        1394537344 -                                      -           0
        ...
        """
        if "r" in self.history_mode:
            Path(self.history_path).touch(exist_ok=True)

            with open(self.history_path) as history_file:
                for line in history_file:
                    epoch, change_id, revision, score = line.split()
                    if change_id == "-":
                        self.timestamp = int(float(epoch))
                    else:
                        self.history[change_id + " " + revision] = score

        self._debug(
            "load_history: history size = %d, timestamp = %d",
            len(self.history),
            self.timestamp,
        )

    def write_history(self, change_id, revision, score, epoch=-1):
        """
        Add review record to history dict and file.
        """
        if change_id != "-":
            self.history[change_id + " " + revision] = score

        if epoch <= 0:
            epoch = self.timestamp

        if "w" in self.history_mode:
            with open(self.history_path, "a") as history_file:
                print(epoch, change_id, revision, score, file=history_file)

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
        self, output, page_path, logs, name, home_path, rc, enforced, time, description
    ):
        template = """\
<html lang="en">
<body>
<pre>
{text}
</pre>
</body>
</html>
"""
        html = template.format(text=logs)
        out_path = os.path.join(output, page_path)

        with open(out_path, "w") as outfile:
            outfile.write(html)

        # I can't fix this any other way...
        subprocess.run(["sed", "-i", "s/\r\r/\r/g", out_path], check=True)

        os.setxattr(home_path, "user.result" + name, str(rc).encode())
        os.setxattr(home_path, "user.enforced" + name, str(enforced).encode())

        if rc == 0:
            color = "green"
            status = "PASS"
        else:
            color = "red"
            status = "FAIL"

        if enforced:
            test_type = "Enforced"
        else:
            test_type = "Optional"

        return f'<tr><td><a href="{page_path}">{name}</a></td><td>{description}</td><td>{test_type}</td><td>{time}</td><td style="color:{color}">{status}</td></tr>'

    @staticmethod
    def run_script(command):
        start_time = time.time()
        timeout_seconds = 500

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

    def parse_ktest_results(self, change_id, home_path):
        """Parse test results from /tmp/ktest-results directory"""
        results_dir = Path("/tmp/ktest-results")
        rows = []

        if not results_dir.exists():
            self._error("ktest-results directory does not exist")
            return []

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
                row = self.generate_log_page(
                    OUTPUT_DIR,
                    page_path,
                    log_content,
                    job_name,
                    home_path,
                    return_code,
                    not optional,  # enforced is the opposite of optional
                    runtime,
                    description,
                )
                rows.append(row)

            except Exception as e:
                self._error(f"Error parsing result file {json_file}: {e}")
                continue

        return rows

    def run_tests(self, branchwide, change_id, home_path):
        rows = ""
        tables = ""

        # Run podman-ktest job lustre-ci at the beginning
        if branchwide:
            command = "cd /home/timothy/ws/ktest && ./podman-ktest job lustre-ci-branch"
        else:
            command = "cd /home/timothy/ws/ktest && ./podman-ktest job lustre-ci"

        timeout_seconds = 800
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
            log_str = pipe.stdout.decode("utf8", errors="strict").strip()
            rc = pipe.returncode
        except subprocess.TimeoutExpired as e:
            log_str = (
                e.stdout.decode("utf8", errors="strict").strip() if e.stdout else ""
            )
            rc = -1

        elapsed_time = int(time.time() - start_time)

        # Generate a row for the podman-ktest job invocation itself
        job_row = self.generate_log_page(
            OUTPUT_DIR,
            change_id + "_podman-ktest-job.html",
            log_str if log_str else "",
            "CI CLI",
            home_path,
            rc,
            False,
            elapsed_time,
            "Run podman-ktest job lustre-ci",
        )

        # Parse individual test results from ktest-results
        test_rows = self.parse_ktest_results(change_id, home_path)

        # Add job summary followed by individual test results
        all_rows = job_row + "\n" + "\n".join(test_rows)
        tables += table_template.format(title="CI Tests", rows=all_rows)

        return tables

    def checkout_patch(self, change):
        revision = change.get("current_revision")

        try:
            ref = change["revisions"][revision]["ref"]
        except:
            ref = change["id"]

        command = (
            "cd /home/timothy/git/lustre-release ; git fetch https://"
            + GERRIT_HOST
            + "/"
            + GERRIT_PROJECT
            + " "
            + ref
            + "&& git checkout FETCH_HEAD"
        )
        return Reviewer.run_script(command)

    def analyze_patch(self, change):
        template = """\
<h1>{title}</h1>
{html}
"""
        runtime = _now()

        revision = change.get("current_revision")

        try:
            raw_change_id = change["id"].split("~", 1)[1]
        except:
            raw_change_id = change["id"]

        change_id = raw_change_id + "_" + revision
        home_path = OUTPUT_DIR + "/" + change_id + "_home.html"
        subject = change.get("subject", "")
        subject = subject[:90]
        rows = ""
        tables = ""

        try:
            commit_message = change["revisions"][str(revision)]["commit"]["message"]
        except:
            commit_message = ""

        open(home_path, "wb").close()

        rows = rows + self.generate_log_page(
            OUTPUT_DIR,
            change_id + "_patch.html",
            commit_message,
            "Patch",
            home_path,
            0,
            True,
            0,
            "Fetch patch commit message",
        )

        self.checkout_patch(change)

        tables += table_template.format(title="Fetch Branch", rows=rows)
        tables += self.run_tests(change.get("branchwide", False), change_id, home_path)

        html_tmp = template.format(title=subject, html=tables)
        html = page_template.format(title=subject, style=STYLESHEET, html=html_tmp)

        with open(home_path, "w") as outfile:
            outfile.write(html)

        runtime = _now() - runtime

        os.setxattr(home_path, "user.patch_revision", revision.encode())
        os.setxattr(home_path, "user.change_id", raw_change_id.encode())
        os.setxattr(home_path, "user.subject", subject.encode())
        os.setxattr(home_path, "user.time_stamp", str(int(time.time())).encode())
        os.setxattr(home_path, "user.total_runtime", str(runtime).encode())

        print_WorkList_to_HTML()

    def post_review(self, change, revision, review_input):
        """
        POST review_input for the given revision of change.

        review_comments.setdefault("/COMMIT_MSG", []).append(
            {
                "line": commitmsg_trivial_lineno(commit_message) + 7,
                "message": SuspiciousTrivialUsage,
            }
        )

        outputdict = {
            "message": (message),
            "labels": {"Code-Review": code_review_score},
            "comments": review_comments,
            "notify": notify,
        }

        reviewer.post_review(
            WorkItem.change, WorkItem.revision, outputdict
        )
        """
        path = "/changes/" + change["id"] + "/revisions/" + revision + "/review"
        self._debug("post_review: path = '%s'", path)
        return self._post(path, review_input)

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
            # self._debug("change_needs_review: already reviewed")
            return False

        self._debug("change_needs_review: current_revision = '%s'", current_revision)

        return True

    def review_change(self, change):
        """
        Review the current revision of change.
        * Pipe the patch through checkpatch(es).
        * Save results to review history.
        * POST review to gerrit.
        """
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

        # TODO: We should post some results to Gerrit...
        # add_review_comment(workItem)

        self.write_history(change["id"], current_revision, 0)

    def update(self):
        """
        GET recently updated changes and review as needed.
        """

        self.check_for_branches()

        new_timestamp = _now()
        age = 48

        self._debug("start update")

        open_changes = self.get_changes(
            {"status": "open", "-age": str(age) + "h", "-label": "Code-Review=-2"}
        )
        # self._debug("update: got %d open_changes", len(open_changes))

        # Sort the list backwards so we get newer changes first.
        # Useful if there's a patchset so we start with the tail end of it
        # to get a quicker reading of the health of the entire thing
        for change in sorted(open_changes, key=lambda x: x["_number"], reverse=True):
            if self.change_needs_review(change):
                self._debug("start review")
                self.review_change(change)
                self._debug("end review")

                print_WorkList_to_HTML()

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
            change = make_change_from_hash(
                branch["Branch"], branch["Subject"], branch["Branch"]
            )
            self.review_change(change)
            self.git_commit_and_push()

        self.last_branch_review = _now()

        self._debug("successfully reviewed branches")

    def podman_reset(self):
        subprocess.run(["podman", "stop", "--all"])
        subprocess.run(["buildah", "rm", "--all"])
        subprocess.run(["podman", "system", "prune", "-f"])

    def git_commit_and_push(self):
        """
        Commit and push all uncommitted changes in the given git repository directory.

        Args:
        output_dir (str): Path to the git repository.
        message (str, optional): Commit message. If not provided, a timestamped message is used.
        """
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

            # Skip if the only staged files are REVIEW_HISTORY or status.html
            skip_only = {"REVIEW_HISTORY", "status.html"}

            if set(staged_files).issubset(skip_only):
                self._debug("only REVIEW_HISTORY/status.html changed, skipping commit")
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
        """
        * Load review history.
        * Call update() every poll_interval seconds.
        """

        if self.timestamp <= 0:
            self.load_history()

        while True:
            self.update()
            time.sleep(self.update_interval)
            print_WorkList_to_HTML()


def print_Status_to_HTML():
    template = """\
<pre>
{text}
</pre>
"""
    command = "cd /home/timothy/ws/ktest ; ./ci-lustre/generate-status"

    log_str, rc, runtime = Reviewer.run_script(command)
    if not log_str:
        log_str = ""

    html_tmp = template.format(text=log_str)
    html = page_template.format(title="Status", style=STYLESHEET, html=html_tmp)
    status_path = os.path.join(OUTPUT_DIR, "status.html")

    with open(status_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {status_path}")


def print_WorkList_to_HTML():
    template = """\
<h1>Testing Status</h1>
<table>
    <colgroup>
        <col style="width: 10%">
        <col style="width: 40%">
        <col style="width: 20%">
        <col style="width: 10%">
        <col style="width: 20%">
    </colgroup>
    <thead>
       <tr>
            <th>Tests</th>
            <th>Subject</th>
            <th>Hash</th>
            <th>Change ID</th>
            <th>Time</th>
            <th>Runtime</th>
            <th>Enforced</th>
            <th>Optional</th>
       </tr>
    </thead>
    <tbody>
        {rows}
    </tbody>
</table>
"""

    shutil.copy(
        "/home/timothy/ws/ktest/ci-lustre/style/styles.css",
        os.path.join(OUTPUT_DIR, STYLESHEET),
    )

    files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith("_home.html")]
    files.sort()

    rows = []

    # Collect (file, timestamp, attrs) tuples first
    file_data = []
    for f in files:
        path = os.path.join(OUTPUT_DIR, f)
        attrs = []

        try:
            patch_revision = os.getxattr(path, "user.patch_revision").decode(
                errors="replace"
            )
            change_id = os.getxattr(path, "user.change_id").decode(errors="replace")
            subject = os.getxattr(path, "user.subject").decode(errors="replace")
            timestamp_raw = os.getxattr(path, "user.time_stamp").decode(
                errors="replace"
            )
            runtime = os.getxattr(path, "user.total_runtime").decode(errors="replace")
        except:
            continue

        # Convert timestamp (string) to float
        try:
            timestamp = float(timestamp_raw)
            readable = datetime.fromtimestamp(
                timestamp, ZoneInfo("America/New_York")
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            timestamp = 0
            readable = "Invalid"

        # --- Collect all result/enforced xattrs ---
        result_attrs = []
        for attr in os.listxattr(path):
            if attr.startswith("user.result"):
                name = attr[len("user.result") :]
                try:
                    rc = int(os.getxattr(path, attr).decode(errors="replace"))
                except Exception:
                    rc = -1
                enforced_attr = f"user.enforced{name}"
                try:
                    enforced_val = os.getxattr(path, enforced_attr).decode(
                        errors="replace"
                    )
                    enforced = enforced_val.strip().lower() == "true"
                except OSError:
                    enforced = False
                result_attrs.append((name, rc, enforced))

        # --- Compute PASS/FAIL for enforced and optional ---
        enforced_results = [rc for _, rc, enforced in result_attrs if enforced]
        optional_results = [rc for _, rc, enforced in result_attrs if not enforced]

        def summarize(results):
            if not results:
                return "N/A", "gray"
            if all(rc == 0 for rc in results):
                return "PASS", "green"
            return "FAIL", "red"

        enforced_summary, enforced_color = summarize(enforced_results)
        optional_summary, optional_color = summarize(optional_results)

        # --- Build attribute columns ---
        attrs.extend(
            [
                subject,
                f'<a href="https://review.whamcloud.com/plugins/gitiles/fs/lustre-release/+/{patch_revision}">{patch_revision}</a>',
                f'<a href="https://review.whamcloud.com/c/fs/lustre-release/+/{change_id}">{change_id}</a>',
                readable,
                runtime,
                f'<span style="color:{enforced_color};">{enforced_summary}</span>',
                f'<span style="color:{optional_color};">{optional_summary}</span>',
            ]
        )

        file_data.append((f, timestamp, attrs))

    # Sort descending by timestamp (newest first)
    file_data.sort(key=lambda x: x[1], reverse=True)

    # Build HTML rows
    for f, timestamp, attrs in file_data:
        attr_html = "".join(f"<td>{v}</td>" for v in attrs)

        try:
            f_html = pyhtml.escape(f)
        except:
            f_html = ""

        rows.append(f'<tr><td><a href="{f_html}">Link</a></td>{attr_html}</tr>')

    # Combine into final HTML
    rows = "\n".join(rows)
    html_tmp = template.format(rows=rows)
    html = page_template.format(title="Testing Status", style=STYLESHEET, html=html_tmp)
    index_path = os.path.join(OUTPUT_DIR, "index.html")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {index_path}")

    print_Status_to_HTML()


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_NOFILE, (131072, 131072))
    logging.basicConfig(format="%(asctime)s %(message)s", level=logging.DEBUG)

    with open(GERRIT_AUTH_PATH) as auth_file:
        auth = json.load(auth_file)
        username = auth[GERRIT_HOST]["gerrit/http"]["username"]
        password = auth[GERRIT_HOST]["gerrit/http"]["password"]

    reviewer = Reviewer(
        GERRIT_HOST,
        GERRIT_PROJECT,
        GERRIT_BRANCH,
        username,
        password,
        REVIEW_HISTORY_PATH,
    )

    print_WorkList_to_HTML()

    try:
        reviewer.run()
    except KeyboardInterrupt:
        print_WorkList_to_HTML()
        sys.exit(1)
