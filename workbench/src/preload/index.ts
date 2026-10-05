// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { contextBridge, ipcRenderer } from "electron";
import type { WorkbenchApi } from "../shared/api";

function on<T extends unknown[]>(channel: string, cb: (...args: T) => void): () => void {
  const listener = (_e: Electron.IpcRendererEvent, ...args: unknown[]) => cb(...(args as T));
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

const api: WorkbenchApi = {
  templates: {
    list: () => ipcRenderer.invoke("templates:list")
  },
  workspaces: {
    list: () => ipcRenderer.invoke("workspaces:list"),
    create: (template, vars, extraTabs) =>
      ipcRenderer.invoke("workspaces:create", template, vars, extraTabs),
    destroy: (id, force) => ipcRenderer.invoke("workspaces:destroy", id, force),
    panes: (id) => ipcRenderer.invoke("workspaces:panes", id),
    onChanged: (cb) => on("workspaces:changed", cb),
    onSpawn: (cb) => on("workspaces:spawn", cb)
  },
  gerrit: {
    changes: () => ipcRenderer.invoke("gerrit:changes")
  },
  browser: {
    show: (id, bounds) => ipcRenderer.invoke("browser:show", id, bounds),
    hide: () => ipcRenderer.invoke("browser:hide"),
    setBounds: (bounds) => ipcRenderer.invoke("browser:bounds", bounds),
    navigate: (id, url) => ipcRenderer.invoke("browser:navigate", id, url),
    back: (id) => ipcRenderer.invoke("browser:back", id),
    forward: (id) => ipcRenderer.invoke("browser:forward", id),
    reload: (id) => ipcRenderer.invoke("browser:reload", id),
    zoom: (id, action) => ipcRenderer.invoke("browser:zoom", id, action),
    newTab: (id, url) => ipcRenderer.invoke("browser:new-tab", id, url),
    closeTab: (id, tabId) => ipcRenderer.invoke("browser:close-tab", id, tabId),
    closeActiveTab: (id) => ipcRenderer.invoke("browser:close-active-tab", id),
    activateTab: (id, tabId) => ipcRenderer.invoke("browser:activate-tab", id, tabId),
    onTabs: (cb) => on("browser:tabs", cb),
    onShortcut: (cb) => on("browser:shortcut", cb),
    onHtmlFullscreen: (cb) => on("browser:html-fullscreen", cb)
  },
  window: {
    setFullscreen: (on) => ipcRenderer.invoke("window:fullscreen", on),
    isFullscreen: () => ipcRenderer.invoke("window:is-fullscreen"),
    onFullscreen: (cb) => on("window:fullscreen", cb)
  },
  bookmarks: {
    toolbar: () => ipcRenderer.invoke("bookmarks:toolbar"),
    popup: (id, nodes, x, y) => ipcRenderer.invoke("bookmarks:popup", id, nodes, x, y)
  },
  adblock: {
    dashboard: (id) => ipcRenderer.invoke("adblock:dashboard", id)
  },
  cookies: {
    import: (id) => ipcRenderer.invoke("cookies:import", id)
  },
  menu: {
    popup: (items) => ipcRenderer.invoke("menu:popup", items),
    onAction: (cb) => on("menu:action", cb)
  },
  clipboard: {
    read: () => ipcRenderer.invoke("clipboard:read"),
    write: (text) => ipcRenderer.invoke("clipboard:write", text)
  },
  system: {
    stats: () => ipcRenderer.invoke("system:stats")
  },
  pty: {
    open: (wsId, pane, cols, rows) => ipcRenderer.invoke("pty:open", wsId, pane, cols, rows),
    write: (key, data) => ipcRenderer.send("pty:write", key, data),
    resize: (key, cols, rows) => ipcRenderer.send("pty:resize", key, cols, rows),
    onData: (cb) => on("pty:data", cb),
    onExit: (cb) => on("pty:exit", cb)
  }
};

contextBridge.exposeInMainWorld("workbench", api);
