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
# Author: John L. Hammond <john.hammond@intel.com>
# Modified for ktest: Timothy Day <timday@amazon.com>
#

import fnmatch
import logging
import json
import os
import sys
import requests
import time
import getpass
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import re
import random
import html as pyhtml
from datetime import datetime
from zoneinfo import ZoneInfo
import dateutil.parser
import shutil
import subprocess
import resource
from enum import Enum
from pathlib import Path

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


GERRIT_HOST = os.getenv("GERRIT_HOST", "review.whamcloud.com")
GERRIT_PROJECT = os.getenv("GERRIT_PROJECT", "fs/lustre-release")
GERRIT_BRANCH = ["master"]
GERRIT_AUTH_PATH = os.getenv("GERRIT_AUTH_PATH", "GERRIT_AUTH")
GERRIT_DRYRUN = os.getenv("GERRIT_DRYRUN", None)

# When this is set - only changes with this topic would be tested.
# good for trial runs before big deployment
GERRIT_FORCETOPIC = os.getenv("GERRIT_FORCETOPIC", None)

FAILED_POSTS_DIR = "failed_posts"
OUTPUT_DIR = "/home/timothy/git-big/upstream-patch-review"
STYLESHEET = "styles.css"

StopOnIdle = False
DrainQueueAndStop = False

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

REVIEW_HISTORY_PATH = os.getenv("REVIEW_HISTORY_PATH", OUTPUT_DIR + "/REVIEW_HISTORY")
# TrivialNagMessage = 'It is recommended to add "Test-Parameters: trivial" directive to patches that do not change any running code to ease the load on the testing subsystem'
TrivialNagMessage = ""  # don't want to promote trivial anymore
TrivialIgnoredMessage = 'Even though "Test-Parameters: trivial" was detected, this deeply suspicious bot still run some testing'
SuspiciousTrivialUsage = "Suspicious test-param trivial usage for general code changes"

USE_CODE_REVIEW_SCORE = False

IGNORE_OLDER_THAN_DAYS = 30

reviewer = None

I_DONT_KNOW_HOW_TO_TEST_THESE = [
    "contrib/lbuild/*",
    "*dkms*",
    "*spec*",
    "debian/*",
    "rpm/*",
    "lustre/kernel_patches/*",
]


# We store all job items here
WorkList = []


def match_fnmatch_list(item, fnlist):
    for pattern in fnlist:
        if fnmatch.fnmatch(item, pattern):
            return True
    return False


def is_notknow_howto_test(filelist):
    """Returns true if there are any changed files that
    are not noops, but we are not testing it"""
    for item in sorted(filelist):
        if match_fnmatch_list(item, I_DONT_KNOW_HOW_TO_TEST_THESE):
            return True
    return False


def commitmsg_trivial_lineno(message):
    trivial_re = re.compile("^Test-Parameters:.*trivial")
    for idx, line in enumerate(message.splitlines()):
        if trivial_re.match(line):
            return idx
    return 0


def is_trivial_requested(message):
    return commitmsg_trivial_lineno(message) > 0


def is_buildonly_requested(message):
    trivial_re = re.compile("^Test-Parameters:.*forbuildonly")
    for line in message.splitlines():
        if trivial_re.match(line):
            return True
    return False


def is_testonly_requested(message):
    trivial_re = re.compile("^Test-Parameters:.*fortestonly")
    for line in message.splitlines():
        if trivial_re.match(line):
            return True
    return False


