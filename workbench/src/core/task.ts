// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import YAML from "yaml";

export interface TaskDoc {
  meta: Record<string, unknown>;
  body: string;
}

export function serializeTask(doc: TaskDoc): string {
  return `---\n${YAML.stringify(doc.meta).trimEnd()}\n---\n${doc.body}`;
}
