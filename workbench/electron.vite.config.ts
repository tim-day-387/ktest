// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

import path from "node:path";
import { defineConfig, externalizeDepsPlugin } from "electron-vite";

export default defineConfig({
  main: { plugins: [externalizeDepsPlugin()] },
  preload: { plugins: [externalizeDepsPlugin()] },
  // The renderer bundles the Gerrit filter of the ktest status site; the dev
  // server only serves files outside the project when told to.
  renderer: {
    server: {
      fs: {
        allow: [import.meta.dirname, path.join(import.meta.dirname, "../ci-lustre/static-site")]
      }
    }
  }
});