def add_review_comment(WorkItem):
    """
    Convert { PATH: { LINE: [COMMENT, ...] }, ... }, [11] to a gerrit
    ReviewInput() and score
    """
    score = 0
    review_comments = {}
    try:
        commit_message = WorkItem.change["revisions"][str(WorkItem.revision)]["commit"][
            "message"
        ]
    except:
        commit_message = ""

    if WorkItem.Aborted:
        if WorkItem.AbortDone:
            # We already printed everything in the past, just exit
            return

        WorkItem.AbortDone = True
        if WorkItem.BuildDone:
            # No messages were printed anywhere, pretend we did not even see it
            return

        message = "Newer revision detected, aborting all work on revision " + str(
            WorkItem.change.get("revisions", {})
            .get(str(WorkItem.revision), {})
            .get("_number", -1)
        )
    elif WorkItem.EmptyJob:
        if is_notknow_howto_test(
            WorkItem.change["revisions"][str(WorkItem.revision)]["files"]
        ):
            message = "This patch only contains changes that I don't know how to test or build. Skipping"
        else:
            message = "Cannot detect any functional changes in this patch\n"
            if not (
                is_trivial_requested(commit_message)
                or is_testonly_requested(commit_message)
                or is_buildonly_requested(commit_message)
            ):
                message += TrivialNagMessage
    elif (
        WorkItem.BuildDone
        and not WorkItem.InitialTestingStarted
        and not WorkItem.TestingStarted
    ):
        # This is after initial build completion
        message = ""
        if WorkItem.BuildError:
            for buildinfo in WorkItem.builds:
                if buildinfo.get("Failed"):
                    message += buildinfo.get("BuildMessage", "Build Failed") + "\n"
                    if buildinfo.get("ReviewComments"):
                        review_comments.update(buildinfo["ReviewComments"])

            message += (
                "\n Job output URL: "
                + WorkItem.get_base_url()
                + "/"
                + WorkItem.get_results_filename()
            )
            score = -1
        else:
            if WorkItem.retestiteration:
                message = "This is a retest #%d\n" % (WorkItem.retestiteration)
            else:
                distros = []
                for buildinfo in WorkItem.builds:
                    distros.append(buildinfo["distro"])
                message = "Builds for x86_64 " + ",".join(distros) + " successful\n"
                if WorkItem.tests and is_trivial_requested(commit_message):
                    # +7 for line number is needed for our current gerrit since it also shows some metadata at the front
                    review_comments.setdefault("/COMMIT_MSG", []).append(
                        {
                            "line": commitmsg_trivial_lineno(commit_message) + 7,
                            "message": SuspiciousTrivialUsage,
                        }
                    )

            message += (
                "Job output URL: "
                + WorkItem.get_base_url()
                + "/"
                + WorkItem.get_results_filename()
                + "\n\n"
            )
            if WorkItem.initial_tests:
                message += (
                    " Commencing initial testing: "
                    + WorkItem.requested_tests_string(WorkItem.initial_tests)
                )
            else:
                message += " This was detected as a build-only change, no further testing would be performed by this bot.\n"
                if not (
                    is_trivial_requested(commit_message)
                    or is_testonly_requested(commit_message)
                    or is_buildonly_requested(commit_message)
                ):
                    message += TrivialNagMessage
                score = 1
    elif WorkItem.InitialTestingDone and not WorkItem.TestingStarted:
        # This is after initial tests
        if WorkItem.InitialTestingError:
            if WorkItem.AddedTestFailure:
                message = "Newly added or changed test failed in initial testing:\n"
            else:
                if WorkItem.FinalReportPosted:
                    return  # final testing repeats filter on crash/timeout
                WorkItem.FinalReportPosted = True
                message = "Initial testing failed:\n"

            message += WorkItem.test_status_output(WorkItem.initial_tests)
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message = "Initial testing succeeded.\n" + WorkItem.test_status_output(
                WorkItem.initial_tests
            )
            if WorkItem.tests:
                message += (
                    "\nCommencing standard testing: \n- "
                    + WorkItem.requested_tests_string(WorkItem.tests)
                )
            else:
                message += "\nNo additional testing was requested"
                score = 1
    elif WorkItem.TestingDone:
        if WorkItem.FinalReportPosted:
            return  # We already posted final report, these are remaining timeout/crash strugglers.

        WorkItem.FinalReportPosted = True
        message = ""
        if is_trivial_requested(commit_message):
            message += TrivialIgnoredMessage + "\n\n"
        message += "Testing has completed "
        if WorkItem.TestingError:
            message += "with errors!\n"
            score = -1
            review_comments = WorkItem.ReviewComments
        else:
            message += "Successfully\n"
        message += WorkItem.test_status_output(WorkItem.tests)
    else:
        # This is one of those intermediate states like not
        # Fully complete testing round or whatnot, so don't do anything.
        # message = "Help, I don't know why I am here" + str(vars(WorkItem))
        return

    # Errors = notify owner, no errors - no need to spam people
    if score < 0:
        notify = "OWNER"
    else:
        notify = "NONE"

    if USE_CODE_REVIEW_SCORE:
        code_review_score = score
    else:
        if WorkItem.AddedTestFailure:
            code_review_score = -1
        else:
            code_review_score = 0

    outputdict = {
        "message": (message),
        "labels": {"Code-Review": code_review_score},
        "comments": review_comments,
        "notify": notify,
    }
    if WorkItem.change.get("branchwide", False) or not reviewer.post_review(
        WorkItem.change, WorkItem.revision, outputdict
    ):
        # Ok, we had a failure posting this message, let's save it for
        # later processing
        savefile = (
            FAILED_POSTS_DIR
            + "/build-"
            + str(WorkItem.buildnr)
            + "-"
            + str(WorkItem.changenr)
            + "."
            + str(WorkItem.revision)
        )
        if os.path.exists(savefile + ".json"):
            attempt = 1
            while os.path.exists(savefile + "-try" + str(attempt) + ".json"):
                attempt += 1
            savefile = savefile + "-try" + str(attempt)

        try:
            with open(savefile + ".json", "w") as outfile:
                json.dump(
                    {"change": WorkItem.change, "output": outputdict}, outfile, indent=4
                )
        except OSError:
            # Only if we cannot save
            pass


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


