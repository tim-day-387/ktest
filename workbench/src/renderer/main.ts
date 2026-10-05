// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebglAddon } from "@xterm/addon-webgl";
import "@xterm/xterm/css/xterm.css";
import type {
  BookmarkNode,
  Bounds,
  MenuItemSpec,
  SpawnContext,
  SpawnRequest,
  TabInfo,
  TemplateSpec,
  Workspace
} from "../shared/types";
import { spawnChoices, spawnResolved } from "../shared/spawn";
import { initGerritSpawn } from "./gerrit";
import "../shared/api";

const api = window.workbench;
const $ = <T extends HTMLElement>(sel: string) => document.querySelector<T>(sel)!;

// ---------- state ----------
let templates: TemplateSpec[] = [];
let workspaces: Workspace[] = [];
let activeId: string | undefined;
let activePane: string | undefined;

interface Pane {
  key: string;
  el: HTMLDivElement;
  term: Terminal;
  fit: FitAddon;
}
const panes = new Map<string, Pane>(); // key -> pane

/** The message of an error thrown by the main process, without Electron's IPC prefix. */
const errorText = (err: unknown) =>
  String((err as Error).message ?? err).replace(
    /^Error invoking remote method '[^']+': Error: /,
    ""
  );

// ---------- context menus ----------
// Menus are native (the browser view is a native layer above the page, so an
// HTML menu could not overlap it): the main process pops them up and reports
// the clicked item back by id.
type MenuItem = Omit<MenuItemSpec, "id" | "submenu"> & { click?: () => void; submenu?: MenuItem[] };
const SEP: MenuItem = { type: "separator" };
const EDIT_ITEMS: MenuItem[] = [
  { role: "undo" },
  { role: "redo" },
  SEP,
  { role: "cut" },
  { role: "copy" },
  { role: "paste" },
  { role: "selectAll" }
];
const isWeb = (url: string | undefined): url is string => /^https?:\/\//i.test(url ?? "");

let menuActions = new Map<string, () => void>(); // item id -> click, for the menu last popped up
let menuSeq = 0;

function popupMenu(items: MenuItem[]): void {
  if (items.length === 0) return;
  menuActions = new Map();
  const toSpecs = (list: MenuItem[]): MenuItemSpec[] =>
    list.map(({ click, submenu, ...rest }) => {
      const spec: MenuItemSpec = { ...rest };
      if (submenu) spec.submenu = toSpecs(submenu);
      else if (click) {
        spec.id = String(++menuSeq);
        menuActions.set(spec.id, click);
      }
      return spec;
    });
  void api.menu.popup(toSpecs(items));
}

api.menu.onAction((id) => menuActions.get(id)?.());

/** Give `el` a context menu, built when it is right-clicked. */
function contextMenu(el: HTMLElement, build: (ev: MouseEvent) => MenuItem[]): void {
  el.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    ev.stopPropagation(); // the innermost menu wins
    popupMenu(build(ev));
  });
}

/** One entry per template, spawning a workspace from `ctx` (what was right-clicked). */
function spawnItems(ctx: SpawnContext = {}): MenuItem[] {
  return spawnChoices(templates, ctx).map((c) => ({
    label: c.label,
    click: () => void spawn(c.request)
  }));
}

function spawnMenu(label: string, ctx: SpawnContext = {}): MenuItem[] {
  const submenu = spawnItems(ctx);
  return submenu.length ? [{ label, submenu }] : [];
}

// Text fields get the editing menu; everywhere else without a menu of its own gets none.
document.addEventListener("contextmenu", (ev) => {
  ev.preventDefault();
  if (!(ev.target instanceof HTMLInputElement)) return;
  const items = [...EDIT_ITEMS];
  if (ev.target.id === "url") {
    items.splice(6, 0, {
      label: "Paste and Go",
      click: async () => {
        const text = (await api.clipboard.read()).trim();
        if (text && activeId) void api.browser.navigate(activeId, text);
      }
    });
  }
  popupMenu(items);
});

