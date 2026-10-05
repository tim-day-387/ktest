// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

// Bridges uBlock Origin to Electron's partial Chrome extension support.
// Installed as the first script of uBlock's background.html by
// scripts/fetch-ublock.mjs. Wraps both `chrome` and `browser`, since uBlock
// adopts whichever it finds first.
//
// Electron provides webRequest blocking, runtime messaging with a real
// sender.tab, tabs.executeScript/get/query, and storage.local. It lacks
// browserAction, webNavigation, contextMenus, privacy, windows, the tabs
// events, and tabs.insertCSS/removeCSS. Without tab events uBlock never
// creates a page store for a tab, so cosmetic filtering and scriptlets
// (which is what blocks YouTube ads) silently do nothing. This shim
// synthesizes webNavigation.onCommitted and tabs.onUpdated from content
// script connections, and implements insertCSS/removeCSS via executeScript.
(() => {
  try {
    const real = self.chrome;
    const isEvent = (p) => typeof p === "string" && /^on[A-Z]/.test(p);
    const emitter = () => {
      const listeners = new Set();
      return {
        addListener: (f) => void listeners.add(f),
        removeListener: (f) => void listeners.delete(f),
        hasListener: (f) => listeners.has(f),
        hasListeners: () => listeners.size > 0,
        dispatch: (...args) => {
          for (const f of listeners) {
            try {
              f(...args);
            } catch (e) {
              console.error("electron-shim listener:", e);
            }
          }
        }
      };
    };
    // A callable namespace: usable as a function (invokes a trailing callback
    // with undefined, resolves to undefined) and as an object whose members are
    // further callable namespaces or events.
    const stub = () => {
      const target = function (...args) {
        const cb = args[args.length - 1];
        if (typeof cb === "function") setTimeout(() => cb(undefined), 0);
        return Promise.resolve(undefined);
      };
      const members = new Map();
      return new Proxy(target, {
        get(t, p) {
          if (typeof p !== "string" || p in t) return t[p];
          if (!members.has(p)) members.set(p, isEvent(p) ? emitter() : stub());
          return members.get(p);
        }
      });
    };
    const stubs = {};
    for (const name of ["browserAction", "webNavigation", "contextMenus", "privacy", "windows"])
      stubs[name] = stub();
    stubs.windows.WINDOW_ID_NONE = -1;

    // ---- tabs.insertCSS / removeCSS on top of tabs.executeScript ----
    const cssId = (css) => {
      let h = 5381;
      for (let i = 0; i < css.length; i++) h = ((h << 5) + h + css.charCodeAt(i)) | 0;
      return "ubo-css-" + (h >>> 0).toString(36);
    };
    const insertStyle = (id, css) => {
      if (document.getElementById(id)) return;
      const s = document.createElement("style");
      s.id = id;
      s.textContent = css;
      (document.head || document.documentElement).appendChild(s);
    };
    const removeStyle = (id) => {
      const s = document.getElementById(id);
      if (s) s.remove();
    };
    const runInTab = (tabId, details, fn, id, css) =>
      new Promise((resolve) => {
        if (typeof tabId !== "number" || tabId < 0) return resolve();
        real.tabs.executeScript(
          tabId,
          {
            code: `(${fn})(${JSON.stringify(id)}, ${JSON.stringify(css)})`,
            frameId: details.frameId,
            matchAboutBlank: details.matchAboutBlank,
            runAt: details.runAt || "document_start"
          },
          () => {
            void real.runtime.lastError;
            resolve();
          }
        );
      });
    const overrides = {
      tabs: {
        insertCSS: (tabId, details, cb) =>
          runInTab(tabId, details, insertStyle, cssId(details.code || ""), details.code || "").then(
            cb
          ),
        removeCSS: (tabId, details, cb) =>
          runInTab(tabId, details, removeStyle, cssId(details.code || ""), "").then(cb),
        // Electron rejects tab id -1 (Chrome's "current tab") with a throw; uBlock uses it for behind-the-scene.
        executeScript: (tabId, details, cb) => {
          if (typeof tabId !== "number" || tabId < 0) {
            if (cb) setTimeout(() => cb(undefined), 0);
            return Promise.resolve(undefined);
          }
          return real.tabs.executeScript(tabId, details, cb);
        }
      }
    };
    const FILL = {
      tabs: [
        "create",
        "update",
        "remove",
        "query",
        "get",
        "getCurrent",
        "sendMessage",
        "reload",
        "connect",
        "captureVisibleTab",
        "highlight",
        "discard"
      ],
      storage: ["sync", "managed", "session"],
      runtime: ["getBrowserInfo", "getPlatformInfo", "setUninstallURL", "openOptionsPage"],
      alarms: []
    };
    // Fill missing members of namespaces Electron provides partially: events
    // (cached, so listeners persist), overrides, and the listed methods. All
    // else passes through, notably runtime.lastError, which must stay undefined.
    const fill = (name, obj) => {
      const extra = new Map();
      return new Proxy(obj, {
        get: (t, p) => {
          if (typeof p !== "string") return t[p];
          if (overrides[name] && p in overrides[name]) return overrides[name][p];
          if (t[p] != null) return t[p];
          if (!extra.has(p))
            extra.set(p, isEvent(p) ? emitter() : FILL[name].includes(p) ? stub() : undefined);
          return extra.get(p);
        }
      });
    };
    const filled = new Map();
    const wrapped = new Proxy(real, {
      get(t, p) {
        const v = t[p];
        if (v == null && p in stubs) return stubs[p];
        if (v != null && typeof v === "object" && p in FILL) {
          if (!filled.has(p)) filled.set(p, fill(p, v));
          return filled.get(p);
        }
        return v;
      }
    });
    for (const g of ["chrome", "browser"]) {
      try {
        Object.defineProperty(self, g, { value: wrapped, configurable: true, writable: true });
      } catch {
        self[g] = wrapped;
      }
    }

    // ---- synthesize tab / navigation events from content script connections ----
    // Electron never fires tabs.* or webNavigation.* events, but every content
    // script port carries sender.tab (id = webContents id), frameId and url.
    // Feeding those to uBlock as onCommitted/onUpdated makes it create the tab
    // context and page store, run cosmetic filtering, and inject scriptlets.
    const seenDocs = new Set();
    const seenTabs = new Set();
    const onConnect = (port) => {
      const s = port.sender;
      if (!s || !s.tab || typeof s.tab.id !== "number" || typeof s.frameId !== "number") return;
      if (!/^https?:/.test(s.url || "")) return;
      const key = s.documentId || `${s.tab.id}:${s.frameId}:${s.url}`;
      if (seenDocs.has(key)) return;
      seenDocs.add(key);
      const tab = { ...s.tab, url: s.frameId === 0 ? s.url : s.tab.url };
      if (!seenTabs.has(tab.id)) {
        seenTabs.add(tab.id);
        wrapped.tabs.onCreated.dispatch(tab);
      }
      wrapped.webNavigation.onCommitted.dispatch({
        tabId: tab.id,
        frameId: s.frameId,
        parentFrameId: s.frameId === 0 ? -1 : 0,
        url: s.url,
        transitionType: "link",
        transitionQualifiers: [],
        timeStamp: Date.now(),
        processId: 0,
        documentId: s.documentId
      });
      if (s.frameId === 0)
        wrapped.tabs.onUpdated.dispatch(tab.id, { status: "loading", url: s.url }, tab);
    };
    real.runtime.onConnect.addListener(onConnect); // registered first, so it runs before uBlock's handler
  } catch (e) {
    console.error("electron-shim failed: " + e.message);
  }
})();
