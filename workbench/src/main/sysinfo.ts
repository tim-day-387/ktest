// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import os from "node:os";
import { readFile } from "node:fs/promises";
import type { SystemStats } from "../shared/types";

/**
 * MemFree and MemAvailable from /proc/meminfo, as top reports them. Node's
 * os.freemem() is already MemAvailable on Linux, so it only serves as the
 * fallback where /proc is unreadable.
 */
async function memInfo(): Promise<{ free: number; available: number }> {
  try {
    const text = await readFile("/proc/meminfo", "utf8");
    const kb = (key: string) => {
      const m = new RegExp(`^${key}:\\s+(\\d+) kB$`, "m").exec(text);
      return m ? Number(m[1]) * 1024 : undefined;
    };
    const free = kb("MemFree");
    const available = kb("MemAvailable");
    if (free !== undefined && available !== undefined) return { free, available };
  } catch {
    // not Linux, or /proc unreadable
  }
  return { free: os.freemem(), available: os.freemem() };
}

/** A top-style snapshot of the host. */
export async function systemStats(): Promise<SystemStats> {
  const [load1, load5, load15] = os.loadavg();
  return {
    uptime: os.uptime(),
    load: [load1, load5, load15],
    mem: { total: os.totalmem(), ...(await memInfo()) }
  };
}
