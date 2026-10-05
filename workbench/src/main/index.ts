// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import {
  app,
  BrowserWindow,
  clipboard,
  ipcMain,
  Menu,
  session,
  shell,
  type Session
} from "electron";
import { readdir, rm } from "node:fs/promises";
import path from "node:path";
import { fetchGerritChanges } from "../core/gerrit";
import { WorkspaceManager } from "../core/manager";
import { resourcesDir } from "./resources";
import type { BookmarkNode, Bounds, MenuItemSpec, SpawnRequest } from "../shared/types";
import { bookmarkUrls, popupBookmarkMenu, toolbarBookmarks } from "./bookmarks";
import { adblockDashboardUrl, loadAdblock } from "./adblock";
import { BrowserHost, type Shortcut } from "./browser";
import { popupPageMenu, popupShellMenu } from "./contextmenu";
import { importFirefoxCookies } from "./cookies";
import { PtyHost } from "./pty";
import { systemStats } from "./sysinfo";

// Data lives in ~/.config/workbench for the installed app. The development
// build uses ~/.config/workbench-dev so the two never fight over one store.
app.setPath(
  "userData",
  path.join(app.getPath("appData"), app.isPackaged ? "workbench" : "workbench-dev")
);

// Templates ship with the app and can be overridden per user in
// <userData>/templates. Workspaces live under the same config dir.
const userData = app.getPath("userData");
const manager = new WorkspaceManager(
  [path.join(resourcesDir(), "templates"), path.join(userData, "templates")],
  path.join(userData, "workspaces")
);

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1400,
    height: 900,
    autoHideMenuBar: true,
    backgroundColor: "#dddddd",
    webPreferences: {
      preload: path.join(__dirname, "../preload/index.js"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  // The shell UI must never zoom: the browser view is positioned from its CSS
  // coordinates. Chromium persists zoom per host and re-applies it on load, so
  // reset after every load (which also overwrites the stored level).
  win.webContents.setVisualZoomLevelLimits(1, 1);
  const resetZoom = () => {
    if (win.webContents.getZoomLevel() !== 0) win.webContents.setZoomLevel(0);
  };
  win.webContents.on("did-finish-load", resetZoom);
  win.webContents.on("did-navigate", resetZoom);
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });
  if (process.env["ELECTRON_RENDERER_URL"]) {
    void win.loadURL(process.env["ELECTRON_RENDERER_URL"]);
  } else {
    void win.loadFile(path.join(__dirname, "../renderer/index.html"));
  }
  return win;
}

