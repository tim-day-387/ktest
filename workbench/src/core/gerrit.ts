// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import type { GerritChange, GerritChangeList } from "../shared/types";

// The open change list of the ktest status site, queried live. This follows
// `pk patch-status` (podman_ktest/commands/patch_status.py), which writes the
// site's gerrit_changes.json: keep the two in step, the filter shared with
// the site (ci-lustre/static-site/gerrit-filter.js) reads these rows.

const GERRIT = "https://review.whamcloud.com";
const PROJECT = "fs/lustre-release";
const BRANCH = "master";

/** Bots expected to post +1 Verified before a patch can land. */
const BOTS = ["jenkins", "maloo"];

const MAX_PAGES = 20;

interface Vote {
  value?: number;
  name?: string;
  username?: string;
  tags?: string[];
}

interface RawChange {
  _number: number;
  _more_changes?: boolean;
  subject?: string;
  owner?: { name?: string };
  insertions?: number;
  deletions?: number;
  labels?: Record<string, { all?: Vote[] }>;
}

async function queryPage(query: string, start: number): Promise<RawChange[]> {
  const params = new URLSearchParams([
    ["q", query],
    ["o", "DETAILED_LABELS"],
    ["o", "DETAILED_ACCOUNTS"],
    ["S", String(start)]
  ]);
  const res = await fetch(`${GERRIT}/changes/?${params}`, { signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`Gerrit query failed: ${res.status} ${res.statusText}`);
  // Gerrit prefixes its JSON with )]}' against script inclusion.
  return JSON.parse((await res.text()).replace(/^\)\]\}'\n/, "")) as RawChange[];
}

const isBot = (v: Vote) => v.tags?.includes("SERVICE_USER") ?? false;
const voter = (v: Vote) => v.username ?? v.name ?? "";

/** Humans voting Code-Review +1 or better, and whether anyone voted negative on any label. */
function reviewCount(change: RawChange): { reviews: number; blocked: boolean } {
  const humans = new Set<string>();
  let blocked = false;
  for (const [name, label] of Object.entries(change.labels ?? {})) {
    for (const v of label.all ?? []) {
      const value = v.value ?? 0;
      if (value < 0) blocked = true;
      if (name === "Code-Review" && value >= 1 && !isBot(v)) humans.add(voter(v));
    }
  }
  return { reviews: humans.size, blocked };
}

/** FAIL on any negative bot vote, PASS once every expected bot voted +1, else the count so far. */
function testStatus(change: RawChange): string {
  const passed = new Set<string>();
  for (const v of change.labels?.["Verified"]?.all ?? []) {
    if (!isBot(v)) continue;
    const value = v.value ?? 0;
    if (value < 0) return "FAIL";
    if (value >= 1) passed.add(voter(v));
  }
  const count = BOTS.filter((b) => passed.has(b)).length;
  return count === BOTS.length ? "PASS" : `${count}/${BOTS.length}`;
}

function toRow(change: RawChange): GerritChange {
  const insertions = change.insertions ?? 0;
  const deletions = change.deletions ?? 0;
  return {
    number: change._number,
    subject: change.subject ?? "",
    author: change.owner?.name ?? "?",
    ...reviewCount(change),
    test: testStatus(change),
    insertions,
    deletions,
    size: insertions + deletions
  };
}

/** Every open change of the branch, smallest patch first. */
export async function fetchGerritChanges(): Promise<GerritChangeList> {
  const query = `status:open project:${PROJECT} branch:${BRANCH}`;
  const raw: RawChange[] = [];
  // Gerrit caps a response and flags its last entry when there is more.
  for (let page = 0; page < MAX_PAGES; page++) {
    const batch = await queryPage(query, raw.length);
    raw.push(...batch);
    if (!batch.at(-1)?._more_changes) break;
  }
  return {
    gerrit: GERRIT,
    project: PROJECT,
    generated: Math.floor(Date.now() / 1000),
    changes: raw.map(toRow).sort((a, b) => a.size - b.size || a.number - b.number)
  };
}
