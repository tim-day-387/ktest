// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import type { Session } from "electron";
import { findProfile, readCookies } from "../core/firefox";

export interface ImportResult {
  imported: number;
  failed: number;
  /** false when no Firefox profile was found. */
  profile: boolean;
}

/** Copy Firefox's first-party cookies into an Electron session. Existing cookies with the same key are overwritten. */
export async function importFirefoxCookies(ses: Session): Promise<ImportResult> {
  const profile = await findProfile();
  if (!profile) return { imported: 0, failed: 0, profile: false };
  const cookies = await readCookies(profile);
  let imported = 0;
  let failed = 0;
  const BATCH = 50;
  for (let i = 0; i < cookies.length; i += BATCH) {
    await Promise.all(
      cookies.slice(i, i + BATCH).map(async (c) => {
        const bareHost = c.host.replace(/^\./, "");
        try {
          await ses.cookies.set({
            url: `${c.secure ? "https" : "http"}://${bareHost}${c.path}`,
            name: c.name,
            value: c.value,
            // A leading dot marks a domain cookie; host-only cookies must not pass `domain`.
            ...(c.host.startsWith(".") ? { domain: c.host } : {}),
            path: c.path,
            secure: c.secure,
            httpOnly: c.httpOnly,
            expirationDate: c.expiry,
            sameSite: c.sameSite
          });
          imported++;
        } catch {
          failed++;
        }
      })
    );
  }
  if (failed) console.warn(`cookie import: ${imported} imported, ${failed} rejected by Chromium`);
  return { imported, failed, profile: true };
}
