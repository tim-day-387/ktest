// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { EventEmitter } from "node:events";
import { randomBytes } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import type { PaneSpec, TemplateSpec, Workspace } from "../shared/types";
import { runHook } from "./hooks";
import { WorkspaceStore } from "./store";
import { prefillVars } from "../shared/spawn";
import {
  loadTemplates,
  render,
  renderRecord,
  resolveVars,
  usesEmptyVar,
  varsUsed
} from "./template";
import { serializeTask } from "./task";

export interface TerminalSpec {
  shell: string;
  cwd: string;
  env: Record<string, string>;
  panes: PaneSpec[];
}

export interface BrowserSpec {
  partition: string;
  /** Template tabs; may be empty, in which case the browser host opens about:blank. */
  tabs: string[];
  /** Firefox toolbar bookmark paths (folders or bookmarks) to open as tabs; resolved by the main process. */
  bookmarkPaths?: string[];
  importCookies: boolean;
  adblock: boolean;
}

export interface ManagerEvents {
  changed: [Workspace[]];
}

/** Session partition per browser profile kind. */
const PARTITION: Record<"isolated" | "shared" | "ephemeral", (id: string) => string> = {
  isolated: (id) => `persist:ws-${id}`,
  shared: () => "persist:shared",
  ephemeral: (id) => `ws-${id}`
};

const hostOf = (url: string) => {
  try {
    return new URL(url).hostname;
  } catch {
    return undefined;
  }
};

/**
 * True when `url` is another address of what template tab `tab` opens: it is
 * on the tab's host and fills every variable the tab uses as the tab's own
 * address does. A link to a patchset or a file of a Gerrit change is the change's tab.
 */
function sameTarget(
  spec: TemplateSpec,
  tab: string,
  url: string,
  vars: Record<string, string>
): boolean {
  const rendered = render(tab, vars);
  if (rendered === url) return true;
  const used = varsUsed(tab);
  const host = hostOf(rendered);
  if (used.length === 0 || host === undefined || host !== hostOf(url)) return false;
  // Compared as the addresses fill them: a change given as 68716/2 is change 68716 in both.
  const filled = prefillVars(spec, { url });
  const own = prefillVars(spec, { url: rendered });
  return used.every((name) => filled[name] !== undefined && filled[name] === own[name]);
}

/**
 * Owns templates and the workspace lifecycle. Knows nothing about Electron;
 * the main process subscribes to its events to manage browser views and ptys.
 */
export class WorkspaceManager extends EventEmitter<ManagerEvents> {
  private templates = new Map<string, TemplateSpec>();
  private workspaces = new Map<string, Workspace>();
  private store: WorkspaceStore;

  constructor(
    private templateDirs: string[],
    workspacesRoot: string
  ) {
    super();
    this.store = new WorkspaceStore(workspacesRoot);
  }

  async init(): Promise<void> {
    await this.reloadTemplates();
    for (const ws of await this.store.loadAll()) {
      // Anything that was mid-transition when the app died is left for the user to destroy.
      if (ws.state === "creating" || ws.state === "destroying") ws.state = "failed";
      if (ws.state === "ready") ws.state = "suspended";
      this.workspaces.set(ws.id, ws);
    }
    this.emitChanged();
  }

  async reloadTemplates(): Promise<void> {
    this.templates = await loadTemplates(this.templateDirs);
  }

  /** Templates marked `default` come first, so every picker and menu offers them first. */
  listTemplates(): TemplateSpec[] {
    const all = [...this.templates.values()];
    return [...all.filter((t) => t.default), ...all.filter((t) => !t.default)];
  }

  list(): Workspace[] {
    return [...this.workspaces.values()];
  }

  get(id: string): Workspace {
    const ws = this.workspaces.get(id);
    if (!ws) throw new Error(`no such workspace: ${id}`);
    return ws;
  }

  template(ws: Workspace): TemplateSpec {
    return this.templates.get(ws.template) ?? { name: ws.template };
  }