// ---------- sidebar: workspace list ----------
function renderList(): void {
  const ul = $("#workspace-list");
  ul.replaceChildren(
    ...workspaces.map((ws) => {
      const li = document.createElement("li");
      li.className = ws.id === activeId ? "active" : "";
      li.innerHTML = `<span class="dot ${ws.state}"></span><span class="name"></span><button class="destroy" title="Destroy workspace">&times;</button>`;
      li.querySelector(".name")!.textContent = ws.name;
      li.title = `${ws.id} (${ws.template}, ${ws.state})${ws.error ? "\n" + ws.error : ""}`;
      li.onclick = () => void select(ws.id);
      li.querySelector<HTMLButtonElement>(".destroy")!.onclick = (ev) => {
        ev.stopPropagation();
        void destroy(ws.id);
      };
      contextMenu(li, () => [
        { label: "Open", click: () => void select(ws.id) },
        { label: "Copy Path", click: () => void api.clipboard.write(ws.dir) },
        SEP,
        ...spawnMenu("New Workspace"),
        SEP,
        { label: "Destroy…", click: () => void destroy(ws.id) }
      ]);
      return li;
    })
  );
}

contextMenu($("#sidebar"), () => {
  const items = spawnItems();
  return items.length ? [{ label: "New Workspace", enabled: false }, SEP, ...items] : [];
});

// ---------- home: the two workspace spawners ----------
// Shown in place of a workspace: the preset picker for a single workspace
// above, the Gerrit changes that spawn a collection below.
let chosenPreset: TemplateSpec | undefined;
/** URL the workspace being set up opens first (a right-clicked link, page or tab). */
let spawnUrl: string | undefined;

const gerritSpawn = initGerritSpawn({
  templates: () => templates,
  workspaces: () => workspaces,
  created: (ids) => {
    if (!activeId) void select(ids[0]);
  }
});

function showPresetGrid(): void {
  $("#form-error").textContent = "";
  $("#preset-form").classList.add("hidden");
  $("#preset-grid").classList.remove("hidden");
  $("#preset-grid").replaceChildren(
    ...templates.map((t) => {
      const card = document.createElement("button");
      card.className = "preset";
      card.type = "button";
      card.innerHTML = "<strong></strong><span></span><small></small>";
      card.querySelector("strong")!.textContent = t.name;
      card.querySelector("span")!.textContent = t.description ?? "";
      const n = Object.keys(t.variables ?? {}).length;
      card.querySelector("small")!.textContent = n
        ? `${n} field${n > 1 ? "s" : ""}`
        : "no fields, creates immediately";
      card.onclick = () => void spawn({ template: t.name, vars: {}, url: spawnUrl });
      return card;
    })
  );
}

/** Leave the workspace for the home view, with both spawners as they start out. */
async function showHome(): Promise<void> {
  chosenPreset = undefined;
  spawnUrl = undefined;
  showPresetGrid();
  gerritSpawn.show();
  await deselect();
  $("#home").scrollTop = 0;
}

const formVisible = () => !activeId && !$("#preset-form").classList.contains("hidden");

/** Show the preset's form, going to the home view if needed; `values` override the defaults. */
function showPresetForm(t: TemplateSpec, values: Record<string, string> = {}): void {
  chosenPreset = t;
  $("#preset-title").textContent = t.name;
  $("#preset-desc").textContent = t.description ?? "";
  $("#var-fields").replaceChildren(
    ...Object.entries(t.variables ?? {}).map(([name, v]) => {
      const label = document.createElement("label");
      label.textContent = v.prompt;
      const input = document.createElement("input");
      input.name = name;
      input.value = values[name] ?? v.default ?? "";
      input.placeholder = v.pattern ?? "";
      label.append(input);
      return label;
    })
  );
  $("#preset-grid").classList.add("hidden");
  $("#preset-form").classList.remove("hidden");
  if (activeId) void deselect();
  $("#home").scrollTop = 0;
  $("#var-fields").querySelector<HTMLInputElement>("input")?.focus();
}

/**
 * Start a workspace from the picker or a context menu. It is created right
 * away when there is nothing to ask: the preset has no variables, or the
 * right-clicked context supplied what has no default. Otherwise the form
 * comes up, filled in as far as the context goes.
 */