/** Resolve what the user typed in the URL bar: a URL as-is, anything else as an https host. */
function toUrl(text: string): string {
  return /^(?:[a-z][a-z0-9+.-]*:\/\/|about:|data:)/i.test(text) ? text : `https://${text}`;
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      { label: "File", submenu: [{ role: "quit" }] },
      { label: "View", submenu: [{ role: "toggleDevTools" }] }
    ])
  );
  await manager.init();
  await removeOrphanedPartitions();
  const win = createWindow();
  const send = (channel: string, ...args: unknown[]) => {
    if (!win.isDestroyed()) win.webContents.send(channel, ...args);
  };

  // Full screen mode is plain window fullscreen: the whole app fills the screen.
  const setFullscreen = (on: boolean) => {
    if (win.isFullScreen() !== on) win.setFullScreen(on);
  };
  win.on("enter-full-screen", () => send("window:fullscreen", true));
  win.on("leave-full-screen", () => send("window:fullscreen", false));
  ipcMain.handle("window:fullscreen", (_e, on: boolean) => setFullscreen(on));
  ipcMain.handle("window:is-fullscreen", () => win.isFullScreen());

  // Tab state goes to the renderer, and open URLs are persisted (debounced) so they restore next time.
  const tabSaveTimers = new Map<string, NodeJS.Timeout>();
  const shortcuts: Record<Shortcut, (wsId: string) => void> = {
    "new-tab": (wsId) => browser.newTab(wsId),
    "close-tab": (wsId) => browser.closeActiveTab(wsId),
    "next-tab": (wsId) => browser.step(wsId, 1),
    "prev-tab": (wsId) => browser.step(wsId, -1),
    "focus-url": (wsId) => {
      win.webContents.focus();
      send("browser:shortcut", wsId, "focus-url");
    },
    "toggle-fullscreen": () => setFullscreen(!win.isFullScreen()),
    "exit-fullscreen": () => setFullscreen(false)
  };
  const browser = new BrowserHost(
    win,
    (wsId, tabs) => {
      send("browser:tabs", wsId, tabs);
      clearTimeout(tabSaveTimers.get(wsId));
      tabSaveTimers.set(
        wsId,
        setTimeout(() => void manager.setTabs(wsId, browser.urls(wsId)).catch(() => {}), 1000)
      );
    },
    (wsId, action) => shortcuts[action](wsId),
    (_wsId, on) => {
      // A page's own fullscreen (video players): Chromium puts the window into
      // fullscreen and restores it by itself. Calling setFullScreen while that
      // transition is in flight corrupts the window's restore bounds, so only
      // tell the renderer to give the page the whole window meanwhile.
      send("browser:html-fullscreen", on);
    },
    (wsId, wc, params) =>
      popupPageMenu(win, wc, params, {
        templates: () => manager.listTemplates(),
        newTab: (url, activate) => browser.newTab(wsId, url, activate),
        // The renderer creates the workspace: it may have to ask for the remaining variables.
        spawn: (req: SpawnRequest) => {
          win.webContents.focus();
          send("workspaces:spawn", req);
        }
      })
  );
  const ptys = new PtyHost(
    (key, data) => send("pty:data", key, data),
    (key, code) => send("pty:exit", key, code)
  );

  manager.on("changed", (list) => send("workspaces:changed", list));

  // --- templates & workspaces ---
  ipcMain.handle("templates:list", async () => {
    await manager.reloadTemplates();
    return manager.listTemplates();
  });
  ipcMain.handle("workspaces:list", () => manager.list());
  ipcMain.handle(
    "workspaces:create",
    (_e, template: string, vars: Record<string, string>, extraTabs?: string[]) =>
      manager.create(template, vars, extraTabs)
  );
  ipcMain.handle("workspaces:destroy", async (_e, id: string, force?: boolean) => {
    const { partition } = manager.browserSpec(id);
    // Terminals go first so that nothing runs in the workspace under its destroy hook.
    ptys.killWorkspace(id);
    const refusal = await manager.destroy(id, force);
    // The browser profile is only cleared once the workspace is really gone.
    if (refusal === undefined) await browser.destroy(id, partition);
    return refusal;
  });
  ipcMain.handle("workspaces:panes", (_e, id: string) => manager.terminalSpec(id).panes);

  // --- open Gerrit changes (home view) ---
  ipcMain.handle("gerrit:changes", () => fetchGerritChanges());

  // --- embedded browser ---
  // A session partition is set up once per run: uBlock loaded, Firefox cookies
  // imported. The first workspace to open a shared partition decides both.
  const sessions = new Map<string, Promise<Session>>();
  const prepareSession = (partition: string, adblock: boolean, cookies: boolean) => {
    let ready = sessions.get(partition);
    if (!ready) {
      ready = (async () => {
        const ses = session.fromPartition(partition);
        if (adblock) await loadAdblock(ses, partition);
        if (cookies) await importFirefoxCookies(ses);
        return ses;
      })();
      sessions.set(partition, ready);
    }
    return ready;
  };
  // Opening a workspace's tabs takes several awaits; a second show for the
  // same workspace meanwhile must join the first rather than open twice.
  const opening = new Map<string, Promise<void>>();
  const openBrowser = (id: string) => {
    let p = opening.get(id);
    if (!p) {
      p = (async () => {
        const { partition, tabs, bookmarkPaths, importCookies, adblock } = manager.browserSpec(id);
        await prepareSession(partition, adblock, importCookies);
        const saved = manager.get(id).tabs;
        let initial = saved?.length ? saved : tabs;
        if (!saved?.length && bookmarkPaths?.length) {
          const { urls, missing } = await bookmarkUrls(bookmarkPaths).catch(() => ({
            urls: [],
            missing: bookmarkPaths
          }));
          if (missing.length) console.warn(`bookmarks not found: ${missing.join(", ")}`);
          initial = [...tabs, ...urls];
        }
        browser.open(id, partition, initial);
      })().finally(() => opening.delete(id));
      opening.set(id, p);
    }
    return p;
  };
  ipcMain.handle("browser:show", async (_e, id: string, bounds: Bounds) => {
    if (!browser.has(id)) await openBrowser(id);
    browser.show(id, bounds);
    manager.markReady(id);
  });
  ipcMain.handle("adblock:dashboard", (_e, id: string) => {
    const url = adblockDashboardUrl(session.fromPartition(manager.browserSpec(id).partition));
    if (url) void browser.active(id)?.loadURL(url);
    return !!url;
  });
  ipcMain.handle("cookies:import", async (_e, id: string) => {
    const { partition } = manager.browserSpec(id);
    const result = await importFirefoxCookies(session.fromPartition(partition));
    browser.active(id)?.reload();
    return result;
  });
  ipcMain.handle("browser:hide", () => browser.hide());
  ipcMain.handle("browser:bounds", (_e, bounds: Bounds) => browser.setBounds(bounds));
  ipcMain.handle("browser:navigate", (_e, id: string, url: string) =>
    browser
      .active(id)
      ?.loadURL(toUrl(url))
      .catch(() => {})
  );
  ipcMain.handle("browser:back", (_e, id: string) =>
    browser.active(id)?.navigationHistory.goBack()
  );
  ipcMain.handle("browser:forward", (_e, id: string) =>
    browser.active(id)?.navigationHistory.goForward()
  );
  ipcMain.handle("browser:reload", (_e, id: string) => browser.active(id)?.reload());
  ipcMain.handle("browser:zoom", (_e, id: string, action: "in" | "out" | "reset") =>
    browser.zoom(id, action)
  );
  ipcMain.handle("browser:new-tab", (_e, id: string, url?: string) => browser.newTab(id, url));
  ipcMain.handle("browser:close-tab", (_e, id: string, tabId: string) =>
    browser.closeTab(id, tabId)
  );
  ipcMain.handle("browser:close-active-tab", (_e, id: string) => browser.closeActiveTab(id));
  ipcMain.handle("browser:activate-tab", (_e, id: string, tabId: string) =>
    browser.activate(id, tabId)
  );

  // --- Firefox bookmarks toolbar ---
  ipcMain.handle("bookmarks:toolbar", async () => {
    try {
      return await toolbarBookmarks();
    } catch (err) {
      console.warn("could not read Firefox bookmarks:", err);
      return [];
    }
  });
  ipcMain.handle("bookmarks:popup", (_e, id: string, nodes: BookmarkNode[], x: number, y: number) =>
    popupBookmarkMenu(win, nodes, x, y, (url) => void browser.active(id)?.loadURL(url))
  );

  // --- context menus of the shell UI (pages pop up their own, see BrowserHost) ---
  ipcMain.handle("menu:popup", (_e, items: MenuItemSpec[]) =>
    popupShellMenu(win, items, (id) => send("menu:action", id))
  );
  ipcMain.handle("clipboard:read", () => clipboard.readText());
  ipcMain.handle("clipboard:write", (_e, text: string) => clipboard.writeText(text));

  // --- host status (sidebar footer) ---
  ipcMain.handle("system:stats", () => systemStats());

  // --- terminals ---
  ipcMain.handle("pty:open", (_e, wsId: string, pane: string, cols: number, rows: number) => {
    const key = `${wsId}:${pane}`;
    if (!ptys.has(key)) {
      const spec = manager.terminalSpec(wsId);
      const p = spec.panes.find((x) => x.name === pane) ?? { name: pane };
      ptys.open(key, {
        shell: spec.shell,
        cwd: spec.cwd,
        env: spec.env,
        command: p.command,
        cols,
        rows
      });
      manager.markReady(wsId);
    }
    return key;
  });
  ipcMain.on("pty:write", (_e, key: string, data: string) => ptys.write(key, data));
  ipcMain.on("pty:resize", (_e, key: string, cols: number, rows: number) =>
    ptys.resize(key, cols, rows)
  );

  win.on("closed", () => ptys.killAll());
});

/** Destroyed workspaces clear their partition's data but Chromium keeps the directory; drop those. */
async function removeOrphanedPartitions(): Promise<void> {
  const dir = path.join(userData, "Partitions");
  const live = new Set(manager.list().map((ws) => `ws-${ws.id}`));
  let entries: string[] = [];
  try {
    entries = await readdir(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    if (name.startsWith("ws-") && !live.has(name))
      await rm(path.join(dir, name), { recursive: true, force: true }).catch(() => {});
  }
}

app.on("window-all-closed", () => app.quit());
