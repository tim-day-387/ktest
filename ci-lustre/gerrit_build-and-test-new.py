#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2014, Intel Corporation.
#
# Author: John L. Hammond <john.hammond@intel.com>
# Modified for ktest: Timothy Day <timday@amazon.com>
#

"""
Gerrit Universal Reviewer Daemon
~~~~~~ ~~~~~~~~~~ ~~~~~~~~ ~~~~~~

* Watch for new change revisions in a gerrit instance.
* Pass new revisions through builders and testers
* POST reviews back to gerrit based on results
"""

import base64
import fnmatch
import logging
import json
import os
import sys
import requests
import time
import urllib.request, urllib.parse, urllib.error
import queue
import threading
import pwd
import re
import random
import operator
import html as pyhtml
from datetime import datetime
import dateutil.parser
import shutil
import pickle
from pprint import pprint
import subprocess
import resource
import tracemalloc

tracemalloc.start()

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

def _getenv_list(key, default=None, sep=":"):
    """
    'PATH' => ['/bin', '/usr/bin', ...]
    """
    value = os.getenv(key)
    if value is None:
        return default
    return value.split(sep)


GERRIT_HOST = os.getenv("GERRIT_HOST", "review.whamcloud.com")
GERRIT_PROJECT = os.getenv("GERRIT_PROJECT", "fs/lustre-release")
GERRIT_BRANCH = ["master"]
GERRIT_AUTH_PATH = os.getenv("GERRIT_AUTH_PATH", "GERRIT_AUTH")
GERRIT_CHANGE_NUMBER = os.getenv("GERRIT_CHANGE_NUMBER", None)
GERRIT_DRYRUN = os.getenv("GERRIT_DRYRUN", None)
GERRIT_FORCEALLTESTS = os.getenv("GERRIT_FORCEALLTESTS", None)
GERRIT_BRANCHMONITORDIR = os.getenv("GERRIT_BRANCHMONITORDIR", "./branches/")
GERRIT_COMMANDMONITORDIR = os.getenv("GERRIT_COMMANDMONITORDIR", "./commands/")

# When this is set - only changes with this topic would be tested.
# good for trial runs before big deployment
GERRIT_FORCETOPIC = os.getenv("GERRIT_FORCETOPIC", None)

SAVEDSTATE_DIR = "savedstate"
DONEWITH_DIR = "donewith"
FAILED_POSTS_DIR = "failed_posts"
OUTPUT_DIR = "/home/timothy/git/upstream-patch-review"
LAST_BUILD_ID = "LASTBUILD_ID"
STYLESHEET = "styles.css"

StopMachine = False
StopOnIdle = False
DrainQueueAndStop = False
managerthread = None

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
STYLE_LINK = os.getenv(
    "STYLE_LINK", "http://wiki.lustre.org/Lustre_Coding_Style_Guidelines"
)
# TrivialNagMessage = 'It is recommended to add "Test-Parameters: trivial" directive to patches that do not change any running code to ease the load on the testing subsystem'
TrivialNagMessage = ""  # don't want to promote trivial anymore
TrivialIgnoredMessage = 'Even though "Test-Parameters: trivial" was detected, this deeply suspicious bot still run some testing'
SuspiciousTrivialUsage = "Suspicious test-param trivial usage for general code changes"

USE_CODE_REVIEW_SCORE = False

IGNORE_OLDER_THAN_DAYS = 30

build_queue = queue.Queue()
build_condition = threading.Condition()
testing_queue = queue.PriorityQueue()
testing_condition = threading.Condition()
managing_queue = queue.Queue()
managing_condition = threading.Condition()
reviewer = None
StatsWriter = None

fsconfig = {}

architectures = ["x86_64"]
builders = []
workers = []

CODE_FILES = ["*.[ch]"]
I_DONT_KNOW_HOW_TO_TEST_THESE = [
    "contrib/lbuild/*",
    "*dkms*",
    "*spec*",
    "debian/*",
    "rpm/*",
    "lustre/kernel_patches/*",
]
TEST_SCRIPT_FILES = ["lustre/tests/*"]
LUTF_ONLY_FILES = ["lustre/tests/lutf/*"]


# We store all job items here
WorkList = []
DoneList = []


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


def populate_testlist_from_array(
    testlist, testarray, LDiskfsOnly, ZFSOnly, DNE=True, Force=False, Branch=None
):
    for item in testarray:

        def getemptytest(item):
            test = {}

            # Must always have these two
            test["test"] = item["test"]
            test["timeout"] = item["timeout"]
            for elem in (
                "name",
                "testparam",
                "DNE",
                "env",
                "SSK",
                "SELINUX",
                "fstype",
                "austerparam",
                "vmparams",
                "singletimeout",
                "clientdistro",
                "serverdistro",
                "forcedistro",
            ):
                if item.get(elem):
                    test[elem] = item[elem]

            if not test.get("name"):
                test["name"] = test["test"]

            return test

        if item.get("disabled") and not Force:
            continue
        # Exclude somestuff that only partially landed.
        if item.get("onlybranch") and Branch:
            if not Branch.startswith(item.get("onlybranch", "noitdoesnot")):
                continue
        if not DNE and item.get("DNE"):
            continue

        if item.get("fstype"):
            test = getemptytest(item)
            # Items that specify fstype are self contained and are not expanded
            testlist.append(test)
            continue
        if LDiskfsOnly:
            test = getemptytest(item)
            test["fstype"] = "ldiskfs"
            test["DNE"] = DNE
            testlist.append(test)
        if ZFSOnly:
            test = getemptytest(item)
            test["fstype"] = "zfs"
            testlist.append(test)
        if ZFSOnly and DNE and not LDiskfsOnly:
            # Need to also do DNE run
            test = getemptytest(item)
            test["fstype"] = "zfs"
            test["DNE"] = True
            testlist.append(test)
        if LDiskfsOnly and not ZFSOnly and DNE:
            # Need to capture non-DNE run for ldiskfs
            test = getemptytest(item)
            test["fstype"] = "ldiskfs"
            testlist.append(test)

    return testlist


