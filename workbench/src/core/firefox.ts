// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { copyFile, mkdtemp, readFile, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { BookmarkNode } from "../shared/types";

/** Directories that may hold profiles.ini, in order of preference. */
export function firefoxDirs(): string[] {
  const home = os.homedir();
  const dirs = [
    process.env["WORKBENCH_FIREFOX_DIR"],
    // Firefox 139+ on Linux keeps new profiles under XDG_CONFIG_HOME.
    path.join(process.env["XDG_CONFIG_HOME"] || path.join(home, ".config"), "mozilla/firefox"),
    path.join(home, ".mozilla/firefox"),
    path.join(home, "snap/firefox/common/.mozilla/firefox"),
    path.join(home, ".var/app/org.mozilla.firefox/.mozilla/firefox")
  ];
  return dirs.filter((d): d is string => !!d);
}

/** Find the default profile directory by parsing profiles.ini. */
export async function findProfile(dirs = firefoxDirs()): Promise<string | undefined> {
  for (const dir of dirs) {
    let ini: string;
    try {
      ini = await readFile(path.join(dir, "profiles.ini"), "utf8");
    } catch {
      continue;
    }
    const sections: Record<string, string>[] = [];
    let cur: Record<string, string> | undefined;
    for (const raw of ini.split(/\r?\n/)) {
      const line = raw.trim();
      if (line.startsWith("[")) sections.push((cur = { __name: line.slice(1, -1) }));
      else if (cur && line.includes("=")) {
        const i = line.indexOf("=");
        cur[line.slice(0, i)] = line.slice(i + 1);
      }
    }
    const profiles = sections.filter((s) => s.__name.startsWith("Profile"));
    const resolve = (s: Record<string, string>) =>
      s.IsRelative === "0" ? s.Path : path.join(dir, s.Path);
    // Modern Firefox records the default per installation; older ones flag a profile.
    const install = sections.find((s) => s.__name.startsWith("Install") && s.Default);
    if (install) {
      const p = profiles.find((s) => s.Path === install.Default);
      return p ? resolve(p) : path.join(dir, install.Default);
    }
    const flagged = profiles.find((s) => s.Default === "1") ?? profiles[0];
    if (flagged?.Path) return resolve(flagged);
  }
  return undefined;
}

interface Row {
  id: number;
  type: number;
  parent: number;
  title: string | null;
  guid: string;
  url: string | null;
}

/**
 * Firefox holds its databases open while running, so a file (and its WAL) is
 * snapshotted to a temp dir and the copy is opened read-only.
 */
async function withSnapshot<T>(
  profileDir: string,
  file: string,
  fn: (db: DatabaseSync) => T
): Promise<T> {
  const src = path.join(profileDir, file);
  const tmp = await mkdtemp(path.join(os.tmpdir(), "workbench-ff-"));
  try {
    await copyFile(src, path.join(tmp, file));
    await copyFile(`${src}-wal`, path.join(tmp, `${file}-wal`)).catch(() => {});
    const db = new DatabaseSync(path.join(tmp, file), { readOnly: true });
    try {
      return fn(db);
    } finally {
      db.close();
    }
  } finally {
    await rm(tmp, { recursive: true, force: true });
  }
}

/** Read the bookmark tree from places.sqlite: the toolbar, menu, other and mobile roots. */
export function readBookmarks(profileDir: string): Promise<BookmarkNode[]> {
  return withSnapshot(profileDir, "places.sqlite", (db) =>
    buildTree(
      db
        .prepare(
          `SELECT b.id, b.type, b.parent, b.title, b.guid, p.url
             FROM moz_bookmarks b LEFT JOIN moz_places p ON p.id = b.fk
            ORDER BY b.parent, b.position`
        )
        .all() as unknown as Row[]
    )
  );
}

export interface FirefoxCookie {
  name: string;
  value: string;
  /** Leading dot means a domain cookie; otherwise host-only. */
  host: string;
  path: string;
  /** Unix seconds. */
  expiry: number;
  secure: boolean;
  httpOnly: boolean;
  sameSite: "unspecified" | "no_restriction" | "lax" | "strict";
}

/** Firefox sameSite column: 0 none, 1 lax, 2 strict, 256 unset. */
const SAME_SITE: Record<number, FirefoxCookie["sameSite"]> = { 1: "lax", 2: "strict" };

interface CookieRow {
  name: string;
  value: string;
  host: string;
  path: string;
  expiry: number;
  isSecure: number;
  isHttpOnly: number;
  sameSite: number;
}

/**
 * Read first-party, unexpired cookies from cookies.sqlite. Session cookies are
 * not in the file (Firefox keeps them in memory), and cookies Firefox has
 * partitioned under another top-level site or a container are skipped.
 */
export function readCookies(profileDir: string): Promise<FirefoxCookie[]> {
  const now = Math.floor(Date.now() / 1000);
  return withSnapshot(profileDir, "cookies.sqlite", (db) =>
    (
      db
        .prepare(
          `SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite
             FROM moz_cookies WHERE originAttributes = '' AND expiry > ?`
        )
        .all(now) as unknown as CookieRow[]
    ).map((r) => ({
      name: r.name,
      value: r.value,
      host: r.host,
      path: r.path,
      expiry: r.expiry,
      secure: r.isSecure === 1,
      httpOnly: r.isHttpOnly === 1,
      // Chromium rejects None without Secure, so such cookies fall back to unspecified.
      sameSite:
        SAME_SITE[r.sameSite] ??
        (r.sameSite === 0 && r.isSecure === 1 ? "no_restriction" : "unspecified")
    }))
  );
}

const ROOT_GUIDS: Record<string, string> = {
  toolbar_____: "Bookmarks Toolbar",
  menu________: "Bookmarks Menu",
  unfiled_____: "Other Bookmarks",
  mobile______: "Mobile Bookmarks"
};

export function buildTree(rows: Row[]): BookmarkNode[] {
  const byParent = new Map<number, Row[]>();
  for (const r of rows) {
    const list = byParent.get(r.parent) ?? [];
    list.push(r);
    byParent.set(r.parent, list);
  }
  const children = (id: number): BookmarkNode[] => {
    const out: BookmarkNode[] = [];
    for (const r of byParent.get(id) ?? []) {
      if (r.type === 2) out.push({ title: r.title || "(untitled)", children: children(r.id) });
      else if (r.type === 1 && r.url && !r.url.startsWith("place:"))
        out.push({ title: r.title || r.url, url: r.url });
      // type 3 separators and "place:" smart folders are skipped
    }
    return out;
  };
  return Object.entries(ROOT_GUIDS).flatMap(([guid, title]) => {
    const root = rows.find((r) => r.guid === guid);
    return root ? [{ title, children: children(root.id) }] : [];
  });
}

/** Cached reader: re-reads only when places.sqlite or its WAL changed. */
export class BookmarkSource {
  private stamp = "";
  private tree: BookmarkNode[] = [];

  async get(): Promise<{ tree: BookmarkNode[]; profile?: string }> {
    const profile = await findProfile();
    if (!profile) return { tree: [] };
    const src = path.join(profile, "places.sqlite");
    const stamp = await Promise.all(
      [src, `${src}-wal`].map((f) =>
        stat(f).then(
          (s) => `${s.mtimeMs}:${s.size}`,
          () => "-"
        )
      )
    );
    const key = `${profile}|${stamp.join("|")}`;
    if (key !== this.stamp) {
      this.tree = await readBookmarks(profile);
      this.stamp = key;
    }
    return { tree: this.tree, profile };
  }
}
