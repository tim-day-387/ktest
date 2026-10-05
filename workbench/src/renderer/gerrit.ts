// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

// The Gerrit spawner of the home view: the open changes, filtered
// like the home table of the ktest status site, each becoming a workspace.

import "../../../ci-lustre/static-site/gerrit-filter.js";
import type {
  GerritChange,
  GerritChangeList,
  SpawnContext,
  TemplateSpec,
  Workspace
} from "../shared/types";
import { prefillVars, spawnResolved } from "../shared/spawn";
import "../shared/api";

interface Profile {
  label: string;
  /** Selects ktest runs, which only the status site has. */
  kind?: string;
  authors?: string[];
  needReview?: boolean;
}

/** The filter shared with the status site (ci-lustre/static-site/gerrit-filter.js). */
const filter = (
  globalThis as unknown as {
    GerritFilter: {
      profiles: Record<string, Profile>;
      readyOptions: [string, string][];
      readyTitle: string;
      testOptions: [string, string][];
      matches(
        change: GerritChange | null,
        filter: { ready?: string; test?: string; authors?: string[] | null }
      ): boolean;
    };
  }
).GerritFilter;

export interface GerritSpawnHost {
  templates(): TemplateSpec[];
  workspaces(): Workspace[];
  /** Workspaces were created, all without error. */
  created(ids: string[]): void;
}

interface Row {
  change: GerritChange;
  tr: HTMLTableRowElement;
  check: HTMLInputElement;
}

const api = window.workbench;
const $ = <T extends HTMLElement>(sel: string) => document.querySelector<T>(sel)!;