class TestType(Enum):
    STYLE = 1
    REPORT = 2
    OUT_OF_TREE_BUILD = 3
    IN_TREE_BUILD = 4
    VM = 5
    OUT_OF_TREE_BUILD_CHECK = 6


TESTS = [
    {
        "Command": "./podman-ktest run_immutable -w /home/ktest/git/lustre-release /home/ktest/ktest/tools/checkpatch-parallel",
        "Title": "Checkpatch Report",
        "Description": "Run checkpatch.pl on the entire Lustre tree",
        "Enforced": False,
        "Type": TestType.REPORT,
    },
    {
        "Command": "./podman-ktest run_immutable -w /home/ktest/git/lustre-release /home/ktest/ktest/tools/contributions",
        "Title": "Contribution Report",
        "Description": "Fetch contribution statistics for Lustre tree",
        "Enforced": False,
        "Type": TestType.REPORT,
    },
    {
        "Command": "./podman-ktest run_immutable -w /home/ktest/git/lustre-release /home/ktest/ktest/tools/license-status",
        "Title": "SPDX Report",
        "Description": "Fetch license info for Lustre tree",
        "Enforced": False,
        "Type": TestType.REPORT,
    },
    {
        "Command": "./podman-ktest run_immutable -w /home/ktest/git/lustre-release /home/ktest/ktest/tools/kernel_doc_helper.sh",
        "Title": "Kernel Doc Report",
        "Description": "Check the number of kernel doc errors",
        "Enforced": False,
        "Type": TestType.REPORT,
    },
    {
        "Command": "./podman-ktest run -w /home/ktest/git/lustre-release /home/ktest/ktest/tools/checkpatch",
        "Title": "Checkpatch",
        "Description": "Run checkpatch.pl from the given mainline kernel",
        "Enforced": False,
        "Type": TestType.STYLE,
    },
    {
        "Command": "./podman-ktest run ./qlkbuild build --purge-ktest-out 1 --clean-git 1 --allow-warnings 0 --build-lustre 1",
        "Title": "Lustre Build Strict",
        "Description": "Build Lustre (LLVM/OutOfTree/NoWarnings)",
        "Enforced": False,
        "Type": TestType.OUT_OF_TREE_BUILD,
    },
    {
        "Command": "./podman-ktest run ./qlkbuild build --purge-ktest-out 1 --clean-git 1 --allow-warnings 1 --build-lustre 1",
        "Title": "Lustre Build",
        "Description": "Build Lustre (LLVM/OutOfTree)",
        "Enforced": True,
        "Type": TestType.OUT_OF_TREE_BUILD,
    },
    {
        "Command": "./podman-ktest build_al2023",
        "Title": "Build on Amazon Linux 2023",
        "Description": "Build Lustre (AL2023/OutOfTree)",
        "Enforced": True,
        "Type": TestType.OUT_OF_TREE_BUILD_CHECK,
    },
    {
        "Command": "./podman-ktest build_u24",
        "Title": "Build on Ubuntu 24",
        "Description": "Build Lustre (U24/OutOfTree)",
        "Enforced": True,
        "Type": TestType.OUT_OF_TREE_BUILD_CHECK,
    },
    {
        "Command": "./podman-ktest run_immutable ./qlkbuild ccplugin --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 1",
        "Title": "Compiler Plugin",
        "Description": "Run Lustre LLVM Compiler Plugin",
        "Enforced": False,
        "Type": TestType.OUT_OF_TREE_BUILD_CHECK,
    },
    {
        "Command": "./podman-ktest run_immutable ./qlkbuild clang-tidy --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 1",
        "Title": "Clang Tidy",
        "Description": "Run Clang Tidy",
        "Enforced": False,
        "Type": TestType.OUT_OF_TREE_BUILD_CHECK,
    },
    {
        "Command": "./podman-ktest run ./qlkbuild build --purge-ktest-out 1 --clean-git 1 --allow-warnings 1 --build-lustre 1",
        "Title": "Build In-Tree Lustre",
        "Description": "Build Lustre (InTree)",
        "Enforced": True,
        "Type": TestType.IN_TREE_BUILD,
    },
    {
        "Command": "./podman-ktest run_immutable ./qlkbuild run --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 0 --build-kernel 0 ./tests/fs/lustre/boot.ktest",
        "Title": "boot.ktest",
        "Description": "Boot a simple Lustre kernel",
        "Enforced": True,
        "Type": TestType.VM,
    },
    {
        "Command": "./podman-ktest run_immutable ./qlkbuild run --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 0 --build-kernel 0 ./tests/fs/lustre/llmount.ktest",
        "Title": "llmount.ktest",
        "Description": "Mount and umount Lustre",
        "Enforced": True,
        "Type": TestType.VM,
    },
    {
        "Command": "./podman-ktest run_immutable ./qlkbuild run --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 0 --build-kernel 0 ./tests/fs/lustre/llog-unit.ktest",
        "Title": "llog-unit.ktest",
        "Description": "Run llog regression tests",
        "Enforced": True,
        "Type": TestType.VM,
    },
    {
        "Command": "./podman-ktest run_immutable ./qlkbuild run --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 0 --build-kernel 0 ./tests/fs/lustre/mgs-setup.ktest",
        "Title": "mgs-setup.ktest",
        "Description": "Test a separate MGS setup",
        "Enforced": True,
        "Type": TestType.VM,
    },
]


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
        self.branch_review_interval = 60 * 60 * 2
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
        if GERRIT_DRYRUN:
            return
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
            if GERRIT_FORCETOPIC:
                query["topic"] = GERRIT_FORCETOPIC

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

    def run_test(self, command, change_id, name, home_path, enforced, description):
        log_str, rc, runtime = Reviewer.run_script(command)
        if not log_str:
            log_str = ""

        return self.generate_log_page(
            OUTPUT_DIR,
            change_id + "_" + name.replace(" ", "_") + ".html",
            log_str,
            name,
            home_path,
            rc,
            enforced,
            runtime,
            description,
        )

    def run_tests_for_group(self, tests, change_id, home_path):
        rows = ""

        for test in tests:
            rows += self.run_test(
                test["Command"],
                change_id,
                test["Title"],
                home_path,
                test["Enforced"],
                test["Description"],
            )

        return rows

    def run_tests_for_group_parallel(self, tests, change_id, home_path):
        tests = list(tests)
        results = [None] * len(tests)

        with ThreadPoolExecutor(max_workers=64) as executor:
            futures = {
                executor.submit(
                    self.run_test,
                    test["Command"],
                    change_id,
                    test["Title"],
                    home_path,
                    test["Enforced"],
                    test["Description"],
                ): idx
                for idx, test in enumerate(tests)
            }

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    # Capture unexpected errors so UI stays consistent
                    result = f"<tr><td colspan=5>Error: {e}</td></tr>"
                results[idx] = result

        return "".join(results)

    def run_tests(self, branchwide, change_id, home_path):
        rows = ""
        tables = ""

        if branchwide:
            rows = self.run_tests_for_group_parallel(
                (t for t in TESTS if t["Type"] == TestType.REPORT),
                change_id,
                home_path,
            )
            tables += table_template.format(title="Reports", rows=rows)
            rows = ""

        if not branchwide:
            rows = self.run_tests_for_group(
                (t for t in TESTS if t["Type"] == TestType.STYLE),
                change_id,
                home_path,
            )
            tables += table_template.format(title="Style", rows=rows)
            rows = ""

        rows = self.run_tests_for_group(
            (t for t in TESTS if t["Type"] == TestType.OUT_OF_TREE_BUILD),
            change_id,
            home_path,
        )
        tables += table_template.format(title="Out-of-Tree Build", rows=rows)
        rows = ""

        rows = self.run_tests_for_group_parallel(
            (t for t in TESTS if t["Type"] == TestType.OUT_OF_TREE_BUILD_CHECK),
            change_id,
            home_path,
        )
        tables += table_template.format(title="Out-of-Tree Checks", rows=rows)
        rows = ""

        rows = self.run_tests_for_group(
            (t for t in TESTS if t["Type"] == TestType.IN_TREE_BUILD),
            change_id,
            home_path,
        )
        tables += table_template.format(title="In-Tree Build", rows=rows)
        rows = ""

        test_list = list(t for t in TESTS if t["Type"] == TestType.VM)

        if branchwide:
            upper_limit = 51
        else:
            upper_limit = 5

        test_list += [
            {
                "Command": f"./podman-ktest run_immutable ./qlkbuild run --purge-ktest-out 0 --clean-git 0 --allow-warnings 1 --build-lustre 0 --build-kernel 0 ./tests/fs/lustre/sanity/sanity-{i}.ktest",
                "Title": f"sanity-{i}.ktest",
                "Description": f"Run sanity.sh tests (group {i})",
                "Enforced": False,
                "Type": TestType.VM,
            }
            for i in range(1, upper_limit)
        ]

        rows = self.run_tests_for_group_parallel(
            test_list,
            change_id,
            home_path,
        )

        tables += table_template.format(title="QEMU", rows=rows)
        rows = ""

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
        if (
            abs(datetime.now() - date_created).days > IGNORE_OLDER_THAN_DAYS
            and not GERRIT_DRYRUN
        ):
            self._debug("change_needs_review: Created too long ago")
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
            subprocess.run(["git", "commit", "-m", message], check=True)

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
            if StopOnIdle and len(WorkList) == 0:
                print_WorkList_to_HTML()
                sys.exit(0)

            if not DrainQueueAndStop:
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