async function spawn(req: SpawnRequest): Promise<void> {
  const t = templates.find((x) => x.name === req.template);
  if (!t) return;
  spawnUrl = req.url;
  const fromContext = Object.keys(req.vars).length > 0 || req.url !== undefined;
  if (Object.keys(t.variables ?? {}).length === 0 || (fromContext && spawnResolved(t, req.vars)))
    return createFromPreset(t, req.vars, req.url);
  showPresetForm(t, req.vars);
}

api.workspaces.onSpawn((req) => void spawn(req));

async function createFromPreset(
  t: TemplateSpec,
  vars: Record<string, string>,
  url?: string
): Promise<void> {
  $("#form-error").textContent = "";
  try {
    const ws = await api.workspaces.create(t.name, vars, url ? [url] : undefined);
    showPresetGrid();
    await select(ws.id);
  } catch (err) {
    // A preset created without its form failed: bring the form up so the error has a place.
    if (!formVisible()) showPresetForm(t, vars);
    $("#form-error").textContent = errorText(err);
  }
}

$("#home-toggle").onclick = () => void showHome();
$("#preset-back").onclick = showPresetGrid;
$<HTMLFormElement>("#preset-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const vars: Record<string, string> = {};
  for (const input of $("#var-fields").querySelectorAll<HTMLInputElement>("input"))
    vars[input.name] = input.value;
  if (chosenPreset) void createFromPreset(chosenPreset, vars, spawnUrl);
});

// ---------- browser pane ----------
function slotBounds(): Bounds {
  const r = $("#browser-slot").getBoundingClientRect();
  return {
    x: Math.round(r.x),
    y: Math.round(r.y),
    width: Math.round(r.width),
    height: Math.round(r.height)
  };
}

new ResizeObserver(() => {
  if (activeId) void api.browser.setBounds(slotBounds());
  for (const p of panes.values()) if (p.el.offsetParent) fitPane(p);
}).observe($("#browser-slot"));

// ---------- tabs ----------
let lastActiveTabId: string | undefined;

