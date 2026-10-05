// SPDX-License-Identifier: GPL-2.0-only

//
// Author: Timothy Day <tday141@gmail.com>
//

// Download a uBlock Origin release, unpack it into vendor/ublock, and patch in
// the Electron API shim. Run with: npm run fetch-ublock [-- <version>]
import { execFileSync } from "node:child_process";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const VERSION = process.argv[2] ?? "1.75.0";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dest = path.join(root, "vendor", "ublock");
const url = `https://github.com/gorhill/uBlock/releases/download/${VERSION}/uBlock0_${VERSION}.chromium.zip`;

const res = await fetch(url);
if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
const zip = path.join(tmpdir(), `ublock-${VERSION}.zip`);
writeFileSync(zip, Buffer.from(await res.arrayBuffer()));

const unpack = path.join(tmpdir(), `ublock-${VERSION}-unpack`);
rmSync(unpack, { recursive: true, force: true });
mkdirSync(unpack, { recursive: true });
try {
  execFileSync("unzip", ["-q", zip, "-d", unpack]);
} catch {
  execFileSync("python3", [
    "-c",
    `import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])`,
    zip,
    unpack
  ]);
}

rmSync(dest, { recursive: true, force: true });
mkdirSync(path.dirname(dest), { recursive: true });
cpSync(path.join(unpack, "uBlock0.chromium"), dest, { recursive: true });
rmSync(unpack, { recursive: true, force: true });
rmSync(zip, { force: true });
if (!existsSync(path.join(dest, "manifest.json")))
  throw new Error("unpack failed: no manifest.json");

// Patch: shim first, before any of uBlock's own background scripts.
copyFileSync(
  path.join(root, "vendor", "ublock-shim.js"),
  path.join(dest, "js", "electron-shim.js")
);
const html = path.join(dest, "background.html");
const patched = readFileSync(html, "utf8").replace(
  /<script/,
  '<script src="js/electron-shim.js"></script>\n<script'
);
writeFileSync(html, patched);
console.log(`uBlock Origin ${VERSION} installed to ${path.relative(root, dest)}`);