def determine_distros_from_change(change):
    try:
        with open("distrolist.json", "r") as blah:
            distrolist = json.load(blah)
    except:
        return []  # We return empty list on error and it'll use default distro

    branch = change["branch"]
    distros = []
    for distro in distrolist:
        if distro.get("branch"):
            if not branch.startswith(distro["branch"]):
                continue
        distros.append(distro)
    return distros


def determine_testlist(change, filelist, commit_message, ForceFull=False, Branch=None):
    """Try to guess what tests to run based on the changes"""
    trivial_requested = is_trivial_requested(commit_message)
    DoNothing = True
    NonTestFilesToo = False
    BuildOnly = False
    LNetOnly = False
    LUTFOnly = False
    ZFSOnly = False
    LDiskfsOnly = False
    FullRun = False
    requested_tests = testlist_from_commit_message(commit_message)

    # Load updated definitions of what we know how to test and what we don't
    IGNORE_FILES = []
    BUILD_ONLY_FILES = []
    LDISKFS_ONLY_FILES = []
    LNET_ONLY_FILES = []
    ZFS_ONLY_FILES = []

    try:
        with open("filelists/ignore.json", "r") as blah:
            IGNORE_FILES = json.load(blah)
    except:
        pass

    try:
        with open("filelists/buildonly.json", "r") as blah:
            BUILD_ONLY_FILES = json.load(blah)
    except:
        pass

    try:
        with open("filelists/ldiskfs.json", "r") as blah:
            LDISKFS_ONLY_FILES = json.load(blah)
    except:
        pass

    try:
        with open("filelists/zfs.json", "r") as blah:
            ZFS_ONLY_FILES = json.load(blah)
    except:
        pass

    try:
        with open("filelists/lnet.json", "r") as blah:
            LNET_ONLY_FILES = json.load(blah)
    except:
        pass

    for item in sorted(filelist):
        # I wish there was a way to detect deleted files, but alas, not in our gerrit?
        if match_fnmatch_list(item, IGNORE_FILES):
            continue  # with deletion would set BuildOnly
        DoNothing = False
        if match_fnmatch_list(item, LUTF_ONLY_FILES):
            LUTFOnly = True
            continue
        if match_fnmatch_list(item, TEST_SCRIPT_FILES):
            testname = os.path.basename(item).replace(".sh", "")
            # if it was already requested by the test-params, don't add it again
            if not testname in requested_tests:
                requested_tests.append(testname)
            if not item.endswith(".c"):
                continue
        NonTestFilesToo = True
        if match_fnmatch_list(item, BUILD_ONLY_FILES):
            BuildOnly = True
            continue
        if match_fnmatch_list(item, ZFS_ONLY_FILES):
            ZFSOnly = True
            continue
        if match_fnmatch_list(item, LDISKFS_ONLY_FILES):
            LDiskfsOnly = True
            continue
        if match_fnmatch_list(item, LNET_ONLY_FILES):
            LNetOnly = True
            continue
        # Otherwise unknown file = full test
        # Need to be smarter here about individual test file changes I guess?
        FullRun = True

    # For lnet only changes we'd volunteer a zfs only run
    # to ensure actual Lustre operations still work.
    if LNetOnly and not FullRun and not LDiskfsOnly and not ZFSOnly:
        ZFSOnly = True

    # any changes outside the build only -> do testing too.
    # FullRun will reset everything separately below
    if BuildOnly and (requested_tests or LNetOnly or LDiskfsOnly or ZFSOnly):
        BuildOnly = False

    # Override for testing
    if GERRIT_FORCEALLTESTS or ForceFull:
        FullRun = True
        LNetOnly = True
        DoNothing = False
    else:
        if change.get("CommentOnly"):
            DoNothing = True

    if FullRun:
        LDiskfsOnly = True
        ZFSOnly = True
        LNetOnly = True
        BuildOnly = False
        # If they asked testonly, we only test what they request.
        if not is_testonly_requested(commit_message):
            trivial_requested = False

    if LUTFOnly:
        requested_tests.append("lutf")

    # Always reload testlists
    with open("ci-lustre/tests/initial.json", "r") as blah:
        initialtestlist = json.load(blah)
    with open("ci-lustre/tests/comprehensive.json", "r") as blah:
        fulltestlist = json.load(blah)
    with open("ci-lustre/tests/lnet.json", "r") as blah:
        lnettestlist = json.load(blah)
    with open("ci-lustre/tests/zfs.json", "r") as blah:
        zfstestlist = json.load(blah)
    with open("ci-lustre/tests/ldiskfs.json", "r") as blah:
        ldiskfstestlist = json.load(blah)

    initial = []
    comprehensive = []

    if not DoNothing and not BuildOnly:
        if requested_tests:
            UnknownItems = NonTestFilesToo
            foundtests = []
            for item in requested_tests:
                if item in (
                    "test-framework",
                    "functions",
                    "yaml",
                ):  # This needs a full run
                    LDiskfsOnly = True
                    ZFSOnly = True
                    LNetOnly = True
                    BuildOnly = False
                    trivial_requested = False
                    NonTestFilesToo = True  # To force runtests
                    UnknownItems = True  # to force everything
                    continue
                Found = False
                for test in (
                    initialtestlist
                    + fulltestlist
                    + lnettestlist
                    + zfstestlist
                    + ldiskfstestlist
                ):
                    if item == test["test"]:
                        foundtests.append(test)
                        # To avoid doubletesting, mark it as disabled in the
                        # regular list too - ok to do sice we reread the list
                        # every time
                        test["disabled"] = True
                        Found = True
                if not Found:
                    UnknownItems = True

            populate_testlist_from_array(
                initial, foundtests, True, True, Force=True, Branch=Branch
            )
            if not UnknownItems:
                # We just populate out test list from the changed scripts
                # we detected that we run in all possible configs
                # Force disabled tests too if we are modifying them we better
                # know how they perform
                # Also let's turn off every other test
                LNetOnly = False
                ZFSOnly = False
                LDiskfsOnly = False
                trivial_requested = True
            else:
                # Hm, not sure what to do here? Probably run everything
                # as requested in addition to modified test files?
                trivial_requested = False

        # Careful, if initial test list was filled above, we presume it's
        # comprehensive but if we have any other files modified that are
        # non-test - add standard initial testing too.
        if not initial or NonTestFilesToo:
            populate_testlist_from_array(
                initial, initialtestlist, LDiskfsOnly, ZFSOnly, Branch=Branch
            )
        if change.get("updated_tests"):
            updtests = change["updated_tests"]
            updtestlist = []
            for i in updtests.keys():
                subtests = []
                for j in updtests[i]:
                    subtests.append(j.replace("test_", ""))
                for j in range(10):
                    updtestlist.append(
                        {
                            "name": i + "-special" + str(j + 1),
                            "test": i,
                            "timeout": -1,
                            "testparam": "--only " + ",".join(subtests),
                        }
                    )
                populate_testlist_from_array(
                    initial, updtestlist, True, False, DNE=True
                )
                populate_testlist_from_array(
                    initial, updtestlist, False, True, DNE=True
                )

        if LNetOnly:
            # For items in this list we don't care about fs as it's supposed
            # to be fs-neutral Lnet-only stuff like lnet-selftest
            populate_testlist_from_array(
                comprehensive, lnettestlist, False, True, DNE=False, Branch=Branch
            )

        if ZFSOnly:
            # For items in this list we don't care about fs as it's supposed
            # to be fs-neutral Lnet-only stuff like lnet-selftest
            populate_testlist_from_array(
                comprehensive, zfstestlist, False, True, Branch=Branch
            )
        if LDiskfsOnly:
            # For items in this list we don't care about fs as it's supposed
            # to be fs-neutral Lnet-only stuff like lnet-selftest
            populate_testlist_from_array(
                comprehensive, ldiskfstestlist, True, False, Branch=Branch
            )

        if not trivial_requested or GERRIT_FORCEALLTESTS:
            populate_testlist_from_array(
                comprehensive, fulltestlist, LDiskfsOnly, ZFSOnly, Branch=Branch
            )

    return (DoNothing, initial, comprehensive)


