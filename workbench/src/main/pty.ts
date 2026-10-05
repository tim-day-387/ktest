// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import * as pty from "node-pty";

export interface PtyOptions {
  shell: string;
  cwd: string;
  env: Record<string, string>;
  command?: string;
  cols: number;
  rows: number;
}

/** Terminal sessions keyed by "<workspaceId>:<pane>". */
export class PtyHost {
  private ptys = new Map<string, pty.IPty>();

  constructor(
    private onData: (key: string, data: string) => void,
    private onExit: (key: string, code: number) => void
  ) {}

  has(key: string): boolean {
    return this.ptys.has(key);
  }

  open(key: string, opts: PtyOptions): void {
    if (this.ptys.has(key)) return;
    const proc = pty.spawn(opts.shell, [], {
      name: "xterm-256color",
      cols: opts.cols,
      rows: opts.rows,
      cwd: opts.cwd,
      env: { ...process.env, ...opts.env, TERM: "xterm-256color" } as Record<string, string>
    });
    proc.onData((d) => this.onData(key, d));
    proc.onExit(({ exitCode }) => {
      this.ptys.delete(key);
      this.onExit(key, exitCode);
    });
    if (opts.command) proc.write(opts.command + "\n");
    this.ptys.set(key, proc);
  }

  write(key: string, data: string): void {
    this.ptys.get(key)?.write(data);
  }

  resize(key: string, cols: number, rows: number): void {
    if (cols > 0 && rows > 0) this.ptys.get(key)?.resize(cols, rows);
  }

  killWorkspace(wsId: string): void {
    for (const [key, proc] of this.ptys) {
      if (key.startsWith(`${wsId}:`)) {
        proc.kill();
        this.ptys.delete(key);
      }
    }
  }

  killAll(): void {
    for (const proc of this.ptys.values()) proc.kill();
    this.ptys.clear();
  }
}
