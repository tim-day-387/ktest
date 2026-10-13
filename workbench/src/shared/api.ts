// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import type {
  BookmarkNode,
  Bounds,
  GerritChangeList,
  MenuItemSpec,
  PaneSpec,
  SpawnRequest,
  SystemStats,
  TabInfo,
  TemplateSpec,
  Workspace
} from "./types";

/** The API the preload script exposes to the renderer as window.workbench. */
export interface WorkbenchApi {
  templates: {
    list(): Promise<TemplateSpec[]>;
  };
  workspaces: {
    list(): Promise<Workspace[]>;
    /** `extraTabs` are URLs opened ahead of the template's own tabs. */
    create(
      template: string,
      vars: Record<string, string>,
      extraTabs?: string[]
    ): Promise<Workspace>;
    /**
     * Resolves to the destroy hook's output if the hook failed: the workspace
     * is then kept, unless `force` is set.
     */
    destroy(id: string, force?: boolean): Promise<string | undefined>;
    panes(id: string): Promise<PaneSpec[]>;
    onChanged(cb: (list: Workspace[]) => void): () => void;
    /** "New Workspace" was picked from a page's context menu. */
    onSpawn(cb: (req: SpawnRequest) => void): () => void;
  };
  gerrit: {
    /** The open Gerrit changes, queried now. */
    changes(): Promise<GerritChangeList>;
  };
  browser: {
    /** Show the workspace's browser (creating its tabs on first show) at the given bounds. */
    show(id: string, bounds: Bounds): Promise<void>;
    hide(): Promise<void>;
    setBounds(bounds: Bounds): Promise<void>;
    /** Navigation acts on the active tab. */
    navigate(id: string, url: string): Promise<void>;
    back(id: string): Promise<void>;
    forward(id: string): Promise<void>;
    reload(id: string): Promise<void>;
    /** Zoom the active tab; Chromium remembers the level per site in the workspace profile. */
    zoom(id: string, action: "in" | "out" | "reset"): Promise<void>;
    newTab(id: string, url?: string): Promise<void>;
    closeTab(id: string, tabId: string): Promise<void>;
    closeActiveTab(id: string): Promise<void>;
    activateTab(id: string, tabId: string): Promise<void>;
    /** Fired whenever a workspace's tab list or any tab's state changes. */
    onTabs(cb: (id: string, tabs: TabInfo[]) => void): () => void;
    /** Keyboard shortcuts captured while a page has focus. */
    onShortcut(cb: (id: string, action: "focus-url") => void): () => void;
    /** A page entered or left its own (HTML) fullscreen; the page then gets the whole window. */
    onHtmlFullscreen(cb: (on: boolean) => void): () => void;
  };
  window: {
    /** Full screen mode: the whole app window fills the screen. */
    setFullscreen(on: boolean): Promise<void>;
    isFullscreen(): Promise<boolean>;
    onFullscreen(cb: (on: boolean) => void): () => void;
  };
  bookmarks: {
    /** Contents of the Firefox bookmarks toolbar folder. */
    toolbar(): Promise<BookmarkNode[]>;
    /** Pop up `nodes` as a native menu at window coordinates (x, y); clicks navigate workspace `id`. */
    popup(id: string, nodes: BookmarkNode[], x: number, y: number): Promise<void>;
  };
  adblock: {
    /** Open uBlock Origin's dashboard in workspace `id`'s browser. Resolves false if uBlock is not loaded. */
    dashboard(id: string): Promise<boolean>;
  };
  cookies: {
    /** Re-import Firefox cookies into workspace `id`'s browser profile and reload it. */
    import(id: string): Promise<{ imported: number; profile: boolean }>;
  };
  menu: {
    /** Pop up a native context menu at the pointer; a click is reported to onAction by item id. */
    popup(items: MenuItemSpec[]): Promise<void>;
    onAction(cb: (id: string) => void): () => void;
  };
  clipboard: {
    read(): Promise<string>;
    write(text: string): Promise<void>;
  };
  system: {
    /** Host uptime, load average and memory snapshot. */
    stats(): Promise<SystemStats>;
  };
  pty: {
    open(wsId: string, pane: string, cols: number, rows: number): Promise<string>;
    write(key: string, data: string): void;
    resize(key: string, cols: number, rows: number): void;
    onData(cb: (key: string, data: string) => void): () => void;
    onExit(cb: (key: string, code: number) => void): () => void;
  };
}

declare global {
  interface Window {
    workbench: WorkbenchApi;
  }
}