def commitmsg_trivial_lineno(message):
    trivial_re = re.compile("^Test-Parameters:.*trivial")
    for idx, line in enumerate(message.splitlines()):
        if trivial_re.match(line):
            return idx
    return 0


def is_trivial_requested(message):
    return commitmsg_trivial_lineno(message) > 0


def testlist_from_commit_message(message):
    testlist = []
    testlist_re = re.compile(r"^Test-Parameters:.*testlist=([-a-zA-Z0-9_,\.]+)")
    # XXX Need to do austeroptions, envdefinitions and perhaps more from
    # https://wiki.whamcloud.com/display/PUB/Changing+Test+Parameters+with+Gerrit+Commit+Messages
    for line in message.splitlines():
        result = testlist_re.match(line)
        if result:
            testlist.append(result.group(1))
    return testlist


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


def parse_checkpatch_output(out, path_line_comments, warning_count):
    """
    Parse string output out of CHECKPATCH into path_line_comments.
    Increment warning_count[0] for each warning.

    path_line_comments is { PATH: { LINE: [COMMENT, ...] }, ... }.
    """

    def add_comment(path, line, level, kind, message):
        """_"""
        logging.debug("add_comment %s %d %s %s '%s'", path, line, level, kind, message)

        path_comments = path_line_comments.setdefault(path, {})
        line_comments = path_comments.setdefault(line, [])
        line_comments.append("(style) %s\n" % message)
        warning_count[0] += 1

    level = None  # 'ERROR', 'WARNING'
    kind = None  # 'CODE_INDENT', 'LEADING_SPACE', ...
    message = None  # 'code indent should use tabs where possible'

    for line in out.splitlines():
        # ERROR:CODE_INDENT: code indent should use tabs where possible
        # #404: FILE: lustre/liblustre/dir.c:103:
        # +        op_data.op_hash_offset = hash_x_index(page->index, 0);$
        line = line.strip()
        if not line:
            level, kind, message = None, None, None
        elif line[0] == "#":
            # '#404: FILE: lustre/liblustre/dir.c:103:'
            tokens = line.split(":", 5)
            if len(tokens) != 5 or tokens[1] != " FILE":
                continue

            path = tokens[2].strip()
            line_number_str = tokens[3].strip()
            if not line_number_str.isdigit():
                continue

            line_number = int(line_number_str)

            if path and level and kind and message:
                add_comment(path, line_number, level, kind, message)
        elif line[0] == "+":
            continue
        else:
            # ERROR:CODE_INDENT: code indent should use tabs where possible
            try:
                level, kind, message = line.split(":", 2)
            except ValueError:
                level, kind, message = None, None, None

            if level not in ("ERROR", "WARNING"):
                level, kind, message = None, None, None


