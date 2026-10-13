// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import {
  BrowserWindow,
  WebContentsView,
  session,
  type ContextMenuParams,
  type WebContents
} from "electron";
import type { Bounds, TabInfo } from "../shared/types";

interface Tab {
  tabId: string;
  wsId: string;
  view: WebContentsView;
  /** URL still to be loaded (never loaded, load failed, or renderer died). */
  pendingUrl?: string;
}

export type Shortcut =
  | "new-tab"
  | "close-tab"
  | "next-tab"
  | "prev-tab"
  | "focus-url"
  | "toggle-fullscreen"
  | "exit-fullscreen";

/** Ctrl+<key> shortcuts forwarded to the shell; Ctrl+Tab, zoom and reload are handled inline. */
const CTRL_SHORTCUTS: Record<string, Shortcut | undefined> = {
  t: "new-tab",
  w: "close-tab",
  l: "focus-url"
};

/**
 * Tabs for every workspace, each a WebContentsView in the workspace's session
 * partition. All views stay attached to the window; exactly one is visible:
 * the active tab of the shown workspace. Detaching and re-attaching views can
 * leave them blank, so visibility is toggled instead.
 */
export class BrowserHost {
  private tabs = new Map<string, Tab>();
  private order = new Map<string, string[]>(); // wsId -> tabIds in strip order
  private activeTab = new Map<string, string>(); // wsId -> active tabId
  private partitions = new Map<string, string>(); // wsId -> partition
  private shownWs?: string;
  private bounds: Bounds = { x: 0, y: 0, width: 0, height: 0 };
  private seq = 0;

  constructor(
    private win: BrowserWindow,
    private onTabs: (wsId: string, tabs: TabInfo[]) => void,
    private onShortcut: (wsId: string, action: Shortcut) => void,
    /** A page entered or left HTML fullscreen (e.g. a video player's own button). */
    private onHtmlFullscreen: (on: boolean) => void,
    /** A page was right-clicked. */
    private onContextMenu: (wsId: string, wc: WebContents, params: ContextMenuParams) => void
  ) {}

  has(wsId: string): boolean {
    return this.order.has(wsId);
  }

  /** Create the initial tabs of a workspace; the first one becomes active. */
  open(wsId: string, partition: string, urls: string[]): void {
    this.partitions.set(wsId, partition);
    this.order.set(wsId, []);
    for (const [i, url] of (urls.length ? urls : ["about:blank"]).entries())
      this.newTab(wsId, url, i === 0);
  }

  newTab(wsId: string, url = "about:blank", activate = true): void {
    const partition = this.partitions.get(wsId);
    if (partition === undefined) throw new Error(`workspace ${wsId} has no browser`);
    const tabId = `${wsId}#${++this.seq}`;
    const view = new WebContentsView({
      webPreferences: { partition, sandbox: true, contextIsolation: true, nodeIntegration: false }
    });
    const tab: Tab = { tabId, wsId, view, pendingUrl: url };
    const wc = view.webContents;
    wc.setWindowOpenHandler(({ url: target }) => {
      this.newTab(wsId, target, true);
      return { action: "deny" };
    });
    const changed = () => this.emit(wsId);
    wc.on("did-navigate", changed);
    wc.on("did-navigate-in-page", changed);
    wc.on("page-title-updated", changed);
    wc.on("did-start-loading", changed);
    wc.on("did-stop-loading", changed);
    wc.on("did-finish-load", () => {
      tab.pendingUrl = undefined;
    });
    wc.on("did-fail-load", (_e, code, _desc, failedUrl, isMainFrame) => {
      if (isMainFrame && code !== -3 /* ERR_ABORTED */) tab.pendingUrl = failedUrl;
    });
    wc.on("render-process-gone", () => {
      tab.pendingUrl = wc.getURL() || tab.pendingUrl || "about:blank";
    });
    let htmlFullscreen = false;
    wc.on("enter-html-full-screen", () => {
      htmlFullscreen = true;
      this.onHtmlFullscreen(true);
    });
    wc.on("leave-html-full-screen", () => {
      htmlFullscreen = false;
      this.onHtmlFullscreen(false);
    });
    wc.on("context-menu", (_e, params) => this.onContextMenu(wsId, wc, params));
    wc.on("before-input-event", (e, input) => {
      if (input.type !== "keyDown" || input.alt || input.meta) return;
      if (input.key === "F11" || input.key === "Escape") {
        if (input.key === "Escape" && htmlFullscreen) return; // Chromium exits HTML fullscreen itself
        if (input.key === "F11") e.preventDefault();
        this.onShortcut(wsId, input.key === "F11" ? "toggle-fullscreen" : "exit-fullscreen");
        return;
      }
      if (!input.control) return;
      const key = input.key.toLowerCase();
      if (key === "=" || key === "+" || key === "-" || key === "0") {
        e.preventDefault();
        this.zoom(wsId, key === "0" ? "reset" : key === "-" ? "out" : "in");
        return;
      }
      if (key === "r") {
        e.preventDefault();
        wc.reload();
        return;
      }
      const action = key === "tab" ? (input.shift ? "prev-tab" : "next-tab") : CTRL_SHORTCUTS[key];
      if (!action) return;
      e.preventDefault();
      this.onShortcut(wsId, action);
    });
    view.setVisible(false);
    this.win.contentView.addChildView(view);
    this.tabs.set(tabId, tab);
    this.order.get(wsId)!.push(tabId);
    if (activate || !this.activeTab.has(wsId)) this.activate(wsId, tabId);
    else this.emit(wsId);
  }

