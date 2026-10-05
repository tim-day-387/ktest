// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import type { Workspace } from "../shared/types";

/**
 * On-disk layout:  <root>/<id>/workspace.json  and  <root>/<id>/task.md
 * The directory is the source of truth; nothing is cached elsewhere.
 */
export class WorkspaceStore {
  constructor(readonly root: string) {}

  dirFor(id: string): string {
    return path.join(this.root, id);
  }

  taskFile(id: string): string {
    return path.join(this.dirFor(id), "task.md");
  }

  async loadAll(): Promise<Workspace[]> {
    await mkdir(this.root, { recursive: true });
    const out: Workspace[] = [];
    for (const id of await readdir(this.root)) {
      try {
        out.push(JSON.parse(await readFile(path.join(this.dirFor(id), "workspace.json"), "utf8")));
      } catch {
        /* not a workspace dir, or half-created; ignore */
      }
    }
    return out.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }

  async save(ws: Workspace): Promise<void> {
    await mkdir(ws.dir, { recursive: true });
    await writeFile(path.join(ws.dir, "workspace.json"), JSON.stringify(ws, null, 2) + "\n");
  }

  async remove(id: string): Promise<void> {
    await rm(this.dirFor(id), { recursive: true, force: true });
  }
}
