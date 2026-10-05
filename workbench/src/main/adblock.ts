// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import type { Session } from "electron";
import { existsSync } from "node:fs";
import path from "node:path";
import { resourcesDir } from "./resources";

const extensionDir = path.join(resourcesDir(), "vendor", "ublock");
const loaded = new WeakMap<Session, string>(); // session -> extension id
let warned = false;

/**
 * Load the vendored uBlock Origin into a session. The caller sets up each
 * session once per run. Extensions can only live in persistent partitions,
 * so ephemeral profiles are skipped.
 *
 * Do not register any `session.webRequest` handler on a session that hosts
 * the extension: Electron then bypasses the extension's own webRequest
 * listeners entirely and uBlock silently stops filtering.
 */
export async function loadAdblock(ses: Session, partition: string): Promise<void> {
  if (!partition.startsWith("persist:")) return;
  if (!existsSync(path.join(extensionDir, "manifest.json"))) {
    if (!warned) console.warn("uBlock Origin not installed; run `npm run fetch-ublock`");
    warned = true;
    return;
  }
  try {
    const ext = await ses.extensions.loadExtension(extensionDir, { allowFileAccess: false });
    loaded.set(ses, ext.id);
  } catch (err) {
    console.warn("could not load uBlock Origin:", err);
  }
}

/** URL of uBlock's dashboard in this session, or undefined if it is not loaded. */
export function adblockDashboardUrl(ses: Session): string | undefined {
  const id = loaded.get(ses);
  return id ? `chrome-extension://${id}/dashboard.html` : undefined;
}
