# SPDX-License-Identifier: GPL-2.0-only

#
# Scrape open Gerrit changes, in the spirit of the Whamcloud
# "Patch Status" wiki page. For each change report the human
# review count, bot test status, patch size, author, and current
# revision. Changes are sorted smallest-first by patch size.
#
# The default output is a human-readable table. With --json the
# output is a document consumed by the ci-lustre static site
# (gerrit_changes.json), which joins it against
# metadata_store.json to mark changes as tested or untested by
# ktest.
#

import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..local_site import generate_local_site

GERRIT = "https://review.whamcloud.com"
PROJECT = "fs/lustre-release"
BRANCH = "master"

# Bots expected to post +1 Verified before a patch can land.
BOTS = {"jenkins", "maloo"}

MAX_PAGES = 20


def query_page(gerrit, query, start):
    params = urlencode(
        [
            ("q", query),
            ("o", "DETAILED_LABELS"),
            ("o", "DETAILED_ACCOUNTS"),
            ("o", "CURRENT_REVISION"),
            ("S", start),
        ]
    )

    url = f"{gerrit}/changes/?{params}"

    raw = urlopen(Request(url)).read().decode()

    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1]

    return json.loads(raw)


def query_changes(gerrit, query):
    """Page through Gerrit query results.

    Gerrit caps results per response and sets _more_changes on the
    last entry of a page, so follow up with the S offset.
    """
    changes = []
    start = 0

    for _ in range(MAX_PAGES):
        page = query_page(gerrit, query, start)

        if not page:
            break

        changes.extend(page)

        if not page[-1].get("_more_changes"):
            break

        start += len(page)

    return changes


def fetch_changes(gerrit, project, branch, authors):
    query = f"status:open project:{project} branch:{branch}"

    # Narrow the query server-side when authors are given. An
    # unknown identity makes Gerrit reject the whole query, so
    # fall back to the plain query and rely on the client-side
    # filter below.
    if authors:
        owners = " OR ".join(f'owner:"{a}"' for a in authors)

        try:
            return query_changes(gerrit, f"{query} ({owners})")
        except HTTPError:
            pass

    return query_changes(gerrit, query)


def is_bot(voter):
    return "SERVICE_USER" in voter.get("tags", [])


def owner_matches(change, authors):
    """Case-insensitive substring match of any requested author
    against the owner's name, username, or email.
    """
    if not authors:
        return True

    owner = change.get("owner", {})

    fields = " ".join(
        str(owner.get(k, "")) for k in ("name", "username", "email")
    ).lower()

    return any(a.lower() in fields for a in authors)


def review_count(change):
    """Count humans voting Code-Review >= +1.

    Returns the count and whether anyone (human or bot) voted
    negative on any label.
    """
    humans = set()
    blocked = False

    for name, label in change.get("labels", {}).items():
        for v in label.get("all", []):
            value = v.get("value", 0) or 0

            if value < 0:
                blocked = True

            if name == "Code-Review" and value >= 1 and not is_bot(v):
                humans.add(v.get("username", v.get("name")))

    return len(humans), blocked


def test_status(change):
    """Bot test status from the Verified label.

    FAIL on any negative bot vote, PASS once every expected bot
    voted +1, otherwise the pass count so far (e.g. "1/2").
    """
    passed = set()
    failed = False

    label = change.get("labels", {}).get("Verified", {})

    for v in label.get("all", []):
        if not is_bot(v):
            continue

        value = v.get("value", 0) or 0

        if value < 0:
            failed = True
        elif value >= 1:
            passed.add(v.get("username", v.get("name")))

    if failed:
        return "FAIL"

    if BOTS <= passed:
        return "PASS"

    return f"{len(passed & BOTS)}/{len(BOTS)}"


def build_rows(changes, authors):
    rows = []

    for change in changes:
        if not owner_matches(change, authors):
            continue

        reviews, blocked = review_count(change)

        rows.append(
            {
                "number": change["_number"],
                "subject": change.get("subject", ""),
                "author": change.get("owner", {}).get("name", "?"),
                "revision": change.get("current_revision", ""),
                "reviews": reviews,
                "blocked": blocked,
                "test": test_status(change),
                "insertions": change.get("insertions", 0),
                "deletions": change.get("deletions", 0),
                "size": change.get("insertions", 0) + change.get("deletions", 0),
            }
        )

    rows.sort(key=lambda r: (r["size"], r["number"]))

    return rows


def print_text(rows):
    header = ("Change", "Size", "Diff", "Rev", "Test", "Author", "Subject")

    table = [header] + [
        (
            str(r["number"]),
            str(r["size"]),
            f"+{r['insertions']}/-{r['deletions']}",
            str(r["reviews"]) + (" -1!" if r["blocked"] else ""),
            r["test"],
            r["author"],
            r["subject"],
        )
        for r in rows
    ]

    widths = [max(len(row[i]) for row in table) for i in range(len(header) - 1)]

    for row in table:
        cols = [c.ljust(w) for c, w in zip(row, widths)]
        print("  ".join(cols + [row[-1]]).rstrip())

    print()
    print(f"{len(rows)} changes")


def write_json(rows, gerrit, project, branch, out):
    doc = {
        "gerrit": gerrit,
        "project": project,
        "branch": branch,
        "generated": int(time.time()),
        "changes": rows,
    }

    json.dump(doc, out)
    out.write("\n")


def cmd_patch_status(args, ktest_dir):
    """Scrape open Gerrit changes and print a table or JSON.

    The static-site document (gerrit_changes.json) is always
    written to the results directory, so the local status site and
    any later pk job rebuilds pick up the change list. An --author
    run is a filtered view and leaves the document alone.
    """
    gerrit = args.gerrit.rstrip("/")

    authors = [
        a.strip() for entry in args.author for a in entry.split(",") if a.strip()
    ]

    changes = fetch_changes(gerrit, args.project, args.branch, authors)

    rows = build_rows(changes, authors)

    if not authors:
        results_dir = Path(args.output)
        results_dir.mkdir(parents=True, exist_ok=True)

        with open(results_dir / "gerrit_changes.json", "w") as f:
            write_json(rows, gerrit, args.project, args.branch, f)

    if args.json:
        write_json(rows, gerrit, args.project, args.branch, sys.stdout)
        return 0

    print_text(rows)

    if authors:
        return 0

    print()
    print(f"wrote {results_dir / 'gerrit_changes.json'}")

    # Refresh the local status site if there are results to pack
    generate_local_site(ktest_dir, results_dir)

    return 0
