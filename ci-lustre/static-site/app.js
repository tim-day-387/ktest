// SPDX-License-Identifier: GPL-2.0-only

//
// Copyright (c) 2025, Amazon and/or its affiliates. All rights reserved.
// Use is subject to license terms.
//
// Author: Timothy Day <timday@amazon.com>
//

// Main application object
const app = {
  metadata: null,
  gerritChanges: null,
  homeRows: [],
  homeAuthors: [],
  currentView: "home",

  // Profile presets: selecting one loads the filter controls,
  // which can then be adjusted by hand. Author names must match
  // the Gerrit display name exactly.
  profileConfig: {
    Patches: { kind: "run" },
    Branches: { kind: "branch" },
    TimNeedReview: {
      authors: ["Timothy Day", "Tim Day"],
      needReview: true,
    },
    GroupNeedReview: {
      authors: [
        "Timothy Day",
        "Tim Day",
        "Arshad Hussain",
        "James Simmons",
        "Neil Brown",
        "Shaun Tancheff",
      ],
      needReview: true,
    },
    All: {},
  },

  // Initialize the application
  async init() {
    try {
      await this.loadMetadata();
      // The Gerrit change list is optional; the page still
      // renders test runs without it
      try {
        await this.loadGerritChanges();
      } catch (error) {
        this.gerritChanges = null;
      }
      this.loadVersion();
      this.handleRoute();
      // Listen for hash changes
      window.addEventListener("hashchange", () => this.handleRoute());
    } catch (error) {
      this.showError("Failed to initialize application: " + error.message);
    }
  },

  // Load a resource as text. Prefers the embedded snapshot from
  // local_data.js, which only exists in file:// builds made by
  // make-local-site, where the browser blocks fetch().
  async loadResource(path) {
    if (window.LOCAL_DATA && path in window.LOCAL_DATA) {
      return window.LOCAL_DATA[path];
    }
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error("Failed to load " + path);
    }
    return await response.text();
  },

  // Load and display git version info from version.json
  async loadVersion() {
    try {
      const version = JSON.parse(await this.loadResource("version.json"));
      const el = document.getElementById("version-info");
      if (el && version.tag && version.commit) {
        el.textContent = `${version.tag} (${version.commit})`;
      }
    } catch (e) {
      // Version info is optional
    }
  },

  // Get the selected value of a radio filter group
  getRadioValue(name) {
    const radios = document.getElementsByName(name);
    for (let i = 0; i < radios.length; i++) {
      if (radios[i].checked) {
        return radios[i].value;
      }
    }
    return "All";
  },

  // Get row status from table row
  getRowStatus(row) {
    const statusCell = row.querySelector("td:nth-child(5)");
    return statusCell ? statusCell.textContent.trim() : "";
  },

  // Update filter on detail page
  updateFilter() {
    const filterValue = this.getRadioValue("statusfilter");
    const table = document.querySelector(".test-results-table");
    if (!table) return;

    const rows = table.querySelectorAll("tbody tr");
    for (const row of rows) {
      const status = this.getRowStatus(row);
      if (filterValue === "All" || status === filterValue) {
        row.classList.remove("filtered");
      } else {
        row.classList.add("filtered");
      }
    }
  },

  // Update the per-column filters on the home page
  updateHomeFilter() {
    const selectValue = (id) => {
      const el = document.getElementById(id);
      return el ? el.value : "All";
    };

    const kindFilter = selectValue("kindfilter");
    const enforcedFilter = selectValue("enforcedfilter");
    const optionalFilter = selectValue("optionalfilter");
    const gerritFilter = selectValue("gerritfilter");
    const readyFilter = selectValue("readyfilter");

    // No selection, or "All" among the selection, disables the
    // author filter
    const select = document.getElementById("authorfilter");
    let authors = null;
    if (select) {
      const picked = Array.from(select.selectedOptions).map((o) => o.value);
      if (picked.length > 0 && !picked.includes("All")) {
        authors = picked.map((v) => this.homeAuthors[parseInt(v)]);
      }
    }

    const table = document.querySelector(".home-table");
    if (!table) return;

    const needsReview = (info) =>
      info.reviews !== null && info.reviews < 2 && !info.blocked;

    const rows = table.querySelectorAll("tbody tr");
    for (let i = 0; i < rows.length; i++) {
      const info = this.homeRows[i] || {};

      const kindMatch = kindFilter === "All" || info.kind === kindFilter;

      // The Enforced dropdown also carries the tested/untested
      // choices, since untested rows have no result to match
      const enforcedMatch =
        enforcedFilter === "All" ||
        (enforcedFilter === "Tested" && info.tested) ||
        (enforcedFilter === "Untested" && !info.tested) ||
        info.enforced === enforcedFilter;
      const optionalMatch =
        optionalFilter === "All" || info.optional === optionalFilter;
      const gerritMatch =
        gerritFilter === "All" || info.gerrit === gerritFilter;

      // "Not ready" hides ready-to-land changes but keeps
      // everything else, so they can be ignored wholesale. The
      // sub-views split not-ready changes by cause: short on
      // reviews (but not vetoed), or carrying a negative vote
      const readyMatch =
        readyFilter === "All" ||
        (readyFilter === "Ready" && info.ready) ||
        (readyFilter === "NotReady" && !info.ready) ||
        (readyFilter === "NeedReview" && needsReview(info)) ||
        (readyFilter === "Negative" && info.blocked);

      const authorMatch = authors === null || authors.includes(info.author);

      rows[i].classList.toggle(
        "filtered",
        !(kindMatch && enforcedMatch && optionalMatch && gerritMatch && readyMatch && authorMatch),
      );
    }
  },

  // Apply a profile: load its preset into the filter controls,
  // then filter. Manual filter changes build on top from there.
  applyProfile(profile) {
    const cfg = this.profileConfig[profile] || {};

    const setValue = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.value = value;
    };

    setValue("kindfilter", cfg.kind || "All");
    setValue("readyfilter", cfg.needReview ? "NeedReview" : "All");
    setValue("enforcedfilter", "All");
    setValue("optionalfilter", "All");
    setValue("gerritfilter", "All");

    const select = document.getElementById("authorfilter");
    if (select) {
      const authors = cfg.authors || [];
      for (const opt of select.options) {
        opt.selected =
          opt.value === "All"
            ? authors.length === 0
            : authors.includes(this.homeAuthors[parseInt(opt.value)]);
      }
    }

    this.updateHomeFilter();
  },

  // Handle routing based on URL hash
  handleRoute() {
    const hash = window.location.hash;

    if (hash.startsWith("#review/")) {
      const gitHash = hash.substring("#review/".length);
      this.showReviewByGitHash(gitHash);
    } else if (hash === "#status") {
      this.showStatus();
    } else {
      this.showHome();
    }
  },

  // Show review by git hash
  showReviewByGitHash(gitHash) {
    // Check if this git hash exists in metadata
    if (this.metadata && this.metadata[gitHash]) {
      this.showTestDetail(gitHash);
    } else {
      this.showError(`Review with git hash ${gitHash} not found`);
    }
  },

  // Load metadata from JSON file
  async loadMetadata() {
    try {
      this.metadata = JSON.parse(
        await this.loadResource("metadata_store.json"),
      );
    } catch (error) {
      throw new Error("Could not load metadata: " + error.message);
    }
  },

  // Load the open Gerrit change list scraped by 'pk patch-status'
  async loadGerritChanges() {
    if (this.gerritChanges) return;
    this.gerritChanges = JSON.parse(
      await this.loadResource("gerrit_changes.json"),
    );
  },

  // Show error message
  showError(message) {
    const content = document.getElementById("content");
    content.innerHTML = `<div class="error">${this.escapeHtml(message)}</div>`;
  },

  // Show home page: every ktest run and every open Gerrit change
  // in a single table. Runs are joined to changes by revision
  // hash and sorted newest-first; open changes ktest has not run
  // yet follow as Untested rows, smallest-first (the change list
  // is already sorted by patch size).
  showHome() {
    this.currentView = "home";
    window.location.hash = "";
    const content = document.getElementById("content");

    if (!this.metadata) {
      this.showError("No metadata available");
      return;
    }

    const doc = this.gerritChanges;
    const changes = doc ? doc.changes || [] : [];
    const gerrit = doc ? doc.gerrit : "https://review.whamcloud.com";
    const project = doc ? doc.project : "fs/lustre-release";

    const summarize = (results) => {
      if (results.length === 0) return { text: "-", color: "gray" };
      if (results.every((r) => r.rc === 0))
        return { text: "PASS", color: "green" };
      return { text: "FAIL", color: "red" };
    };

    // Get all git hashes with complete metadata, newest first
    const testRuns = Object.keys(this.metadata)
      .map((gitHash) => ({
        gitHash: gitHash,
        data: this.metadata[gitHash],
      }))
      .filter((item) => item.data.time_stamp);

    testRuns.sort(
      (a, b) =>
        parseFloat(b.data.time_stamp || 0) - parseFloat(a.data.time_stamp || 0),
    );

    // Per-patch metadata keys are bare revision hashes, which is
    // what the change list joins on; branch-group keys
    // ("{commitHash}_{group}") never match a change
    const changesByRev = new Map(changes.map((c) => [c.revision, c]));

    const rows = [];

    for (const item of testRuns) {
      rows.push({
        gitHash: item.gitHash,
        data: item.data,
        change: changesByRev.get(item.gitHash) || null,
      });
    }

    for (const c of changes) {
      if (this.metadata[c.revision]) continue;
      rows.push({ gitHash: null, data: null, change: c });
    }

    // Author dropdown options reference authors by index so
    // arbitrary names never end up inside an HTML attribute
    this.homeAuthors = [...new Set(changes.map((c) => c.author))].sort();

    const authorOptions = this.homeAuthors
      .map((a, i) => `<option value="${i}">${this.escapeHtml(a)}</option>`)
      .join("");

    let html = `
            <h1>Testing Status</h1>
            <div id="profiles">
                Profile:
                <label><input type="radio" name="profile" onchange="app.applyProfile(this.value)" value="Patches" checked> Tested patches</label>
                <label><input type="radio" name="profile" onchange="app.applyProfile(this.value)" value="Branches"> Tested branches</label>
                <label title="Timothy Day"><input type="radio" name="profile" onchange="app.applyProfile(this.value)" value="TimNeedReview"> Timothy Day - Need review</label>
                <label title="Timothy Day, Arshad Hussain, James Simmons, Neil Brown, Shaun Tancheff"><input type="radio" name="profile" onchange="app.applyProfile(this.value)" value="GroupNeedReview"> Review group - Need review</label>
                <label><input type="radio" name="profile" onchange="app.applyProfile(this.value)" value="All"> All</label>
            </div>
            <table class="home-table">
                <colgroup>
                    <col style="width: 5%">
                    <col style="width: 25%">
                    <col style="width: 8%">
                    <col style="width: 10%">
                    <col style="width: 6%">
                    <col style="width: 6%">
                    <col style="width: 6%">
                    <col style="width: 12%">
                    <col style="width: 8%">
                    <col style="width: 7%">
                    <col style="width: 7%">
                </colgroup>
                <thead>
                    <tr>
                        <th>Change</th>
                        <th>Subject</th>
                        <th>Hash</th>
                        <th>Author</th>
                        <th>Diff</th>
                        <th>Reviews</th>
                        <th>Maloo</th>
                        <th>Time</th>
                        <th>Runtime</th>
                        <th>Enforced</th>
                        <th>Optional</th>
                    </tr>
                    <tr class="filter-row">
                        <th>
                            <select id="kindfilter" onchange="app.updateHomeFilter()">
                                <option value="All" selected>All</option>
                                <option value="run">Tested patches</option>
                                <option value="branch">Tested branches</option>
                                <option value="untested">Untested</option>
                            </select>
                        </th>
                        <th></th>
                        <th></th>
                        <th>
                            <select id="authorfilter" multiple size="4" title="Ctrl/Cmd-click to select multiple authors" onchange="app.updateHomeFilter()">
                                <option value="All" selected>All</option>
                                ${authorOptions}
                            </select>
                        </th>
                        <th></th>
                        <th>
                            <select id="readyfilter" title="Ready to land: Jenkins + Maloo passed, 2+ reviews, no downvotes" onchange="app.updateHomeFilter()">
                                <option value="All" selected>All</option>
                                <option value="Ready">Ready</option>
                                <option value="NotReady">Not ready</option>
                                <option value="NeedReview">Not ready - Need review</option>
                                <option value="Negative">Not ready - Negative review</option>
                            </select>
                        </th>
                        <th>
                            <select id="gerritfilter" onchange="app.updateHomeFilter()">
                                <option value="All" selected>All</option>
                                <option value="PASS">Passed</option>
                                <option value="FAIL">Failed</option>
                            </select>
                        </th>
                        <th></th>
                        <th></th>
                        <th>
                            <select id="enforcedfilter" onchange="app.updateHomeFilter()">
                                <option value="All" selected>All</option>
                                <option value="PASS">Passed</option>
                                <option value="FAIL">Failed</option>
                                <option value="Tested">Tested</option>
                                <option value="Untested">Untested</option>
                            </select>
                        </th>
                        <th>
                            <select id="optionalfilter" onchange="app.updateHomeFilter()">
                                <option value="All" selected>All</option>
                                <option value="PASS">Passed</option>
                                <option value="FAIL">Failed</option>
                            </select>
                        </th>
                    </tr>
                </thead>
                <tbody>
        `;

    this.homeRows = [];

    for (const r of rows) {
      const change = r.change;
      const data = r.data;

      // Branch-group runs are keyed as "{commitHash}_{group}" so
      // each group gets its own row; strip the suffix for the
      // gitiles commit link
      const commitHash = r.gitHash
        ? r.gitHash.split("_")[0]
        : change.revision;

      let enforcedSummary = { text: "Untested", color: "gray" };
      let optionalSummary = { text: "-", color: "gray" };
      let time = "-";
      let runtime = "-";

      if (data) {
        // Collect all result/enforced metadata
        const results = [];
        for (const key in data) {
          if (key.startsWith("result")) {
            const name = key.substring("result".length);
            const rc = parseInt(data[key]);
            const enforcedKey = "enforced" + name;
            const enforced =
              data[enforcedKey] === "True" || data[enforcedKey] === true;
            results.push({ name, rc: isNaN(rc) ? -1 : rc, enforced });
          }
        }

        enforcedSummary = summarize(results.filter((x) => x.enforced));
        optionalSummary = summarize(results.filter((x) => !x.enforced));

        time = new Date(parseFloat(data.time_stamp) * 1000).toLocaleString(
          "en-US",
          {
            timeZone: "America/New_York",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          },
        );
        runtime = data.total_runtime || "-";
      }

      // Gerrit cells come from the joined change; runs on closed
      // changes fall back to the stored change ID, branch-CI runs
      // have neither
      let changeCell = "-";
      if (change) {
        changeCell = `<a href="${this.escapeHtml(`${gerrit}/${change.number}`)}" target="_blank">${this.escapeHtml(change.number)}</a>`;
      } else if (data && data.change_id) {
        changeCell = `<a href="${this.escapeHtml(`${gerrit}/c/${project}/+/${data.change_id}`)}" target="_blank">${this.escapeHtml(data.change_id)}</a>`;
      }

      const diffCell = change
        ? `+${this.escapeHtml(change.insertions)}/-${this.escapeHtml(change.deletions)}`
        : "-";
      const reviewsCell = change
        ? this.escapeHtml(`${change.reviews}` + (change.blocked ? " -1!" : ""))
        : "-";

      const gerritTest = change ? change.test : "-";
      const gerritColor = { PASS: "green", FAIL: "red" }[gerritTest] || "";

      const subject =
        (data && data.subject) || (change && change.subject) || commitHash;

      // Tested rows link their result cells to the detail page
      const enforcedCell = data
        ? `<a href="#review/${this.escapeHtml(r.gitHash)}" style="color:${enforcedSummary.color};">${enforcedSummary.text}</a>`
        : `<span style="color:${enforcedSummary.color};">${enforcedSummary.text}</span>`;
      const optionalCell = `<span style="color:${optionalSummary.color};">${optionalSummary.text}</span>`;

      // Branch-group runs are keyed "{commitHash}_{group}";
      // per-patch runs are bare revision hashes
      this.homeRows.push({
        kind: !data
          ? "untested"
          : r.gitHash.includes("_")
            ? "branch"
            : "run",
        tested: Boolean(data),
        enforced: enforcedSummary.text,
        optional: data ? optionalSummary.text : "Untested",
        gerrit: gerritTest,
        reviews: change ? change.reviews : null,
        blocked: change ? change.blocked : false,
        // Ready to land: both bots +1 Verified, 2+ positive human
        // reviews, and no negative vote anywhere
        ready: Boolean(
          change &&
            change.test === "PASS" &&
            change.reviews >= 2 &&
            !change.blocked,
        ),
        author: change ? change.author : "-",
      });

      html += `
                <tr>
                    <td>${changeCell}</td>
                    <td>${this.escapeHtml(subject)}</td>
                    <td><a href="${this.escapeHtml(`${gerrit}/plugins/gitiles/${project}/+/${commitHash}`)}" target="_blank">${this.escapeHtml(commitHash.substring(0, 12))}</a></td>
                    <td>${change ? this.escapeHtml(change.author) : "-"}</td>
                    <td>${diffCell}</td>
                    <td>${reviewsCell}</td>
                    <td style="color:${gerritColor};">${this.escapeHtml(gerritTest)}</td>
                    <td>${time}</td>
                    <td>${this.escapeHtml(runtime)}</td>
                    <td>${enforcedCell}</td>
                    <td>${optionalCell}</td>
                </tr>
            `;
    }

    const tested = rows.filter((r) => r.data).length;

    let summaryLine = `${tested} test runs, ${rows.length - tested} untested open changes`;
    if (doc && doc.generated) {
      const scraped = new Date(doc.generated * 1000).toLocaleString("en-US", {
        timeZone: "America/New_York",
      });
      summaryLine += `, scraped ${this.escapeHtml(scraped)}`;
    }

    html += `
                </tbody>
            </table>
            <p>${summaryLine}</p>
        `;

    content.innerHTML = html;

    // Load the default profile into the fresh filter controls
    this.applyProfile(this.getRadioValue("profile"));
  },

  // Show detailed test results for a specific test
  showTestDetail(gitHash) {
    this.currentView = "detail";
    const content = document.getElementById("content");

    const data = this.metadata[gitHash];
    if (!data) {
      this.showError("Test data not found");
      return;
    }

    const subject = data.subject || "Unknown";
    const changeId = data.change_id || "";

    // Collect all result/enforced metadata
    const results = [];
    for (const key in data) {
      if (key.startsWith("result")) {
        const name = key.substring("result".length);
        const rc = parseInt(data[key]);
        const enforcedKey = "enforced" + name;
        const enforced =
          data[enforcedKey] === "True" || data[enforcedKey] === true;
        const runtimeKey = "runtime" + name;
        const runtime = data[runtimeKey] || "-";
        const descriptionKey = "description" + name;
        const description = data[descriptionKey] || `Job: ${name}`;

        // Determine log file path based on gitHash
        const logPath =
          gitHash + "_" + name.replace(/ /g, "_").toLowerCase() + ".log";

        results.push({
          name,
          rc: isNaN(rc) ? -1 : rc,
          enforced,
          runtime,
          description,
          logPath,
        });
      }
    }

    // Generate split view layout
    let leftHtml = `
            <a href="#" onclick="app.showHome(); return false;" class="back-link">← Back to Home</a>
            <h1>${this.escapeHtml(subject)}</h1>
            <div id="filters">
                Filter by (Status):
                <label><input type="radio" name="statusfilter" onchange="app.updateFilter()" value="PASS"> Passed</label>
                <label><input type="radio" name="statusfilter" onchange="app.updateFilter()" value="FAIL"> Failed</label>
                <label><input type="radio" name="statusfilter" onchange="app.updateFilter()" value="All" checked> All</label>
            </div>
        `;

    // Combine all results into one table (no separate sections)
    leftHtml += this.renderResultsTable("", results);

    const rightHtml = `<div class="right-panel empty" id="logPanel">Select a test to view logs</div>`;

    content.innerHTML = `
            <div class="split-view">
                <div class="left-panel">${leftHtml}</div>
                ${rightHtml}
            </div>
        `;
  },

  // Render a table of test results
  renderResultsTable(title, results) {
    const captionHtml = title
      ? `<caption>${this.escapeHtml(title)}</caption>`
      : "";

    let html = `
            <table class="test-results-table" style="margin-bottom: 2em;">
                ${captionHtml}
                <colgroup>
                    <col style="width: 30%;">
                    <col style="width: 30%;">
                    <col style="width: 15%;">
                    <col style="width: 15%;">
                    <col style="width: 10%;">
                </colgroup>
                <thead>
                    <tr>
                        <th>Test</th>
                        <th>Description</th>
                        <th>Type</th>
                        <th>Runtime</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
        `;

    for (const result of results) {
      const status = result.rc === 0 ? "PASS" : "FAIL";
      const color = result.rc === 0 ? "green" : "red";
      const testType = result.enforced ? "Enforced" : "Optional";
      const description = result.description || `Job: ${result.name}`;
      const runtime = result.runtime || "-";

      html += `
                <tr>
                    <td><a href="#" onclick="app.showLog('${this.escapeHtml(result.logPath)}', '${this.escapeHtml(result.name)}'); return false;">${this.escapeHtml(result.name)}</a></td>
                    <td>${this.escapeHtml(description)}</td>
                    <td>${testType}</td>
                    <td>${this.escapeHtml(runtime)}</td>
                    <td style="color:${color};">${status}</td>
                </tr>
            `;
    }

    html += `
                </tbody>
            </table>
        `;

    return html;
  },

  // Show status page
  async showStatus() {
    this.currentView = "status";
    window.location.hash = "status";
    const content = document.getElementById("content");

    content.innerHTML = '<div class="loading">Loading status...</div>';

    try {
      const statusText = await this.loadResource("status.txt");
      content.innerHTML = `<pre>${this.escapeHtml(statusText)}</pre>`;
    } catch (error) {
      this.showError("Failed to load status: " + error.message);
    }
  },

  // Show log in right panel
  async showLog(logPath, testName) {
    const logPanel = document.getElementById("logPanel");
    if (!logPanel) return;

    logPanel.classList.remove("empty");
    logPanel.innerHTML = `
            <div class="log-content">Loading...</div>
        `;

    try {
      const logText = await this.loadResource(logPath);
      const logContent = logPanel.querySelector(".log-content");
      if (logContent) {
        logContent.textContent = logText;
      }
    } catch (error) {
      const logContent = logPanel.querySelector(".log-content");
      if (logContent) {
        logContent.textContent =
          "Error loading log: " + error.message + "\nPath: " + logPath;
      }
    }
  },

  // Escape HTML to prevent XSS
  escapeHtml(text) {
    if (text === null || text === undefined) return "";
    const div = document.createElement("div");
    div.textContent = text.toString();
    return div.innerHTML;
  },
};

// Initialize app when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  app.init();
});
