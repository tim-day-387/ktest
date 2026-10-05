// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import {
  BrowserWindow,
  clipboard,
  Menu,
  type ContextMenuParams,
  type MenuItemConstructorOptions,
  type WebContents
} from "electron";
import { spawnChoices } from "../shared/spawn";
import type { MenuItemSpec, SpawnContext, SpawnRequest, TemplateSpec } from "../shared/types";

type Items = MenuItemConstructorOptions[];

export interface PageMenuActions {
  templates(): TemplateSpec[];
  /** `activate` false opens the tab in the background. */
  newTab(url: string, activate: boolean): void;
  spawn(req: SpawnRequest): void;
}

const SEARCH_URL = "https://duckduckgo.com/?q=";
const isWeb = (url: string) => /^https?:\/\//i.test(url);
/** Menu labels treat "&" as an accelerator marker. */
const esc = (s: string) => s.replace(/&/g, "&&");
const quote = (s: string) => {
  const line = s.replace(/\s+/g, " ").trim();
  return esc(line.length > 30 ? line.slice(0, 29) + "…" : line);
};

/** Drop leading, trailing and doubled separators left behind by the optional sections. */
function tidy(items: Items): Items {
  const out = items.filter(
    (item, i) => item.type !== "separator" || (i > 0 && items[i - 1].type !== "separator")
  );
  while (out.at(-1)?.type === "separator") out.pop();
  return out;
}

function spawnMenu(label: string, ctx: SpawnContext, actions: PageMenuActions): Items {
  const choices = spawnChoices(actions.templates(), ctx);
  if (choices.length === 0) return [];
  return [
    {
      label,
      submenu: choices.map((c) => ({ label: esc(c.label), click: () => actions.spawn(c.request) }))
    }
  ];
}

/** The context menu of a web page: link, image, selection, editing and page sections as they apply. */
export function popupPageMenu(
  win: BrowserWindow,
  wc: WebContents,
  p: ContextMenuParams,
  actions: PageMenuActions
): void {
  const items: Items = [];
  const selection = p.selectionText.trim();
  const flags = p.editFlags;

  if (p.linkURL) {
    if (isWeb(p.linkURL)) {
      items.push(
        { label: "Open Link in New Tab", click: () => actions.newTab(p.linkURL, false) },
        ...spawnMenu(
          "Open Link in New Workspace",
          { url: p.linkURL, text: p.linkText || selection },
          actions
        )
      );
    }
    items.push(
      { label: "Copy Link Address", click: () => clipboard.writeText(p.linkURL) },
      { type: "separator" }
    );
  }

  if (p.mediaType === "image") {
    if (isWeb(p.srcURL))
      items.push({ label: "Open Image in New Tab", click: () => actions.newTab(p.srcURL, false) });
    items.push(
      { label: "Copy Image", click: () => wc.copyImageAt(p.x, p.y) },
      { label: "Copy Image Address", click: () => clipboard.writeText(p.srcURL) },
      { type: "separator" }
    );
  }

  if (p.misspelledWord) {
    items.push(
      ...p.dictionarySuggestions.map((word) => ({
        label: esc(word),
        click: () => wc.replaceMisspelling(word)
      })),
      {
        label: "Add to Dictionary",
        click: () => wc.session.addWordToSpellCheckerDictionary(p.misspelledWord)
      },
      { type: "separator" }
    );
  }

  if (p.isEditable) {
    items.push(
      { label: "Undo", enabled: flags.canUndo, click: () => wc.undo() },
      { label: "Redo", enabled: flags.canRedo, click: () => wc.redo() },
      { type: "separator" },
      { label: "Cut", enabled: flags.canCut, click: () => wc.cut() },
      { label: "Copy", enabled: flags.canCopy, click: () => wc.copy() },
      { label: "Paste", enabled: flags.canPaste, click: () => wc.paste() },
      { label: "Select All", enabled: flags.canSelectAll, click: () => wc.selectAll() },
      { type: "separator" }
    );
  } else if (selection) {
    items.push({ label: "Copy", click: () => wc.copy() });
  }

  if (selection) {
    items.push(
      {
        label: `Search the Web for “${quote(selection)}”`,
        click: () => actions.newTab(SEARCH_URL + encodeURIComponent(selection), true)
      },
      ...spawnMenu("New Workspace from Selection", { text: selection }, actions),
      { type: "separator" }
    );
  }

  // Nothing specific under the pointer: the page itself.
  if (!p.linkURL && p.mediaType === "none" && !p.isEditable && !selection) {
    const history = wc.navigationHistory;
    items.push(
      { label: "Back", enabled: history.canGoBack(), click: () => history.goBack() },
      { label: "Forward", enabled: history.canGoForward(), click: () => history.goForward() },
      { label: "Reload", click: () => wc.reload() },
      { type: "separator" }
    );
    if (isWeb(p.pageURL)) {
      items.push(
        ...spawnMenu(
          "Open Page in New Workspace",
          { url: p.pageURL, text: wc.getTitle() },
          actions
        ),
        { label: "Copy Page Address", click: () => clipboard.writeText(p.pageURL) },
        { type: "separator" }
      );
    }
  }

  items.push({ label: "Inspect Element", click: () => wc.inspectElement(p.x, p.y) });
  // Without coordinates the menu opens at the pointer, wherever the tab's view sits in the window.
  Menu.buildFromTemplate(tidy(items)).popup({ window: win });
}

/** Pop up a menu described by the renderer at the pointer; a click is reported by item id. */
export function popupShellMenu(
  win: BrowserWindow,
  specs: MenuItemSpec[],
  onAction: (id: string) => void
): void {
  const build = (list: MenuItemSpec[]): Items =>
    list.map((s) => {
      if (s.type === "separator") return { type: "separator" };
      if (s.role) return { role: s.role };
      const item: MenuItemConstructorOptions = { label: esc(s.label ?? ""), enabled: s.enabled };
      if (s.submenu) item.submenu = build(s.submenu);
      else if (s.id !== undefined) {
        const id = s.id;
        item.click = () => onAction(id);
      }
      return item;
    });
  Menu.buildFromTemplate(tidy(build(specs))).popup({ window: win });
}
