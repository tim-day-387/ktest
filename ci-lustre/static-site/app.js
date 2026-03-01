// SPDX-License-Identifier: GPL-2.0

//
// Copyright (c) 2025, Amazon and/or its affiliates. All rights reserved.
// Use is subject to license terms.
//
// Author: Timothy Day <timday@amazon.com>
//

// Main application object
const app = {
  metadata: null,
  currentView: "home",

  // Initialize the application
  async init() {
    try {
      await this.loadMetadata();
      this.loadVersion();
      this.handleRoute();
      // Listen for hash changes
      window.addEventListener("hashchange", () => this.handleRoute());
    } catch (error) {
      this.showError("Failed to initialize application: " + error.message);
    }
  },

  // Load and display git version info from version.json
  async loadVersion() {
    try {
      const response = await fetch("version.json");
      if (!response.ok) return;
      const version = await response.json();
      const el = document.getElementById("version-info");
      if (el && version.tag && version.commit) {
        el.textContent = `${version.tag} (${version.commit})`;
      }
    } catch (e) {
      // Version info is optional
    }
  },

  // Get selected filter value
  getSelectedFilter() {
    const radios = document.getElementsByName("statusfilter");
    for (let i = 0; i < radios.length; i++) {
      if (radios[i].checked) {
        return radios[i].value;
      }
    }
    return "All";
  },

  // Get selected filter value for home page enforced tests
  getSelectedEnforcedFilter() {
    const radios = document.getElementsByName("enforcedfilter");
    for (let i = 0; i < radios.length; i++) {
      if (radios[i].checked) {
        return radios[i].value;
      }
    }
    return "All";
  },

  // Get selected filter value for home page optional tests
  getSelectedOptionalFilter() {
    const radios = document.getElementsByName("optionalfilter");
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

  // Get row enforced status from home page table row
  getRowEnforcedStatus(row) {
    const statusCell = row.querySelector("td:nth-child(7)");
    return statusCell ? statusCell.textContent.trim() : "";
  },

  // Get row optional status from home page table row
  getRowOptionalStatus(row) {
    const statusCell = row.querySelector("td:nth-child(8)");
    return statusCell ? statusCell.textContent.trim() : "";
  },

  // Update filter on detail page
  updateFilter() {
    const filterValue = this.getSelectedFilter();
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

  // Update filter on home page
  updateHomeFilter() {
    const enforcedFilter = this.getSelectedEnforcedFilter();
    const optionalFilter = this.getSelectedOptionalFilter();
    const table = document.querySelector("table");
    if (!table) return;

    const rows = table.querySelectorAll("tbody tr");
    for (const row of rows) {
      const enforcedStatus = this.getRowEnforcedStatus(row);
      const optionalStatus = this.getRowOptionalStatus(row);

      const enforcedMatch = enforcedFilter === "All" || enforcedStatus === enforcedFilter;
      const optionalMatch = optionalFilter === "All" || optionalStatus === optionalFilter;

      if (enforcedMatch && optionalMatch) {
        row.classList.remove("filtered");
      } else {
        row.classList.add("filtered");
      }
    }
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
      const response = await fetch("metadata_store.json");
      if (!response.ok) {
        throw new Error("Failed to load metadata_store.json");
      }
      this.metadata = await response.json();
    } catch (error) {
      throw new Error("Could not load metadata: " + error.message);
    }
  },

  // Show error message
  showError(message) {
    const content = document.getElementById("content");
    content.innerHTML = `<div class="error">${this.escapeHtml(message)}</div>`;
  },

  // Show home page (testing status)
  showHome() {
    this.currentView = "home";
    window.location.hash = "";
    const content = document.getElementById("content");

    if (!this.metadata) {
      this.showError("No metadata available");
      return;
    }

    // Get all git hashes with complete metadata
    const testRuns = Object.keys(this.metadata)
      .map((gitHash) => ({
        gitHash: gitHash,
        data: this.metadata[gitHash],
      }))
      .filter(
        (item) =>
          item.data.patch_revision &&
          item.data.change_id &&
          item.data.subject &&
          item.data.time_stamp,
      );

    // Sort by timestamp (newest first)
    testRuns.sort(
      (a, b) =>
        parseFloat(b.data.time_stamp || 0) - parseFloat(a.data.time_stamp || 0),
    );

    // Build HTML
    let html = `
            <h1>Testing Status</h1>
            <div id="filters">
                <div>
                    Filter by (Enforced):
                    <label><input type="radio" name="enforcedfilter" onchange="app.updateHomeFilter()" value="PASS"> Passed</label>
                    <label><input type="radio" name="enforcedfilter" onchange="app.updateHomeFilter()" value="FAIL"> Failed</label>
                    <label><input type="radio" name="enforcedfilter" onchange="app.updateHomeFilter()" value="All" checked> All</label>
                </div>
                <div>
                    Filter by (Optional):
                    <label><input type="radio" name="optionalfilter" onchange="app.updateHomeFilter()" value="PASS"> Passed</label>
                    <label><input type="radio" name="optionalfilter" onchange="app.updateHomeFilter()" value="FAIL"> Failed</label>
                    <label><input type="radio" name="optionalfilter" onchange="app.updateHomeFilter()" value="All" checked> All</label>
                </div>
            </div>
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
        `;

    for (const item of testRuns) {
      const data = item.data;
      const gitHash = item.gitHash;
      const timestamp = parseFloat(data.time_stamp);
      const date = new Date(timestamp * 1000);
      const readable = date.toLocaleString("en-US", {
        timeZone: "America/New_York",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });

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

      // Compute PASS/FAIL for enforced and optional
      const enforcedResults = results.filter((r) => r.enforced);
      const optionalResults = results.filter((r) => !r.enforced);

      const summarize = (results) => {
        if (results.length === 0) return { text: "N/A", color: "gray" };
        if (results.every((r) => r.rc === 0))
          return { text: "PASS", color: "green" };
        return { text: "FAIL", color: "red" };
      };

      const enforcedSummary = summarize(enforcedResults);
      const optionalSummary = summarize(optionalResults);

      html += `
                <tr>
                    <td><a href="#review/${this.escapeHtml(gitHash)}">Link</a></td>
                    <td>${this.escapeHtml(data.subject)}</td>
                    <td><a href="https://review.whamcloud.com/plugins/gitiles/fs/lustre-release/+/${this.escapeHtml(gitHash)}" target="_blank">${this.escapeHtml(gitHash)}</a></td>
                    <td><a href="https://review.whamcloud.com/c/fs/lustre-release/+/${this.escapeHtml(data.change_id)}" target="_blank">${this.escapeHtml(data.change_id)}</a></td>
                    <td>${readable}</td>
                    <td>${this.escapeHtml(data.total_runtime || "N/A")}</td>
                    <td style="color:${enforcedSummary.color};">${enforcedSummary.text}</td>
                    <td style="color:${optionalSummary.color};">${optionalSummary.text}</td>
                </tr>
            `;
    }

    html += `
                </tbody>
            </table>
        `;

    content.innerHTML = html;
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
        const runtime = data[runtimeKey] || "N/A";
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
      const runtime = result.runtime || "N/A";

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
      const response = await fetch("status.txt");
      if (!response.ok) {
        throw new Error("Failed to load status");
      }
      const statusText = await response.text();
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
      const response = await fetch(logPath);
      if (!response.ok) {
        throw new Error("Failed to load log file");
      }
      const logText = await response.text();
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
