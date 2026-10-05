// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import type { SpawnContext, SpawnRequest, TemplateSpec } from "./types";

/** Longest value taken whole from the context (a selection can be a whole page). */
const MAX_VALUE = 80;

const oneLine = (s: string) => s.replace(/\s+/g, " ").trim();

/**
 * Fill a template's variables from what was right-clicked. A variable with a
 * `match` or `pattern` takes the first match found in the text, then in the
 * URL (only in its `from` source if it names one); one with `from` and
 * neither takes that whole source. Anything else is left to its default or
 * the creation form.
 */
export function prefillVars(spec: TemplateSpec, ctx: SpawnContext): Record<string, string> {
  const sources = { text: ctx.text ? oneLine(ctx.text) : "", url: ctx.url ?? "" };
  const out: Record<string, string> = {};
  for (const [name, v] of Object.entries(spec.variables ?? {})) {
    const search = v.match ?? v.pattern;
    if (search) {
      let re: RegExp;
      try {
        re = new RegExp(search);
      } catch {
        continue;
      }
      for (const from of v.from ? [v.from] : (["text", "url"] as const)) {
        const m = re.exec(sources[from]);
        const hit = m?.[1] ?? m?.[0];
        if (hit) {
          out[name] = hit;
          break;
        }
      }
    } else if (v.from && sources[v.from]) {
      out[name] = sources[v.from].slice(0, MAX_VALUE).trim();
    }
  }
  return out;
}

/** True when nothing is left to ask: every variable is prefilled, optional or has a default. */
export function spawnResolved(spec: TemplateSpec, vars: Record<string, string>): boolean {
  return Object.entries(spec.variables ?? {}).every(([name, v]) =>
    Boolean(vars[name] || v.default || v.optional)
  );
}

/**
 * The "New Workspace" submenu for a context: one entry per template, labelled
 * with what the context filled in. Templates the context matched come first.
 */
export function spawnChoices(
  templates: TemplateSpec[],
  ctx: SpawnContext
): { label: string; request: SpawnRequest }[] {
  const choices = templates.map((t) => {
    const vars = prefillVars(t, ctx);
    const hint = Object.values(vars)[0];
    const short = hint && hint.length > 40 ? hint.slice(0, 39) + "…" : hint;
    return {
      label: short ? `${t.name}: ${short}` : t.name,
      request: { template: t.name, vars, url: ctx.url }
    };
  });
  const matched = (c: (typeof choices)[number]) => Object.keys(c.request.vars).length > 0;
  return [...choices.filter(matched), ...choices.filter((c) => !matched(c))];
}
