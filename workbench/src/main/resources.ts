// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { app } from "electron";
import path from "node:path";

/**
 * Directory holding bundled data that must exist as real files on disk
 * (templates, the uBlock extension): the repo root in development, and
 * electron-builder's extraResources dir in a packaged app.
 */
export function resourcesDir(): string {
  return app.isPackaged ? process.resourcesPath : app.getAppPath();
}