def find_and_abort_duplicates(workitem):
    for item in WorkList:
        if item.changenr == workitem.changenr:
            item.Aborted = True


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


def make_requested_testlist(requestedlistparams, branch):
    # XXX - this is a copy from another func. need to
    # have it all in a single place
    with open("ci-lustre/tests/initial.json", "r") as blah:
        initialtestlist = json.load(blah)
    with open("ci-lustre/tests/comprehensive.json", "r") as blah:
        fulltestlist = json.load(blah)
    with open("ci-lustre/tests/lnet.json", "r") as blah:
        lnettestlist = json.load(blah)
    with open("ci-lustre/tests/zfs.json", "r") as blah:
        zfstestlist = json.load(blah)
    with open("ci-lustre/tests/ldiskfs.json", "r") as blah:
        ldiskfstestlist = json.load(blah)

    testarray = []
    for item in requestedlistparams["testlist"].split(","):
        item = item.strip()
        for test in (
            initialtestlist
            + fulltestlist
            + lnettestlist
            + zfstestlist
            + ldiskfstestlist
        ):
            testname = test.get("name", test["test"])
            if item == testname:
                for i in (
                    "DNE",
                    "fstype",
                    "testparam",
                    "austerparam",
                    "vmparams",
                    "env",
                    "SSK",
                    "SELINUX",
                    "singletimeout",
                    "timeout",
                    "clientdistro",
                    "serverdistro",
                    "forcedistro",
                ):
                    if requestedlistparams.get(i):
                        test[i] = requestedlistparams[i]

                testarray.append(test)
                break

    zfsonly = requestedlistparams.get("zfs", True)
    ldiskfsonly = requestedlistparams.get("ldiskfs", True)
    DNE = requestedlistparams.get("DNE", True)
    # Force to ensure we test what was requested even if disabled
    initial_tests = populate_testlist_from_array(
        [], testarray, ldiskfsonly, zfsonly, DNE=DNE, Force=True, Branch=branch
    )
    return initial_tests


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
        self.post_enabled = False
        self.post_interval = 30
        self.update_interval = 30
        self.request_timeout = 30
        self.push_interval = 240
        self.last_push = 0

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

    def generate_log_page(self, output, page_path, logs, name, home_path, rc, enforced, time):
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

        with open(os.path.join(output, page_path), "w") as outfile:
            outfile.write(html)

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

        return f'<tr><td><a href="{page_path}">{name}</a></td><td>{test_type}</td><td>{time}</td><td style="color:{color}">{status}</td></tr>'

    @staticmethod
    def run_script(command):
        start_time = time.time()

        pipe = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out, _ = pipe.communicate("")
        returncode = pipe.returncode

        elapsed_time = int(time.time() - start_time)

        return out, returncode, elapsed_time

    def run_test(self, command, change_id, name, home_path, enforced):
        log, rc, runtime = Reviewer.run_script(command)
        if not log:
            return

        log_str = log.decode("utf-8")

        return self.generate_log_page(
            OUTPUT_DIR, change_id + "_" + name.replace(" ", "_") + ".html", log_str, name, home_path, rc, enforced, runtime
        )

    def get_patch(self, change):
        revision = change.get("current_revision")
        command = (
            "cd /home/timothy/git/lustre-release ; git fetch https://"
            + GERRIT_HOST
            + "/"
            + GERRIT_PROJECT
            + " "
            + change["revisions"][revision]["ref"]
            + "&& git format-patch -1 -n --stdout FETCH_HEAD | head -n -3"
        )
        return Reviewer.run_script(command)

    def checkout_patch(self, change):
        revision = change.get("current_revision")
        command = (
            "cd /home/timothy/git/lustre-release ; git fetch https://"
            + GERRIT_HOST
            + "/"
            + GERRIT_PROJECT
            + " "
            + change["revisions"][revision]["ref"]
            + "&& git checkout FETCH_HEAD"
        )
        return Reviewer.run_script(command)

    def analyze_patch(self, change):
        template = """\
<h1>{title}</h1>
<table>
<thead>
<tr><th>Test</th><th>Type</th><th>Runtime</th><th>Status</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
"""
        """ Extract useful info from a patch. Things like new tests added,
            tests modified, is it a comment-only change and so on. """
        patch, rc, runtime = self.get_patch(change)
        if not patch:
            return

        revision = change.get("current_revision")
        raw_change_id = change["id"].split("~", 1)[1]
        change_id = raw_change_id + "_" + revision
        home_path = OUTPUT_DIR + "/" + change_id + "_home.html"
        subject = change.get("subject", "")
        rows = ""

        try:
            commit_message = change["revisions"][str(revision)]["commit"]["message"]
        except:
            commit_message = ""

        open(home_path, "wb").close()

        rows = rows + self.generate_log_page(
            OUTPUT_DIR, change_id + "_patch.html", commit_message, "Patch", home_path, 0, True, runtime
        )

        self.checkout_patch(change)

        rows = rows + self.run_test(
            "cd /home/timothy/ws/ktest ; ./ci-lustre/build-lustre -k",
            change_id,
            "Kernel Build",
            home_path,
            True,
        )

        rows = rows + self.run_test(
            "cd /home/timothy/ws/ktest ; ./ci-lustre/build-lustre -l",
            change_id,
            "Lustre Build",
            home_path,
            True,
        )

        rows = rows + self.run_test(
            "cd /home/timothy/git/lustre-release ; git diff HEAD~1 | ~/git/linux/scripts/checkpatch.pl",
            change_id,
            "Checkpatch",
            home_path,
            False,
        )

        html_tmp = template.format(title=subject, rows=rows)
        html = page_template.format(title=subject, style=STYLESHEET, html=html_tmp)

        with open(home_path, "w") as outfile:
            outfile.write(html)

        os.setxattr(home_path, "user.patch_revision", revision.encode())
        os.setxattr(home_path, "user.change_id", raw_change_id.encode())
        os.setxattr(home_path, "user.subject", subject.encode())
        os.setxattr(home_path, "user.time_stamp", str(int(time.time())).encode())

        print_WorkList_to_HTML()

        chfile = None
        function = None
        commentonly = True
        newtests = []
        for line in patch.splitlines():
            line = line.decode("utf-8")
            # print("Working on: " + line)
            if line.startswith("+++ "):
                if newtests:
                    if not change.get("updated_tests"):
                        change["updated_tests"] = {}
                    if basename.endswith(".sh"):
                        change["updated_tests"].update(
                            {basename.replace(".sh", ""): newtests}
                        )
                    newtests = []
                chfile = line.replace("+++ b/", "")
                basename = os.path.basename(chfile)
            if not chfile:  # diff did not start yet - skip
                continue
            if line.startswith("--- "):  # src file - skip
                continue
            if line.startswith("@@ "):
                tags = line.split(" ", 5)
                if tags[0] != "@@" or tags[3] != "@@":
                    print("Malformed patch line: " + line)
                    continue
                if len(tags) > 4:
                    function = tags[4].replace("()", "")
                    if function.endswith("{"):
                        function = function[:-1]
                else:
                    function = None
            if line.startswith(" "):  # context line, not a change - skip
                if basename.endswith(".sh") and line.startswith(
                    " test_"
                ):  # context changed to new function, record it.
                    tags = line[1:].split(" ")
                    function = tags[0].replace("()", "")
                    if function.endswith("{"):
                        function = function[:-1]
                continue
            if line.startswith("-") or line.startswith(
                "+"
            ):  # added/removed/changed line
                tmp = line.replace(" ", "").replace("\t", "")  # remove spaces
                if not line[1:]:  # empty line? skip
                    continue
                if "ldiskfs/kernel_patches/" in chfile:
                    commentonly = False
                if (basename.endswith(".c") or basename.endswith(".h")) and not (
                    tmp[1:].startswith("/*")
                    or tmp[1:].startswith("//")
                    or tmp[1:].startswith("*")
                ):
                    commentonly = False
                if (
                    basename.endswith(".sh")
                    or basename.endswith(".pl")
                    or basename.endswith(".py")
                    or basename
                    in (
                        "runtests",
                        "auster",
                        "rundbench",
                        "runiozone",
                        "runmultiop_bg_pause",
                        "runvmstat",
                        "runobdstat",
                    )
                ) and not tmp[1:].startswith("#"):
                    commentonly = False
                if line[1:].startswith("test_"):
                    function = None  # Added or removed function, we'll catch with the +run_test
                if function and function not in newtests:
                    if basename.endswith(".sh") and function.startswith("test_"):
                        newtests.append(function)
                    function = None  # To ease our work

            if line.startswith("+"):  # Added/changed line
                if basename.endswith(".sh"):
                    # Try to detect a new test added.
                    # while we can try and detect new function added, instead
                    # in our framework there's a very specific pattern:
                    # +run_test 65 "Check lfs quota result"
                    # So let's match for that instead
                    if line.startswith("+run_test "):
                        tags = line.split(" ")
                        if len(tags) > 1:
                            test = "test_" + tags[1]
                            newtests.append(test)

        # Catch remaining stuff
        if newtests and basename.endswith(".sh"):
            if not change.get("updated_tests"):
                change["updated_tests"] = {}
            change["updated_tests"].update({basename.replace(".sh", ""): newtests})
        if commentonly:
            change["CommentOnly"] = True

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

    def review_change(self, change, DISTRO=None):
        """
        Review the current revision of change.
        * Pipe the patch through checkpatch(es).
        * Save results to review history.
        * POST review to gerrit.
        """
        self._debug(
            "review_change: change = %s, subject = '%s'",
            change["id"],
            change.get("subject", ""),
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

        if change.get("branchwide"):
            files = ["everything"]
            isMerge = True
        else:
            files = change["revisions"][str(current_revision)].get("files", [])
            isMerge = (
                len(change["revisions"][str(current_revision)]["commit"]["parents"]) > 1
            )
            self.analyze_patch(change)

        self._debug("determine testlist")

        (DoNothing, ilist, clist) = determine_testlist(
            change,
            files,
            commit_message,
            ForceFull=isMerge,
            Branch=change.get("branch"),
        )

        # For testonly changes only do very minimal testing for now
        # Or not.
        # if is_testonly_requested(commit_message):
        #    clist = []
        self._debug("figure out distro/build")

        if is_buildonly_requested(commit_message):
            clist = []
            ilist = []
        if DISTRO:
            distrolist = []
            if "," in DISTRO:
                for d in DISTRO.split(","):
                    distrolist.append({"distro": d})
                DISTRO = DISTRO.split(",")[0]  # Make first one the main one
        else:
            distrolist = determine_distros_from_change(change)

            """
            workItem = GerritWorkItem(change, distrolist, ilist, clist, fsconfig, EmptyJob=DoNothing, Reviewer=self, DISTRO=DISTRO)
        if DoNothing:
            add_review_comment(workItem)
        else:
            managing_condition.acquire()
            managing_queue.put(workItem)
            managing_condition.notify()
            managing_condition.release()
            """

        self.write_history(change["id"], current_revision, 0)

    def update(self):
        """
        GET recently updated changes and review as needed.
        """

        self.check_for_commands()

        new_timestamp = _now()
        age = 16  # Age
        # self._debug("update: age = %d", age)

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

    def check_for_commands(self):
        global StopOnIdle
        global DrainQueueAndStop
        global GERRIT_FORCETOPIC
        # See if we got any commands
        for commandfile in os.listdir(GERRIT_COMMANDMONITORDIR):
            command = {}
            try:
                with open(GERRIT_COMMANDMONITORDIR + "/" + commandfile, "r") as cmdfl:
                    try:
                        command = json.load(cmdfl)
                    except:  # XXX Add some json format error here
                        pass
                    os.unlink(GERRIT_COMMANDMONITORDIR + "/" + commandfile)
            except OSError:
                pass
            if not command:
                continue
            change = {}
            testlist = command.get("testlist")
            distro = command.get("distro")
            subject = command.get("subject")
            if command.get("test-commit"):
                branch = command.get("branch")
                if not branch:
                    print("Asked for commit, but not branch to match, skipping")
                    continue
                githash = command["test-commit"]
                if not subject:
                    subject = "githash test request " + str(command)
                change = make_change_from_hash(githash, subject, branch)
            elif command.get("test-ref"):
                desiredchange = str(command["test-ref"])
                if not desiredchange:
                    continue
                print("Asking for single change " + desiredchange)
                open_changes = self.get_changes(
                    {"status": "open", "change": desiredchange}, Absolute=True
                )
                if open_changes:
                    change = open_changes[0]

            # We'll hide it in the change so it's visible everywhere
            if command.get("completion-cb"):
                change["completion-cb"] = command["completion-cb"]

            if command.get("highprio"):
                change["highprio"] = command["highprio"]

            if change:
                if testlist:
                    tlist = make_requested_testlist(command, change.get("branch"))

                    workitem = GerritWorkItem(
                        change, None, tlist, [], fsconfig, Reviewer=self, DISTRO=distro
                    )
                    managing_condition.acquire()
                    managing_queue.put(workitem)
                    managing_condition.notify()
                    managing_condition.release()
                else:
                    self.review_change(change, DISTRO=distro)
                continue

            if command.get("retest-item"):
                retestitem = str(command["retest-item"])
                self._debug("Asked to retest build id: " + retestitem)
                # This is a retest request, see if we got new test list
                # or if not - clean up old tests.
                retestfile = DONEWITH_DIR + "/" + retestitem + ".pickle"
                if not os.path.exists(retestfile):
                    self._debug("Build id: " + retestitem + " does not exist")
                    continue  # no file - nothing to do. error print?

                with open(retestfile, "rb") as blah:
                    try:
                        workitem = pickle.load(blah)
                    except:
                        self._debug("Build id: " + retestitem + " cannot be loaded")
                        continue
                if not workitem.BuildDone or workitem.BuildError:
                    self._debug("Build id: " + retestitem + " has no successful build")
                    # Cannot retest a failed build
                    continue

                workitem.Reviewer = reviewer  # Since it cannot be saved otherwise
                workitem.fsconfig = fsconfig
                workitem.FinalReportPosted = False  # To post new report.

                # We don't know how many times it was retested so need to find
                # out by checkign the done with place.
                while os.path.exists(DONEWITH_DIR + "/" + workitem.get_saved_name()):
                    workitem.retestiteration += 1

                workitem.Aborted = False
                workitem.AbortDone = False
                workitem.TestingDone = False
                workitem.TestingStarted = False
                workitem.TestingError = False
                workitem.InitialTestingDone = False
                workitem.InitialTestingError = False
                workitem.InitialTestingStarted = False
                # If they requested a different distro to test, lets
                # cross our fingers and hope the artefacts really are there.
                if distro:
                    workitem.distro = distro

                try:
                    if testlist:
                        workitem.initial_tests = make_requested_testlist(
                            command, workitem.change.get("branch")
                        )
                        workitem.tests = []
                    else:
                        # Redetermine testlist based on current rules
                        # instead of depending on old rules in place of
                        # initial run
                        change = workitem.change
                        current_revision = change.get("current_revision")
                        commit_message = change["revisions"][str(current_revision)][
                            "commit"
                        ]["message"]
                        files = change["revisions"][str(current_revision)].get(
                            "files", []
                        )
                        isMerge = (
                            len(
                                change["revisions"][str(current_revision)]["commit"][
                                    "parents"
                                ]
                            )
                            > 1
                        )
                        (DoNothing, ilist, clist) = determine_testlist(
                            change,
                            files,
                            commit_message,
                            ForceFull=isMerge,
                            Branch=change.get("branch"),
                        )
                        workitem.initial_tests = ilist
                        workitem.tests = clist
                except:  # Add some array list here?
                    self._debug("Build id: " + retestitem + " cannot update test list")

                WorkList.append(workitem)
                managing_condition.acquire()
                managing_queue.put(workitem)
                managing_condition.notify()
                managing_condition.release()
            elif command.get("abort"):
                buildnr = command.get("abort")
                self._debug("Requested abort of build " + str(buildnr))
                for item in WorkList:
                    if item.buildnr == buildnr:
                        item.Aborted = True
                        self._debug("Aborted build " + str(buildnr))
                        break
            elif command.get("idlestop") != None:
                StopOnIdle = command["idlestop"]
            elif command.get("drain-and-stop") != None:
                DrainQueueAndStop = command["drain-and-stop"]
                StopOnIdle = DrainQueueAndStop
            elif command.get("forcetopic") != None:
                GERRIT_FORCETOPIC = command["forcetopic"]
            elif command.get("removetopic") != None:
                GERRIT_FORCETOPIC = None
            elif command.get("add-builders") != None:
                buildersfilename = command["add-builders"]
                try:
                    with open(buildersfilename) as buildersfile:
                        buildersinfo = json.load(buildersfile)
                except:
                    self._debug("Malformed builders file " + buildersfilename)
                else:
                    for builderinfo in buildersinfo:
                        if not builderinfo.get("run"):
                            continue
                        builders.append(
                            mybuilder.Builder(
                                builderinfo,
                                fsconfig,
                                build_condition,
                                build_queue,
                                managing_condition,
                                managing_queue,
                            )
                        )
            elif command.get("add-workers") != None:
                workersfilename = command["add-workers"]
                try:
                    with open(workersfilename) as nodes_file:
                        newworkers = json.load(nodes_file)
                except:
                    self._debug("Malformed testnodes file " + workersfilename)
                else:
                    for worker in newworkers:
                        if not worker.get("name"):
                            continue
                        # we need to ensure the worker we are adding
                        # does not yet exist or hilarity will ensue
                        found = False
                        for t in workers:
                            if t["name"] == worker["name"]:
                                found = True
                                break
                        if not found:
                            worker["thread"] = mytester.Tester(
                                worker,
                                fsconfig,
                                testing_condition,
                                testing_queue,
                                managing_condition,
                                managing_queue,
                            )
                            workers.append(worker)

            elif command.get("del-builders") != None:
                self._debug("Removing builders is not yet implemented")
            elif command.get("del-workers") != None:
                workersfilename = command["del-workers"]
                try:
                    with open(workersfilename) as nodes_file:
                        newworkers = json.load(nodes_file)
                except:
                    self._debug("Malformed testnodes file " + workersfilename)
                else:
                    found = False
                    for worker in newworkers:
                        for t in workers:
                            if t["name"] == worker.get("name"):
                                t["thread"].RequestExit = True
                                found = True
                                break
                    if found:  # need to wake all waiters to ensure threads exit
                        testing_condition.acquire()
                        testing_condition.notifyAll()
                        testing_condition.release()

            else:
                self._debug("Unknown command file contents: " + str(command))

        # Now check if we have any branches to test
        for branch in os.listdir(GERRIT_BRANCHMONITORDIR):
            try:
                with open(GERRIT_BRANCHMONITORDIR + "/" + branch, "r") as brfil:
                    subject = brfil.read()
                    subject = subject.strip()

                os.unlink(GERRIT_BRANCHMONITORDIR + "/" + branch)
            except OSError:
                subject = "Cannot read file"
            # XXX
            change = make_change_from_hash(branch, subject, branch)
            change["highprio"] = True

            self.review_change(change)

    def update_single_change(self, change):

        self.load_history()

        open_changes = self.get_changes(
            {"status": "open", "change": change}, Absolute=True
        )
        self._debug("update: got %d open_changes", len(open_changes))

        for change in open_changes:
            if self.change_needs_review(change):
                self.review_change(change)

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

            # Skip if the only staged file is REVIEW_HISTORY
            if staged_files == ["REVIEW_HISTORY"]:
                self._debug("only REVIEW_HISTORY changed, skipping commit")
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

    log, rc, runtime = Reviewer.run_script(command)
    if not log:
        return

    log_str = log.decode("utf-8")

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
        os.path.join(OUTPUT_DIR, STYLESHEET)
    )

    files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith("_home.html")]
    files.sort()

    rows = []

    # Collect (file, timestamp, attrs) tuples first
    file_data = []
    for f in files:
        path = os.path.join(OUTPUT_DIR, f)
        attrs = []

        patch_revision = os.getxattr(path, "user.patch_revision").decode(
            errors="replace"
        )
        change_id = os.getxattr(path, "user.change_id").decode(errors="replace")
        subject = os.getxattr(path, "user.subject").decode(errors="replace")
        timestamp_raw = os.getxattr(path, "user.time_stamp").decode(errors="replace")

        # Convert timestamp (string) to float
        try:
            timestamp = float(timestamp_raw)
            readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
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
                    enforced_val = os.getxattr(path, enforced_attr).decode(errors="replace")
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
        attrs.extend([
            subject,
            f'<a href="https://review.whamcloud.com/plugins/gitiles/fs/lustre-release/+/{patch_revision}">{patch_revision}</a>',
            f'<a href="https://review.whamcloud.com/c/fs/lustre-release/+/{change_id}">{change_id}</a>',
            readable,
            f'<span style="color:{enforced_color};">{enforced_summary}</span>',
            f'<span style="color:{optional_color};">{optional_summary}</span>',
        ])

        file_data.append((f, timestamp, attrs))

    # Sort descending by timestamp (newest first)
    file_data.sort(key=lambda x: x[1], reverse=True)

    # Build HTML rows
    for f, timestamp, attrs in file_data:
        attr_html = "".join(f"<td>{v}</td>" for v in attrs)
        rows.append(f'<tr><td><a href="{pyhtml.escape(f)}">Link</a></td>{attr_html}</tr>')

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

    for savedstateitem in os.listdir(SAVEDSTATE_DIR):
        with open(SAVEDSTATE_DIR + "/" + savedstateitem, "rb") as blah:
            try:
                saveitem = pickle.load(blah)
            except:
                # delete bad item.
                os.unlink(SAVEDSTATE_DIR + "/" + savedstateitem)
                continue

            sys.stdout.flush()
            if saveitem.Aborted:  # Kill it
                os.unlink(SAVEDSTATE_DIR + "/" + savedstateitem)
            elif not saveitem.BuildDone:
                # Need to clean up build dir
                try:
                    shutil.rmtree(fsconfig["outputs"] + "/" + str(saveitem.buildnr))
                except OSError:
                    pass  # Ok if it's not there
                saveitem.recovering = True
            elif (
                saveitem.BuildError
                or (saveitem.InitialTestingError and saveitem.InitialTestingDone)
                or (saveitem.TestingError and saveitem.TestingDone)
            ):
                pass  # just insert for final notify
            elif saveitem.InitialTestingStarted and not saveitem.InitialTestingDone:
                # To reinsert it we just need to unmark initial testing started
                saveitem.InitialTestingStarted = False
            elif saveitem.TestingStarted and not saveitem.TestingDone:
                # Same here
                saveitem.TestingStarted = False

            saveitem.Reviewer = reviewer  # Since it cannot be saved otherwise
            saveitem.fsconfig = fsconfig

            WorkList.append(saveitem)
            managing_condition.acquire()
            managing_queue.put(saveitem)
            managing_condition.notify()
            managing_condition.release()

    # Now load last 100 entries from the done list
    for savedstateitem in sorted(
        os.listdir(DONEWITH_DIR),
        key=lambda x: int(x.replace("-", ".").split(".")[0]),
        reverse=True,
    ):
        with open(DONEWITH_DIR + "/" + savedstateitem, "rb") as blah:
            try:
                saveitem = pickle.load(blah)
            except:
                continue  # ignore bad items
        if not saveitem.buildnr:
            continue
        if saveitem.artifactsdir:
            savelink = (
                '<a href="'
                + saveitem.artifactsdir.replace(fsconfig["root_path_offset"], "")
                + "/"
                + saveitem.get_results_filename()
                + '">'
            )
        else:
            savelink = ""
        doneitem = {}
        doneitem["build"] = savelink + str(saveitem.buildnr) + "</a>"

        if saveitem.change.get("subject"):
            doneitem["subject"] = savelink + saveitem.change["subject"] + "</a>"
        else:
            doneitem["subject"] = savelink + saveitem.change["id"] + "</a>"

        try:
            doneitem["status"] = saveitem.get_current_text_status()
        except:
            doneitem["status"] = "Too old to know state"

        DoneList.insert(0, doneitem)
        if len(DoneList) == 100:
            break

    print_WorkList_to_HTML()

    try:
        if GERRIT_CHANGE_NUMBER:
            print("Asking for single change " + GERRIT_CHANGE_NUMBER)
            reviewer.update_single_change(GERRIT_CHANGE_NUMBER)

            time.sleep(3)
            while WorkList:
                # Just hang in here until done
                managerthread.join(1)
        else:
            reviewer.run()
    except KeyboardInterrupt:
        StopMachine = True
        print_WorkList_to_HTML()
        sys.exit(1)
