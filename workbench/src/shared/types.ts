// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

export interface VariableSpec {
  prompt: string;
  default?: string;
  pattern?: string;
  /** May be left empty: the variable is then the empty string. */
  optional?: boolean;
  /**
   * Right-click spawn: fill this variable from the clicked context, either its
   * text (selection, link text, page title) or its URL. A variable with a
   * pattern is matched against both without asking.
   */
  from?: "text" | "url";
  /**
   * Right-click spawn: regex to search the context with instead of `pattern`;
   * the value is its first capture group, or the whole match without one.
   */
  match?: string;
}

export interface PaneSpec {
  name: string;
  command?: string;
}

export interface TemplateSpec {
  name: string;
  description?: string;
  /** Listed first: the pick among templates that the same context fills in equally well. */
  default?: boolean;
  variables?: Record<string, VariableSpec>;
  task?: { title?: string; body?: string };
  browser?: {
    profile?: "isolated" | "shared" | "ephemeral";
    tabs?: string[];
    /**
     * Firefox toolbar bookmarks to open as tabs: a folder name (all its bookmarks,
     * recursively) or a list of paths like "TLC/Outlook" (a bookmark) or "TLC" (a folder).
     */
    bookmarks?: string | string[];
    /** Import Firefox cookies into the workspace's browser profile (default: firefox). */
    cookies?: "firefox" | "none";
    /** Load uBlock Origin into the workspace's browser profile (default: true). */
    adblock?: boolean;
  };
  terminal?: { shell?: string; cwd?: string; env?: Record<string, string>; panes?: PaneSpec[] };
  hooks?: { create?: string; destroy?: string };
}

export type WorkspaceState = "creating" | "ready" | "suspended" | "destroying" | "failed";

export interface Workspace {
  id: string;
  name: string;
  template: string;
  state: WorkspaceState;
  createdAt: string;
  dir: string;
  /** Template variables plus workspace_id / workspace_dir / task_file. */
  vars: Record<string, string>;
  error?: string;
  /** URLs of the browser tabs that were open, restored on next open. */
  tabs?: string[];
  /** URLs the workspace was spawned with, opened ahead of the template's tabs. */
  extraTabs?: string[];
}

/** What a right-click spawn was launched from: a selection, a link, a page or a tab. */
export interface SpawnContext {
  text?: string;
  url?: string;
}

/** A workspace to create from a context menu: variables prefilled from the context, URL to open. */
export interface SpawnRequest {
  template: string;
  vars: Record<string, string>;
  url?: string;
}

/**
 * An open Gerrit change: a row of the ktest status site's gerrit_changes.json
 * (podman_ktest/commands/patch_status.py).
 */
export interface GerritChange {
  number: number;
  subject: string;
  /** Display name of the owner. */
  author: string;
  revision: string;
  /** Humans voting Code-Review +1 or better. */
  reviews: number;
  /** Someone, human or bot, voted negative on some label. */
  blocked: boolean;
  /** Bot verification: "PASS", "FAIL", or the pass count so far, e.g. "1/2". */
  test: string;
  insertions: number;
  deletions: number;
  size: number;
}

/** The open changes of a Gerrit project branch, smallest first. */
export interface GerritChangeList {
  gerrit: string;
  project: string;
  branch: string;
  /** When the list was scraped, in seconds since the epoch. */
  generated: number;
  changes: GerritChange[];
}

/** One entry of a native context menu the renderer asks the main process to pop up. */
export interface MenuItemSpec {
  /** Reported through menu.onAction when the item is clicked. */
  id?: string;
  label?: string;
  enabled?: boolean;
  /** Standard edit action on the focused field, instead of reporting a click. */
  role?: "undo" | "redo" | "cut" | "copy" | "paste" | "selectAll";
  type?: "separator";
  submenu?: MenuItemSpec[];
}

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TabInfo {
  tabId: string;
  url: string;
  title: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  active: boolean;
}

export interface BookmarkNode {
  title: string;
  url?: string;
  children?: BookmarkNode[];
}

/** A top-style snapshot of the host: uptime in seconds, 1/5/15 min load, memory in bytes. */
export interface SystemStats {
  uptime: number;
  load: [number, number, number];
  mem: { total: number; free: number; available: number };
}
