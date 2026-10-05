#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Install the packaged app (dist/linux-unpacked) under ~/.local, no root needed:
#   ~/.local/lib/workbench/        the app
#   ~/.local/bin/workbench         launcher symlink
#   ~/.local/share/applications/   desktop entry (shows up in the app grid)
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/dist/linux-unpacked"
[ -x "$src/workbench" ] || { echo "no packaged app at $src; run: npm run install-local" >&2; exit 1; }
lib="$HOME/.local/lib/workbench"
rm -rf "$lib" && mkdir -p "$(dirname "$lib")" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/512x512/apps"
cp -a "$src" "$lib"
ln -sfn "$lib/workbench" "$HOME/.local/bin/workbench"
cp "$root/build/icon.png" "$HOME/.local/share/icons/hicolor/512x512/apps/workbench.png"
cat > "$HOME/.local/share/applications/workbench.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Workbench
Comment=Templated workspaces with a browser, terminal and task
Exec=$lib/workbench %U
Icon=workbench
Terminal=false
Categories=Development;
StartupWMClass=workbench
DESKTOP
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
# Icon themes are rescanned when the theme dir's mtime changes; replacing the
# png in place leaves it untouched, so the shell would keep the old icon.
touch "$HOME/.local/share/icons/hicolor"
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "installed: $lib  (launcher: ~/.local/bin/workbench, desktop entry: Workbench)"