function renderBrowserTabs(tabs: TabInfo[]): void {
  $("#tabs").replaceChildren(
    ...tabs.map((t) => {
      const el = document.createElement("div");
      el.className = `tab${t.active ? " active" : ""}${t.loading ? " loading" : ""}`;
      el.title = t.url;
      el.innerHTML =
        '<span class="title"></span><button class="close" title="Close tab (Ctrl+W)">&times;</button>';
      el.querySelector(".title")!.textContent =
        t.url === "about:blank" ? "New tab" : t.title || t.url.replace(/^https?:\/\//, "");
      el.onclick = () => activeId && void api.browser.activateTab(activeId, t.tabId);
      el.onauxclick = (ev) => {
        if (ev.button === 1 && activeId) void api.browser.closeTab(activeId, t.tabId);
      };
      el.querySelector<HTMLButtonElement>(".close")!.onclick = (ev) => {
        ev.stopPropagation();
        if (activeId) void api.browser.closeTab(activeId, t.tabId);
      };
      contextMenu(el, () => {
        const wsId = activeId;
        if (!wsId) return [];
        return [
          { label: "New Tab", click: () => void newTab() },
          { label: "Duplicate Tab", click: () => void newTab(t.url) },
          ...(isWeb(t.url)
            ? spawnMenu("Open in New Workspace", { url: t.url, text: t.title })
            : []),
          { label: "Copy Address", click: () => void api.clipboard.write(t.url) },
          SEP,
          { label: "Close Tab", click: () => void api.browser.closeTab(wsId, t.tabId) },
          {
            label: "Close Other Tabs",
            enabled: tabs.length > 1,
            click: () => {
              for (const other of tabs)
                if (other.tabId !== t.tabId) void api.browser.closeTab(wsId, other.tabId);
            }
          }
        ];
      });
      return el;
    })
  );
  const active = tabs.find((t) => t.active);
  const url = $<HTMLInputElement>("#url");
  // Keep the user's typing while the same tab updates; a tab switch always refreshes the bar.
  const switched = active?.tabId !== lastActiveTabId;
  lastActiveTabId = active?.tabId;
  if (switched && document.activeElement === url) url.blur();
  if (switched || document.activeElement !== url)
    url.value = active && active.url !== "about:blank" ? active.url : "";
  $<HTMLButtonElement>("#nav-back").disabled = !active?.canGoBack;
  $<HTMLButtonElement>("#nav-forward").disabled = !active?.canGoForward;
}

api.browser.onTabs((id, tabs) => {
  if (id === activeId) renderBrowserTabs(tabs);
});

async function newTab(url?: string): Promise<void> {
  if (!activeId) return;
  await api.browser.newTab(activeId, url);
  if (!url) $("#url").focus();
}

$("#tab-new").onclick = () => void newTab();
contextMenu($("#tab-strip"), () => [{ label: "New Tab", click: () => void newTab() }]);

api.browser.onShortcut((id, action) => {
  if (id === activeId && action === "focus-url") {
    const url = $<HTMLInputElement>("#url");
    url.focus();
    url.select();
  }
});

// Same shortcuts when focus is in the shell rather than a page (but not in a terminal).
document.addEventListener("keydown", (ev) => {
  if (!activeId || !ev.ctrlKey || ev.altKey || ev.metaKey) return;
  const target = ev.target as HTMLElement;
  if (target.closest("#terminal-slot")) return;
  const key = ev.key.toLowerCase();
  if (key === "t") void newTab();
  else if (key === "w") void api.browser.closeActiveTab(activeId);
  else if (key === "l") {
    const url = $<HTMLInputElement>("#url");
    url.focus();
    url.select();
  } else if (key === "=" || key === "+") void api.browser.zoom(activeId, "in");
  else if (key === "-") void api.browser.zoom(activeId, "out");
  else if (key === "0") void api.browser.zoom(activeId, "reset");
  else if (key === "r") void api.browser.reload(activeId);
  else return;
  ev.preventDefault();
});

$("#url").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && activeId) {
    void api.browser.navigate(activeId, (ev.currentTarget as HTMLInputElement).value);
    (ev.currentTarget as HTMLInputElement).blur();
  }
});
$("#nav-back").onclick = () => activeId && void api.browser.back(activeId);
$("#nav-forward").onclick = () => activeId && void api.browser.forward(activeId);
$("#nav-reload").onclick = () => activeId && void api.browser.reload(activeId);
$("#ublock").onclick = async () => {
  if (activeId && !(await api.adblock.dashboard(activeId)))
    alert(
      "uBlock Origin is not loaded in this workspace.\nEither the template has adblock: false, or the app was built without it (make workbench-install)."
    );
};
// ---------- fullscreen ----------
// Full screen mode = the whole app window fullscreen (layout unchanged).
// A page's own fullscreen (video players) additionally hides the shell so the
// page gets the whole window.
let appFullscreen = false;
api.window.onFullscreen((on) => {
  appFullscreen = on;
  $("#fullscreen").title = on ? "Leave full screen (F11 or Esc)" : "Full screen (F11)";
});
void api.window.isFullscreen().then((on) => (appFullscreen = on));
api.browser.onHtmlFullscreen((on) => {
  document.body.classList.toggle("page-fullscreen", on);
  // The browser slot changed size; push new bounds even if the observer is late.
  if (activeId) void api.browser.setBounds(slotBounds());
});
$("#fullscreen").onclick = () => void api.window.setFullscreen(!appFullscreen);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "F11") {
    ev.preventDefault();
    void api.window.setFullscreen(!appFullscreen);
  } else if (
    ev.key === "Escape" &&
    appFullscreen &&
    !document.body.classList.contains("page-fullscreen")
  ) {
    void api.window.setFullscreen(false);
  }
});

$("#cookies").onclick = async (ev) => {
  if (!activeId) return;
  const b = ev.currentTarget as HTMLButtonElement;
  b.disabled = true;
  b.textContent = "Importing…";
  try {
    const r = await api.cookies.import(activeId);
    b.textContent = r.profile ? `Cookies ✓ ${r.imported}` : "No Firefox profile";
  } catch {
    b.textContent = "Import failed";
  }
  b.disabled = false;
  window.setTimeout(() => (b.textContent = "Cookies"), 3000);
};