export function initGerritSpawn(host: GerritSpawnHost) {
  let doc: GerritChangeList | undefined;
  let rows: Row[] = [];
  let authors: string[] = [];
  let loading = false;
  let creating = false;

  const templateSelect = $<HTMLSelectElement>("#gerrit-template");
  const authorSelect = $<HTMLSelectElement>("#gerrit-author");
  const readySelect = $<HTMLSelectElement>("#gerrit-ready");
  const testSelect = $<HTMLSelectElement>("#gerrit-test");
  const allCheck = $<HTMLInputElement>("#gerrit-all");
  const createButton = $<HTMLButtonElement>("#gerrit-create");
  const status = $("#gerrit-status");
  const error = $("#gerrit-error");

  const option = (value: string, label: string) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    return o;
  };

  // A change is spawned like a right-clicked link to it: its subject is the text.
  const context = (list: GerritChangeList, c: Pick<GerritChange, "number" | "subject">) =>
    ({
      text: c.subject,
      url: `${list.gerrit}/c/${list.project}/+/${c.number}`
    }) as Required<SpawnContext>;

  const template = () => host.templates().find((t) => t.name === templateSelect.value);

  /** The workspace `vars` describe, if there already is one: same template, same filled-in values. */
  const existing = (t: TemplateSpec, vars: Record<string, string>) =>
    host
      .workspaces()
      .find(
        (ws) => ws.template === t.name && Object.entries(vars).every(([k, v]) => ws.vars[k] === v)
      );

  const listed = () => rows.filter((r) => !r.tr.classList.contains("filtered"));
  const picked = () => listed().filter((r) => r.check.checked);

  function updateCount(): void {
    const n = picked().length;
    const shown = listed();
    allCheck.checked = shown.length > 0 && n === shown.length;
    allCheck.indeterminate = n > 0 && n < shown.length;
    createButton.textContent = `Create ${n} workspace${n === 1 ? "" : "s"}`;
    createButton.disabled = creating || n === 0 || !template();
    if (creating || !doc) return;
    const scraped = new Date(doc.generated * 1000).toLocaleTimeString();
    status.textContent = `${shown.length} of ${rows.length} open changes listed, as of ${scraped}`;
  }

  function applyFilter(): void {
    // No selection, or "All" among the selection, disables the author filter.
    const chosen = [...authorSelect.selectedOptions].map((o) => o.value);
    const by =
      chosen.length > 0 && !chosen.includes("All") ? chosen.map((v) => authors[Number(v)]) : null;
    for (const r of rows)
      r.tr.classList.toggle(
        "filtered",
        !filter.matches(r.change, {
          ready: readySelect.value,
          test: testSelect.value,
          authors: by
        })
      );
    updateCount();
  }

  /** Load a profile's preset into the filter controls; manual changes build on it. */
  function applyProfile(name: string): void {
    const cfg = filter.profiles[name] ?? { label: name };
    readySelect.value = cfg.needReview ? "NeedReview" : "All";
    testSelect.value = "All";
    const names = cfg.authors ?? [];
    for (const o of authorSelect.options)
      o.selected =
        o.value === "All" ? names.length === 0 : names.includes(authors[Number(o.value)]);
    applyFilter();
  }

  /** Name the workspace each change already has; `recheck` also selects the changes that have none. */
  function markExisting(recheck: boolean): void {
    const t = template();
    for (const r of rows) {
      const ws = t && doc ? existing(t, prefillVars(t, context(doc, r.change))) : undefined;
      r.tr.querySelector(".workspace")!.textContent = ws?.name ?? "";
      if (recheck) r.check.checked = !ws;
    }
    updateCount();
  }

  function renderTemplates(list: GerritChangeList): void {
    // Templates a change fills in completely, the ones taking the most from it first.
    const sample = context(list, { number: 1, subject: "LU-1 sample" });
    const usable = host
      .templates()
      .map((t) => ({ t, vars: prefillVars(t, sample) }))
      .filter(({ t, vars }) => Object.keys(vars).length > 0 && spawnResolved(t, vars))
      .sort((a, b) => Object.keys(b.vars).length - Object.keys(a.vars).length);
    const before = templateSelect.value;
    templateSelect.replaceChildren(...usable.map(({ t }) => option(t.name, t.name)));
    if (usable.some(({ t }) => t.name === before)) templateSelect.value = before;
  }

  function renderRows(list: GerritChangeList): void {
    authors = [...new Set(list.changes.map((c) => c.author))].sort();
    authorSelect.replaceChildren(
      option("All", "All"),
      ...authors.map((a, i) => option(String(i), a))
    );
    rows = list.changes.map((c) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<td><input type="checkbox" /></td><td><a target="_blank"></a></td><td class="subject"></td>' +
        '<td class="author"></td><td class="diff"></td><td class="reviews"></td>' +
        '<td class="test"></td><td class="workspace"></td>';
      const link = tr.querySelector("a")!;
      link.href = context(list, c).url;
      link.textContent = String(c.number);
      tr.querySelector(".subject")!.textContent = c.subject;
      tr.querySelector(".author")!.textContent = c.author;
      tr.querySelector(".diff")!.textContent = `+${c.insertions}/-${c.deletions}`;
      tr.querySelector(".reviews")!.textContent = `${c.reviews}${c.blocked ? " -1!" : ""}`;
      const test = tr.querySelector(".test")!;
      test.textContent = c.test;
      test.classList.toggle("pass", c.test === "PASS");
      test.classList.toggle("fail", c.test === "FAIL");
      const check = tr.querySelector("input")!;
      check.onchange = updateCount;
      return { change: c, tr, check };
    });
    $("#gerrit-table tbody").replaceChildren(...rows.map((r) => r.tr));
  }

  async function load(): Promise<void> {
    if (loading || creating) return;
    loading = true;
    error.textContent = "";
    status.textContent = "Querying Gerrit…";
    try {
      const list = await api.gerrit.changes();
      doc = list;
      renderTemplates(list);
      renderRows(list);
      markExisting(true);
      applyProfile(
        document.querySelector<HTMLInputElement>("#gerrit-profiles input:checked")?.value ?? "All"
      );
      if (!template()) error.textContent = "No template can be filled in from a Gerrit change.";
    } catch (err) {
      status.textContent = "";
      error.textContent = `Could not list the open changes: ${(err as Error).message ?? err}`;
    }
    loading = false;
  }

  async function create(): Promise<void> {
    const t = template();
    const list = doc;
    const todo = picked();
    if (!t || !list || todo.length === 0 || creating) return;
    if (
      todo.length > 1 &&
      !confirm(`Create ${todo.length} "${t.name}" workspaces, one per selected change?`)
    )
      return;
    creating = true;
    error.textContent = "";
    updateCount();
    const ids: string[] = [];
    const errors: string[] = [];
    // One at a time: create hooks may share a resource, such as the git repo of a worktree.
    for (const [i, r] of todo.entries()) {
      status.textContent = `Creating ${i + 1} of ${todo.length}: ${r.change.number} ${r.change.subject}`;
      const ctx = context(list, r.change);
      try {
        const ws = await api.workspaces.create(t.name, prefillVars(t, ctx), [ctx.url]);
        ids.push(ws.id);
        // A failed create hook leaves the workspace, marked failed, for inspection.
        if (ws.state === "failed") errors.push(`${r.change.number}: ${ws.error ?? "failed"}`);
        r.check.checked = false;
      } catch (err) {
        errors.push(`${r.change.number}: ${(err as Error).message ?? err}`);
      }
    }
    creating = false;
    markExisting(false);
    error.textContent = errors.join("\n");
    if (ids.length > 0 && errors.length === 0) host.created(ids);
  }

  // The site's profiles that select by Gerrit state alone; the first is the default.
  const profiles = Object.entries(filter.profiles).filter(([, cfg]) => !cfg.kind);
  $("#gerrit-profiles").replaceChildren(
    "Profile:",
    ...profiles.map(([name, cfg], i) => {
      const label = document.createElement("label");
      label.title = (cfg.authors ?? []).join(", ");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "gerrit-profile";
      radio.value = name;
      radio.checked = i === 0;
      radio.onchange = () => applyProfile(name);
      label.append(radio, ` ${cfg.label}`);
      return label;
    })
  );
  readySelect.title = filter.readyTitle;
  readySelect.replaceChildren(...filter.readyOptions.map(([v, l]) => option(v, l)));
  testSelect.replaceChildren(...filter.testOptions.map(([v, l]) => option(v, l)));
  for (const el of [authorSelect, readySelect, testSelect]) el.onchange = applyFilter;
  templateSelect.onchange = () => markExisting(true);
  allCheck.onchange = () => {
    for (const r of listed()) r.check.checked = allCheck.checked;
    updateCount();
  };
  $("#gerrit-refresh").onclick = () => void load();
  createButton.onclick = () => void create();

  return {
    /** The home view came up: list the changes, once. */
    show(): void {
      if (!doc) void load();
    },
    workspacesChanged(): void {
      if (doc && !creating) markExisting(false);
    }
  };
}