  /** `extraTabs` are URLs to open ahead of the template's own tabs (a right-clicked link). */
  async create(
    templateName: string,
    given: Record<string, string>,
    extraTabs?: string[]
  ): Promise<Workspace> {
    const spec = this.templates.get(templateName);
    if (!spec) throw new Error(`no such template: ${templateName}`);

    const id = `${templateName}-${randomBytes(3).toString("hex")}`;
    const dir = this.store.dirFor(id);
    const userVars = resolveVars(spec, given);
    const vars = {
      ...userVars,
      workspace_id: id,
      workspace_dir: dir,
      task_file: this.store.taskFile(id)
    };
    const ws: Workspace = {
      id,
      name: render(spec.task?.title ?? spec.name, vars).trim(),
      template: templateName,
      state: "creating",
      createdAt: new Date().toISOString(),
      dir,
      vars,
      extraTabs
    };
    this.workspaces.set(id, ws);
    this.emitChanged();

    // Any failure past this point leaves a "failed" workspace the user can inspect and destroy.
    try {
      await mkdir(dir, { recursive: true });
      await writeFile(
        this.store.taskFile(id),
        serializeTask({
          meta: {
            title: ws.name,
            status: "open",
            template: templateName,
            created: ws.createdAt,
            vars: userVars
          },
          body: render(spec.task?.body ?? "", vars)
        })
      );
      await this.store.save(ws);
      if (spec.hooks?.create) {
        const res = await runHook(render(spec.hooks.create, vars), {
          cwd: dir,
          env: this.hookEnv(ws)
        });
        if (res.code !== 0)
          throw new Error(`create hook exited ${res.code}:\n${res.output.slice(-2000)}`);
      }
      ws.state = "ready";
    } catch (err) {
      ws.state = "failed";
      ws.error = (err as Error).message;
    }
    await this.store.save(ws);
    this.emitChanged();
    return ws;
  }

  /**
   * Run the destroy hook and remove the workspace. A failing hook refuses the
   * destroy: the workspace is kept and the hook's output returned, so that a
   * hook can protect what it could not save. `force` removes it regardless.
   */
  async destroy(id: string, force = false): Promise<string | undefined> {
    const ws = this.get(id);
    // A second hook must not run against a workspace whose create or destroy hook still is.
    if (ws.state === "creating" || ws.state === "destroying")
      throw new Error(`workspace "${ws.name}" is still ${ws.state}`);
    const prev = ws.state;
    ws.state = "destroying";
    this.emitChanged();
    const spec = this.template(ws);
    if (spec.hooks?.destroy) {
      const res = await runHook(render(spec.hooks.destroy, ws.vars), {
        cwd: ws.dir,
        env: this.hookEnv(ws)
      });
      if (res.code !== 0 && !force) {
        ws.state = prev;
        this.emitChanged();
        return `destroy hook exited ${res.code}:\n${res.output.slice(-2000)}`;
      }
      if (res.code !== 0) console.warn(`[${id}] destroy hook exited ${res.code}:\n${res.output}`);
    }
    await this.store.remove(id);
    this.workspaces.delete(id);
    this.emitChanged();
    return undefined;
  }

  markReady(id: string): void {
    const ws = this.get(id);
    if (ws.state === "suspended") {
      ws.state = "ready";
      this.emitChanged();
    }
  }

  /** Remember the workspace's open tabs (no change event: the list UI does not show them). */
  async setTabs(id: string, urls: string[]): Promise<void> {
    const ws = this.workspaces.get(id);
    if (!ws || JSON.stringify(ws.tabs) === JSON.stringify(urls)) return;
    ws.tabs = urls;
    await this.store.save(ws);
  }

  terminalSpec(id: string): TerminalSpec {
    const ws = this.get(id);
    const t = this.template(ws).terminal ?? {};
    return {
      shell: t.shell ?? process.env.SHELL ?? "/bin/bash",
      // Terminals start in the home directory unless the template says otherwise; `~` is expanded.
      cwd: render(t.cwd ?? "~", ws.vars).replace(/^~(?=\/|$)/, os.homedir()),
      env: { ...this.hookEnv(ws), ...renderRecord(t.env, ws.vars) },
      panes: (t.panes?.length ? t.panes : [{ name: "shell" }]).map((p) => ({
        name: p.name,
        command: p.command ? render(p.command, ws.vars) : undefined
      }))
    };
  }

  browserSpec(id: string): BrowserSpec {
    const ws = this.get(id);
    const spec = this.template(ws);
    const b = spec.browser ?? {};
    const extra = ws.extraTabs ?? [];
    return {
      partition: PARTITION[b.profile ?? "isolated"](id),
      // A template tab is dropped when the link the workspace was spawned from already
      // shows it. A tab that uses an empty variable is dropped too: there is nothing to
      // point it at.
      tabs: [
        ...new Set([
          ...extra,
          ...(b.tabs ?? [])
            .filter((u) => !usesEmptyVar(u, ws.vars))
            .filter((u) => !extra.some((url) => sameTarget(spec, u, url, ws.vars)))
            .map((u) => render(u, ws.vars))
        ])
      ],
      bookmarkPaths: b.bookmarks === undefined ? undefined : [b.bookmarks].flat(),
      importCookies: b.cookies !== "none",
      adblock: b.adblock !== false
    };
  }

  /** The contract between the app and anything it runs: three env vars. */
  private hookEnv(ws: Workspace): Record<string, string> {
    return {
      WORKSPACE_ID: ws.id,
      WORKSPACE_DIR: ws.dir,
      TASK_FILE: this.store.taskFile(ws.id)
    };
  }

  private emitChanged(): void {
    this.emit("changed", this.list());
  }
}
