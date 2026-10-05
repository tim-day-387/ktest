// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import YAML from "yaml";
import type { TemplateSpec } from "../shared/types";

/** Load *.yaml templates from each dir in order; later dirs override earlier ones by name. */
export async function loadTemplates(dirs: string[]): Promise<Map<string, TemplateSpec>> {
  const out = new Map<string, TemplateSpec>();
  for (const dir of dirs) {
    let entries: string[];
    try {
      entries = await readdir(dir);
    } catch {
      continue;
    }
    for (const file of entries.sort()) {
      if (!/\.ya?ml$/.test(file)) continue;
      const full = path.join(dir, file);
      // A broken user template must not keep the app from starting: warn and skip it.
      try {
        const spec = YAML.parse(await readFile(full, "utf8")) as TemplateSpec | null;
        if (!spec || typeof spec.name !== "string") throw new Error("template has no name");
        out.set(spec.name, spec);
      } catch (err) {
        console.warn(`${full}: skipped: ${(err as Error).message}`);
      }
    }
  }
  return out;
}

/** Substitute {{name}} placeholders. Unknown names are left untouched. */
export function render(text: string, vars: Record<string, string>): string {
  return text.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (m, key: string) =>
    Object.hasOwn(vars, key) ? vars[key] : m
  );
}

/** Names of the variables the text uses. */
export function varsUsed(text: string): string[] {
  return [...text.matchAll(/\{\{\s*([\w.]+)\s*\}\}/g)].map((m) => m[1]);
}

/** True when the text uses a variable that is empty, i.e. an optional one left out. */
export function usesEmptyVar(text: string, vars: Record<string, string>): boolean {
  return varsUsed(text).some((name) => vars[name] === "");
}

/** Render each value; an entry that uses an empty variable is dropped. */
export function renderRecord(
  rec: Record<string, string> | undefined,
  vars: Record<string, string>
) {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(rec ?? {}))
    if (!usesEmptyVar(v, vars)) out[k] = render(v, vars);
  return out;
}

/** Apply defaults and validate user-supplied variables against the template's spec. */
export function resolveVars(
  spec: TemplateSpec,
  given: Record<string, string>
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, v] of Object.entries(spec.variables ?? {})) {
    const value = given[name]?.trim() || v.default;
    if (value === undefined || value === "") {
      if (!v.optional) throw new Error(`variable "${name}" is required`);
      out[name] = "";
      continue;
    }
    if (v.pattern && !new RegExp(`^(?:${v.pattern})$`).test(value)) {
      throw new Error(`variable "${name}" must match /${v.pattern}/`);
    }
    out[name] = value;
  }
  return out;
}