  closeTab(wsId: string, tabId: string): void {
    const ids = this.order.get(wsId);
    const tab = this.tabs.get(tabId);
    if (!ids || !tab || tab.wsId !== wsId) return;
    const idx = ids.indexOf(tabId);
    ids.splice(idx, 1);
    this.tabs.delete(tabId);
    this.win.contentView.removeChildView(tab.view);
    tab.view.webContents.close();
    if (ids.length === 0) {
      this.activeTab.delete(wsId);
      this.newTab(wsId, "about:blank", true); // a workspace always has one tab
      return;
    }
    if (this.activeTab.get(wsId) === tabId) this.activate(wsId, ids[Math.min(idx, ids.length - 1)]);
    else this.emit(wsId);
  }

  closeActiveTab(wsId: string): void {
    const tabId = this.activeTab.get(wsId);
    if (tabId !== undefined) this.closeTab(wsId, tabId);
  }

  activate(wsId: string, tabId: string): void {
    const tab = this.tabs.get(tabId);
    if (!tab || tab.wsId !== wsId) return;
    this.activeTab.set(wsId, tabId);
    this.refresh();
    this.loadPending(tab);
    this.emit(wsId);
  }

  /** Zoom the active tab. Chromium keeps the level per site within the partition. */
  zoom(wsId: string, action: "in" | "out" | "reset"): void {
    const wc = this.active(wsId);
    if (!wc) return;
    const level = action === "reset" ? 0 : wc.getZoomLevel() + (action === "out" ? -1 : 1);
    wc.setZoomLevel(Math.min(8, Math.max(-6, level)));
  }

  /** Cycle to the neighbouring tab. */
  step(wsId: string, delta: 1 | -1): void {
    const ids = this.order.get(wsId);
    const cur = this.activeTab.get(wsId);
    if (!ids?.length || cur === undefined) return;
    this.activate(wsId, ids[(ids.indexOf(cur) + delta + ids.length) % ids.length]);
  }

  show(wsId: string, bounds: Bounds): void {
    this.shownWs = wsId;
    this.bounds = bounds;
    this.refresh();
    const active = this.activeTab.get(wsId);
    const tab = active !== undefined ? this.tabs.get(active) : undefined;
    if (tab) this.loadPending(tab);
    this.emit(wsId);
  }

  hide(): void {
    this.shownWs = undefined;
    this.refresh();
  }

  setBounds(bounds: Bounds): void {
    this.bounds = bounds;
    this.refresh();
  }

  /** Web contents of the workspace's active tab. */
  active(wsId: string): WebContents | undefined {
    const id = this.activeTab.get(wsId);
    return id !== undefined ? this.tabs.get(id)?.view.webContents : undefined;
  }

  list(wsId: string): TabInfo[] {
    const active = this.activeTab.get(wsId);
    return (this.order.get(wsId) ?? []).map((tabId) => {
      const tab = this.tabs.get(tabId)!;
      const wc = tab.view.webContents;
      return {
        tabId,
        url: wc.getURL() || tab.pendingUrl || "about:blank", // restored tabs load lazily on activation
        title: wc.getTitle(),
        loading: wc.isLoading(),
        canGoBack: wc.navigationHistory.canGoBack(),
        canGoForward: wc.navigationHistory.canGoForward(),
        active: tabId === active
      };
    });
  }

  /** URLs of the workspace's tabs, for persistence. */
  urls(wsId: string): string[] {
    return (this.order.get(wsId) ?? []).map((tabId) => {
      const tab = this.tabs.get(tabId)!;
      return tab.view.webContents.getURL() || tab.pendingUrl || "about:blank";
    });
  }

  async destroy(wsId: string, partition: string): Promise<void> {
    if (this.shownWs === wsId) this.hide();
    for (const tabId of this.order.get(wsId) ?? []) {
      const tab = this.tabs.get(tabId)!;
      this.win.contentView.removeChildView(tab.view);
      tab.view.webContents.close();
      this.tabs.delete(tabId);
    }
    this.order.delete(wsId);
    this.activeTab.delete(wsId);
    this.partitions.delete(wsId);
    if (partition.startsWith("persist:ws-"))
      await session.fromPartition(partition).clearStorageData();
  }

  /** Make exactly the shown workspace's active tab visible, at the current bounds. */
  private refresh(): void {
    const visible = this.shownWs !== undefined ? this.activeTab.get(this.shownWs) : undefined;
    for (const [tabId, tab] of this.tabs) {
      const on = tabId === visible;
      if (on) tab.view.setBounds(this.bounds);
      tab.view.setVisible(on);
    }
  }

  private loadPending(tab: Tab): void {
    if (tab.pendingUrl !== undefined && !tab.view.webContents.isLoading()) {
      const url = tab.pendingUrl;
      tab.pendingUrl = undefined;
      void tab.view.webContents.loadURL(url).catch(() => {
        tab.pendingUrl = url;
      });
    }
  }

  private emit(wsId: string): void {
    if (this.order.has(wsId)) this.onTabs(wsId, this.list(wsId));
  }
}