// ---------- Firefox bookmarks toolbar ----------
let toolbar: BookmarkNode[] = [];

function popupUnder(el: HTMLElement, nodes: BookmarkNode[]): void {
  if (!activeId) return;
  const r = el.getBoundingClientRect();
  void api.bookmarks.popup(activeId, nodes, r.left, r.bottom);
}

async function refreshBookmarkBar(): Promise<void> {
  toolbar = await api.bookmarks.toolbar();
  $("#bookmark-bar").classList.toggle("hidden", toolbar.length === 0);
  $("#bookmark-items").replaceChildren(
    ...toolbar.map((n) => {
      const b = document.createElement("button");
      b.textContent = n.title;
      b.title = n.url ?? n.title;
      if (n.children) {
        b.className = "folder";
        b.onclick = () => popupUnder(b, n.children!);
      } else {
        b.onclick = (ev) =>
          activeId &&
          (ev.ctrlKey ? void newTab(n.url) : void api.browser.navigate(activeId, n.url!));
        b.onauxclick = (ev) => ev.button === 1 && void newTab(n.url);
        contextMenu(b, () => [
          { label: "Open", click: () => activeId && void api.browser.navigate(activeId, n.url!) },
          { label: "Open in New Tab", click: () => void newTab(n.url) },
          ...(isWeb(n.url)
            ? spawnMenu("Open in New Workspace", { url: n.url, text: n.title })
            : []),
          { label: "Copy Link Address", click: () => void api.clipboard.write(n.url!) }
        ]);
      }
      return b;
    })
  );
  layoutBookmarkBar();
}

/** Hide the items that do not fit and offer them from the » button instead. */
function layoutBookmarkBar(): void {
  const container = $("#bookmark-items");
  const more = $<HTMLButtonElement>("#bookmark-more");
  const buttons = [...container.querySelectorAll<HTMLButtonElement>("button")];
  for (const b of buttons) b.classList.remove("overflow");
  const limit = container.getBoundingClientRect().right;
  let firstHidden = buttons.length;
  for (let i = 0; i < buttons.length; i++) {
    if (buttons[i].getBoundingClientRect().right > limit) {
      firstHidden = i;
      break;
    }
  }
  for (let i = firstHidden; i < buttons.length; i++) buttons[i].classList.add("overflow");
  more.style.visibility = firstHidden < buttons.length ? "visible" : "hidden";
  more.onclick = () => popupUnder(more, toolbar.slice(firstHidden));
}

new ResizeObserver(layoutBookmarkBar).observe($("#bookmark-items"));
window.addEventListener("focus", () => void refreshBookmarkBar());

// ---------- terminal panes ----------
const TERM_FONT = '"JetBrains Mono", "Fira Code", "DejaVu Sans Mono", "Liberation Mono", monospace';
const TERM_FONT_DEFAULT = 15;
/** Terminal palette matching the gray shell; ANSI colors darkened to stay legible on it. */
const TERM_THEME = {
  background: "#dddddd",
  foreground: "#333333",
  cursor: "#333333",
  cursorAccent: "#dddddd",
  selectionBackground: "#000080",
  selectionForeground: "#ffffff",
  black: "#333333",
  red: "#9a0b00",
  green: "#155a19",
  yellow: "#6b4700",
  blue: "#1f4f8f",
  magenta: "#6f2f8c",
  cyan: "#0f5861",
  white: "#f2f2f2",
  brightBlack: "#555555",
  brightRed: "#b8160c",
  brightGreen: "#1e6b22",
  brightYellow: "#7d5400",
  brightBlue: "#2460b0",
  brightMagenta: "#8a3fae",
  brightCyan: "#146c77",
  brightWhite: "#ffffff"
};
let termFontSize = TERM_FONT_DEFAULT;
try {
  termFontSize = Number(localStorage.getItem("termFontSize")) || TERM_FONT_DEFAULT;
} catch {
  /* storage unavailable */
}

