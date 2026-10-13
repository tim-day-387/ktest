// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

// Filters over the open Gerrit change list: the rows of
// gerrit_changes.json, as scraped by 'pk patch-status'. Shared by
// the status site, which loads this as a plain script ahead of
// app.js, and by workbench, which bundles it into its spawn
// dashboard. Hence one global and no imports or exports.
globalThis.GerritFilter = {
  // Profile presets: selecting one loads the filter controls,
  // which can then be adjusted by hand. Author names must match
  // the Gerrit display name exactly. A profile with a kind selects
  // ktest runs, which only the status site has.
  profiles: {
    Patches: { label: "Tested patches", kind: "run" },
    Branches: { label: "Tested branches", kind: "branch" },
    TimNeedReview: {
      label: "Timothy Day - Need review",
      authors: ["Timothy Day", "Tim Day"],
      needReview: true,
    },
    GroupNeedReview: {
      label: "Review group - Need review",
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
    All: { label: "All" },
  },

  // [value, label] choices of the ready-to-land filter
  readyOptions: [
    ["All", "All"],
    ["Ready", "Ready"],
    ["NotReady", "Not ready"],
    ["NeedReview", "Not ready - Need review"],
    ["Need1Review", "Not ready - Need 1 review"],
    ["Need2Reviews", "Not ready - Need 2 reviews"],
    ["Negative", "Not ready - Negative review"],
  ],
  readyTitle: "Ready to land: Jenkins + Maloo passed, 2+ reviews, no downvotes",

  // [value, label] choices of the bot test (Maloo) filter
  testOptions: [
    ["All", "All"],
    ["PASS", "Passed"],
    ["FAIL", "Failed"],
  ],

  // Ready to land: both bots +1 Verified, 2+ positive human
  // reviews, and no negative vote anywhere
  isReady(change) {
    return Boolean(
      change && change.test === "PASS" && change.reviews >= 2 && !change.blocked,
    );
  },

  // Short on reviews, but not vetoed
  needsReview(change) {
    return Boolean(change && change.reviews < 2 && !change.blocked);
  },

  // How many of the 2 reviews are still missing
  reviewsNeeded(change) {
    return change ? Math.max(0, 2 - change.reviews) : 0;
  },

  // Whether a change passes the filter { ready, test, authors }:
  // a readyOptions value, a testOptions value, and a list of author
  // names or null for any author. The change is null for a row
  // without one, e.g. a ktest run on a closed change.
  matches(change, filter) {
    const ready = filter.ready || "All";
    const test = filter.test || "All";

    // "Not ready" hides ready-to-land changes but keeps
    // everything else, so they can be ignored wholesale. The
    // sub-views split not-ready changes by cause: short on
    // reviews (but not vetoed), by either count or exactly one or
    // two, or carrying a negative vote
    const readyMatch =
      ready === "All" ||
      (ready === "Ready" && this.isReady(change)) ||
      (ready === "NotReady" && !this.isReady(change)) ||
      (ready === "NeedReview" && this.needsReview(change)) ||
      (ready === "Need1Review" &&
        this.needsReview(change) &&
        this.reviewsNeeded(change) === 1) ||
      (ready === "Need2Reviews" &&
        this.needsReview(change) &&
        this.reviewsNeeded(change) === 2) ||
      (ready === "Negative" && Boolean(change && change.blocked));

    const testMatch = test === "All" || (change ? change.test : "-") === test;

    const authorMatch =
      !filter.authors ||
      Boolean(change && filter.authors.includes(change.author));

    return readyMatch && testMatch && authorMatch;
  },
};
