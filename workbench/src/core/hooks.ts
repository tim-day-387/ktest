// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { spawn } from "node:child_process";

export interface HookResult {
  code: number;
  output: string;
}

/** Run a shell snippet with the workspace environment; never throws, reports the exit code instead. */
export function runHook(
  script: string,
  opts: { cwd: string; env: Record<string, string> }
): Promise<HookResult> {
  return new Promise((resolve) => {
    const child = spawn("/bin/bash", ["-eo", "pipefail", "-c", script], {
      cwd: opts.cwd,
      env: { ...process.env, ...opts.env },
      stdio: ["ignore", "pipe", "pipe"]
    });
    let output = "";
    child.stdout.on("data", (d) => (output += d));
    child.stderr.on("data", (d) => (output += d));
    child.on("error", (err) => resolve({ code: 127, output: output + String(err) }));
    child.on("close", (code) => resolve({ code: code ?? 1, output }));
  });
}
