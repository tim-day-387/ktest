# SPDX-License-Identifier: GPL-2.0-only

#
# Build a self-contained copy of the CI status site that works when
# index.html is opened directly in a browser (file://), where fetch()
# is blocked. Copies the static-site files into local-site/ at the top
# of the ktest tree and packs the data files into local_data.js, which
# app.js prefers over fetch() when present.
#
# The output lives in the tree (rather than /tmp) because snap-packaged
# browsers run with a private /tmp and can only read the home directory.
#

import json
import shutil
from pathlib import Path

SITE_FILES = ["index.html", "app.js", "styles.css", "favicon.ico"]
DATA_FILES = [
    "metadata_store.json",
    "status.txt",
    "version.json",
    "gerrit_changes.json",
    "branch_status.json",
    "coverity_status.json",
]


def generate_local_site(ktest_dir, results_dir):
    """Build the local status site from a results directory.

    results_dir holds the files nginx would normally serve
    alongside the site: metadata_store.json (created empty if
    missing) and the per-test .log files, plus optionally
    status.txt, version.json, gerrit_changes.json,
    branch_status.json, and coverity_status.json.
    """
    data_dir = Path(results_dir)
    site_dir = Path(ktest_dir) / "ci-lustre" / "static-site"
    out_dir = Path(ktest_dir) / "local-site"

    # No runs yet (e.g. pk patch-status before any pk job): start
    # an empty store so the site still renders
    metadata_path = data_dir / "metadata_store.json"
    if not metadata_path.is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text("{}\n")

    out_dir.mkdir(exist_ok=True)

    # Pack data files and logs into a script the page can load on file://
    local_data = {}
    for path in [data_dir / f for f in DATA_FILES] + sorted(data_dir.glob("*.log")):
        if path.is_file():
            local_data[path.name] = path.read_text(errors="replace")

    with open(out_dir / "local_data.js", "w") as f:
        f.write("window.LOCAL_DATA = ")
        json.dump(local_data, f)
        f.write(";\n")

    for name in SITE_FILES:
        shutil.copy(site_dir / name, out_dir / name)

    # local_data.js must load before app.js; the root link has nowhere
    # to go on file://, so point it back at the page itself
    index = out_dir / "index.html"
    html = index.read_text()
    html = html.replace(
        '<script src="app.js"></script>',
        '<script src="local_data.js"></script>\n<script src="app.js"></script>',
    )
    html = html.replace('<a href="/">', '<a href="index.html">')
    index.write_text(html)

    logs = len(local_data) - sum(f in local_data for f in DATA_FILES)
    print(f"Packed {logs} logs from {data_dir} into {out_dir}/local_data.js")
    print(f"Open {out_dir}/index.html in a browser")
