// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { BrowserWindow, Menu, type MenuItemConstructorOptions } from "electron";
import { BookmarkSource } from "../core/firefox";
import type { BookmarkNode } from "../shared/types";

const source = new BookmarkSource();

/** Contents of the Firefox "Bookmarks Toolbar" folder, or [] when there is no profile. */
export async function toolbarBookmarks(): Promise<BookmarkNode[]> {
  const tree = await source.get();
  return tree.find((r) => r.title === "Bookmarks Toolbar")?.children ?? [];
}

function items(nodes: BookmarkNode[], open: (url: string) => void): MenuItemConstructorOptions[] {
  if (nodes.length === 0) return [{ label: "(empty)", enabled: false }];
  return nodes.map((n) =>
    n.children
      ? { label: trim(n.title), submenu: items(n.children, open) }
      : { label: trim(n.title), toolTip: n.url, click: () => open(n.url!) }
  );
}

const trim = (s: string) => (s.length > 70 ? s.slice(0, 67) + "…" : s);

/** Pop up a list of bookmarks as a native menu at (x, y) in the window. */
export function popupBookmarkMenu(
  win: BrowserWindow,
  nodes: BookmarkNode[],
  x: number,
  y: number,
  open: (url: string) => void
): void {
  Menu.buildFromTemplate(items(nodes, open)).popup({
    window: win,
    x: Math.round(x),
    y: Math.round(y)
  });
}

/**
 * Resolve toolbar bookmark paths to URLs, in order. A path names a bookmark
 * ("TLC/Outlook") or a folder ("TLC", all its bookmarks recursively). Path
 * segments are folder/bookmark titles; a single segment is also searched
 * recursively so "TLC" works wherever the folder sits. Unknown paths are skipped.
 */
export async function bookmarkUrls(
  paths: string[]
): Promise<{ urls: string[]; missing: string[] }> {
  const root = await toolbarBookmarks();
  const flat = (nodes: BookmarkNode[]): string[] =>
    nodes.flatMap((n) => (n.url ? [n.url] : flat(n.children ?? [])));
  const findDeep = (nodes: BookmarkNode[], title: string): BookmarkNode | undefined => {
    for (const n of nodes) {
      if (n.title === title) return n;
      if (n.children) {
        const hit = findDeep(n.children, title);
        if (hit) return hit;
      }
    }
    return undefined;
  };
  const resolve = (path: string): BookmarkNode | undefined => {
    const segs = path
      .split("/")
      .map((x) => x.trim())
      .filter(Boolean);
    if (segs.length === 1) return findDeep(root, segs[0]);
    let nodes = root;
    let node: BookmarkNode | undefined;
    for (const seg of segs) {
      node = nodes.find((n) => n.title === seg);
      if (!node) return undefined;
      nodes = node.children ?? [];
    }
    return node;
  };
  const urls: string[] = [];
  const missing: string[] = [];
  for (const p of paths) {
    const node = resolve(p);
    if (!node) missing.push(p);
    else urls.push(...(node.url ? [node.url] : flat(node.children ?? [])));
  }
  return { urls, missing };
}