function setTermFontSize(size: number): void {
  termFontSize = Math.min(40, Math.max(8, size));
  try {
    localStorage.setItem("termFontSize", String(termFontSize));
  } catch {
    /* ignore */
  }
  for (const p of panes.values()) {
    p.term.options.fontSize = termFontSize;
    if (p.el.offsetParent) fitPane(p);
  }
}

function fitPane(p: Pane): void {
  p.fit.fit();
  api.pty.resize(p.key, p.term.cols, p.term.rows);
}

async function ensurePane(wsId: string, name: string): Promise<Pane> {
  const key = `${wsId}:${name}`;
  const existing = panes.get(key);
  if (existing) return existing;

  const el = document.createElement("div");
  el.className = "pane hidden";
  $("#terminal-slot").append(el);
  const term = new Terminal({
    fontSize: termFontSize,
    fontFamily: TERM_FONT,
    lineHeight: 1.15,
    cursorBlink: true,
    scrollback: 5000,
    theme: TERM_THEME
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(el);
  try {
    term.loadAddon(new WebglAddon());
  } catch {
    /* fall back to the DOM renderer */
  }
  term.onData((d) => api.pty.write(key, d));
  const pane: Pane = { key, el, term, fit };
  panes.set(key, pane);

  el.classList.remove("hidden");
  fit.fit();
  await api.pty.open(wsId, name, term.cols, term.rows);
  return pane;
}

$("#terminal-slot").addEventListener(
  "keydown",
  (ev) => {
    if (!ev.ctrlKey || ev.altKey || ev.metaKey) return;
    if (ev.key === "=" || ev.key === "+") setTermFontSize(termFontSize + 1);
    else if (ev.key === "-") setTermFontSize(termFontSize - 1);
    else if (ev.key === "0") setTermFontSize(TERM_FONT_DEFAULT);
    else return;
    ev.preventDefault();
    ev.stopPropagation();
  },
  true
);
// F11 toggles full screen from the terminal too (xterm would otherwise turn it
// into an escape sequence). Escape stays a terminal key there.
$("#terminal-slot").addEventListener(
  "keydown",
  (ev) => {
    if (ev.key !== "F11") return;
    ev.preventDefault();
    ev.stopPropagation();
    void api.window.setFullscreen(!appFullscreen);
  },
  true
);

contextMenu($("#terminal-slot"), (ev) => {
  const term = activeId && activePane ? panes.get(`${activeId}:${activePane}`)?.term : undefined;
  // A program that tracks the mouse gets the click itself; Shift+right-click forces the menu.
  if (!term || (term.modes.mouseTrackingMode !== "none" && !ev.shiftKey)) return [];
  const selection = term.getSelection().trim();
  const url = isWeb(selection) && !/\s/.test(selection) ? selection : undefined;
  return [
    ...(url ? [{ label: "Open Link in New Tab", click: () => void newTab(url) }, SEP] : []),
    { label: "Copy", enabled: !!selection, click: () => void api.clipboard.write(selection) },
    {
      label: "Paste",
      click: async () => {
        term.paste(await api.clipboard.read());
        term.focus();
      }
    },
    { label: "Select All", click: () => term.selectAll() },
    { label: "Clear", click: () => term.clear() },
    SEP,
    ...(selection ? spawnMenu("New Workspace from Selection", { text: selection, url }) : [])
  ];
});

api.pty.onData((key, data) => panes.get(key)?.term.write(data));
api.pty.onExit((key, code) =>
  panes.get(key)?.term.write(`\r\n[process exited with code ${code}]\r\n`)
);

async function showPane(wsId: string, name: string): Promise<void> {
  activePane = name;
  const key = `${wsId}:${name}`;
  for (const p of panes.values()) p.el.classList.toggle("hidden", p.key !== key);
  const pane = await ensurePane(wsId, name);
  if (activeId !== wsId || activePane !== name) {
    pane.el.classList.add("hidden"); // selection changed while the pty was opening
    return;
  }
  fitPane(pane);
  pane.term.focus();
  for (const b of $("#terminal-tabs").querySelectorAll("button"))
    b.classList.toggle("active", b.dataset.pane === name);
}

async function renderPaneTabs(wsId: string): Promise<void> {
  const specs = await api.workspaces.panes(wsId);
  $("#terminal-tabs").replaceChildren(
    ...specs.map((p) => {
      const b = document.createElement("button");
      b.textContent = p.name;
      b.dataset.pane = p.name;
      b.onclick = () => void showPane(wsId, p.name);
      return b;
    })
  );
  await showPane(
    wsId,
    activePane && specs.some((p) => p.name === activePane) ? activePane : specs[0].name
  );
}

// ---------- destroy ----------
async function destroy(id: string): Promise<void> {
  const ws = workspaces.find((w) => w.id === id);
  if (
    !ws ||
    !confirm(
      `Destroy workspace "${ws.name}"?\nThis removes its directory, browser data and terminals.`
    )
  )
    return;
  if (activeId === id) {
    // Move on to the workspace below in the sidebar, the one above for the last, else home.
    const i = workspaces.indexOf(ws);
    const next = workspaces[i + 1] ?? workspaces[i - 1];
    await (next ? select(next.id) : deselect());
  }
  for (const [key, p] of panes) {
    if (key.startsWith(`${id}:`)) {
      p.term.dispose();
      p.el.remove();
      panes.delete(key);
    }
  }
  try {
    const refusal = await api.workspaces.destroy(id);
    if (
      refusal !== undefined &&
      confirm(
        `"${ws.name}" was kept because its destroy hook failed.\n\n${refusal.trim()}\n\nDestroy it anyway? Its directory is removed as it is.`
      )
    )
      await api.workspaces.destroy(id, true);
  } catch (err) {
    alert(errorText(err));
  }
}

// ---------- selection ----------
/** Bumped on every selection so async work from a superseded selection can bail out. */
let selectSeq = 0;

async function select(id: string): Promise<void> {
  const ws = workspaces.find((w) => w.id === id);
  if (!ws) return;
  const seq = ++selectSeq;
  activeId = id;
  activePane = undefined;
  lastActiveTabId = undefined;
  renderList();
  $("#home").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  // Show the browser first: it is the slowest thing to become visible. Showing
  // emits the tab list, which onTabs renders since activeId is already set.
  await api.browser.show(id, slotBounds());
  if (seq !== selectSeq) return;
  await renderPaneTabs(id);
}

async function deselect(): Promise<void> {
  activeId = undefined;
  await api.browser.hide();
  $("#workspace").classList.add("hidden");
  $("#home").classList.remove("hidden");
  renderList();
}

api.workspaces.onChanged((list) => {
  workspaces = list;
  renderList();
  gerritSpawn.workspacesChanged();
  if (activeId && !workspaces.some((w) => w.id === activeId)) void deselect();
});

// ---------- sidebar footer: top-style host status ----------
const pad = (n: number) => String(n).padStart(2, "0");

function fmtUptime(secs: number): string {
  const days = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return days > 0 ? `${days}d ${h}:${pad(m)}` : `${h}:${pad(m)}`;
}

let uptimeAt = 0; // Date.now() when `uptime` was sampled
let uptime = 0;

function renderClock(): void {
  const now = new Date();
  const time = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  const up = uptimeAt ? ` up ${fmtUptime(uptime + (Date.now() - uptimeAt) / 1000)}` : "";
  $("#sys-time").textContent = `${time}${up}`;
}

async function renderStats(): Promise<void> {
  const s = await api.system.stats();
  uptime = s.uptime;
  uptimeAt = Date.now();
  const gib = (b: number) => (b / 2 ** 30).toFixed(1);
  $("#sys-load").textContent = `load average: ${s.load.map((l) => l.toFixed(2)).join(", ")}`;
  $("#sys-mem").textContent =
    `GiB Mem: ${gib(s.mem.free)} free, ${gib(s.mem.available)} avail, ${gib(s.mem.total)} total`;
  renderClock();
}

setInterval(renderClock, 1000);
setInterval(() => void renderStats().catch(() => {}), 2000);

// ---------- boot ----------
void (async () => {
  [templates, workspaces] = await Promise.all([api.templates.list(), api.workspaces.list()]);
  renderList();
  void showHome();
  await Promise.all([refreshBookmarkBar(), renderStats().catch(() => {})]);
})();
