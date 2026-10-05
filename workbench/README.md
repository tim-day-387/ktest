# Workbench

Part of ktest (`workbench/`). Desktop app for on-demand, templated workspaces. Each workspace has an
embedded browser, one or more terminal panes, and a `task.md` descriptor.

## Build

From the ktest top level (node/npm >= 22 on PATH; `~/.local/node` here):

```
make workbench            # deps (npm install, vendor uBlock Origin) + build
make workbench-install    # package and install under ~/.local (no root)
make workbench-dist       # .deb and AppImage in workbench/dist/
```

Inside `workbench/`: `make dev` (dev server with HMR, data in
~/.config/workbench-dev), `make check` (typecheck), `make fmt` (prettier),
`make clean`, `make distclean` (also drops node_modules and vendor/ublock).
`make V=1` shows the npm output.

## Install

```
make workbench-install
workbench                 # or launch "Workbench" from the app grid
```

The installed app keeps its data in `~/.config/workbench`; the dev build uses
`~/.config/workbench-dev`, so both can run at once. Re-run the install after
changes to update the installed copy. Bundled templates and uBlock are
shipped as plain files under `resources/` (Chromium cannot load an extension
from inside app.asar).

## Layout

```
src/core/      lifecycle, templates, task file, hooks   (no Electron imports)
src/main/      Electron main: window, WebContentsView per workspace, pty host, IPC
src/preload/   contextBridge -> window.workbench
src/renderer/  two-pane UI: sidebar | browser + terminal, or the home view
src/shared/    types shared by all layers
templates/     built-in templates; ~/.config/workbench/templates overrides by name
```

State lives in `~/.config/workbench/workspaces/<id>/` as `workspace.json`
and `task.md`. The directory is the source of truth.

## Templates

The home view's "Single workspace" spawner is a preset picker listing every
template; a preset without variables creates the workspace immediately. See
Home for the other spawner.

```yaml
name: lustre-fix
default: true         # listed first in the pickers and menus (lustre-review is)
variables:            # form fields at creation time; {{name}} substitutes everywhere below
  change: { prompt: Gerrit change, pattern: "[0-9]+" }
  ticket: { prompt: Jira ticket, pattern: "LU-[0-9]+", optional: true }   # may be left empty
  purpose: { prompt: Purpose, default: scratch, from: text }   # right-click spawn, see Context menus
task:
  title: "Gerrit {{change}} {{ticket}}"
  body: "..."
browser:
  profile: isolated   # isolated | shared | ephemeral  (session partition)
  tabs: [https://jira.whamcloud.com/browse/{{ticket}}]   # dropped when ticket is left empty
  bookmarks: [TLC/Outlook, TLC/Teams]   # or a folder name; Firefox toolbar bookmarks opened as tabs
terminal:
  cwd: "{{workspace_dir}}/lustre"   # default: ~ (a leading ~ is expanded)
  env: { KTEST_JOB: "{{ticket}}" }   # likewise not set
  panes: [{ name: shell }, { name: agent, command: claude }]   # command is typed into the interactive shell
hooks:                # bash; {{vars}} are substituted, and WORKSPACE_ID/DIR, TASK_FILE are in env
  create: git worktree add ...   # non-zero exit -> workspace state "failed"
  destroy: git worktree remove ...   # non-zero exit -> workspace kept, "destroy anyway?" asked
```

Built-in variables: `workspace_id`, `workspace_dir`, `task_file`. Hooks and
terminals receive `WORKSPACE_ID`, `WORKSPACE_DIR` and `TASK_FILE` in their
environment. That is the whole contract with whatever runs inside.

## lustre-fix

The `lustre-fix` template makes a git worktree of `repo` with a Gerrit change
checked out. The change is required (`68716`, or `68716/2` for a patchset);
the Jira ticket is optional:

- The branch is named after the ticket, or `change-68716` without one. It
  starts at the fetched patchset, so `repo` needs no local base branch.
- The change's Gerrit page opens as a tab; with a ticket, so does its Jira
  page, which is also linked from `task.md`.
- If the branch already exists it is used as it is, and no patch is fetched.

What was done is appended to `task.md` under "Gerrit". Right-clicking a
Gerrit change link fills in the change, and the ticket when the link text
carries it, and creates the workspace at once; from a ticket alone the form
opens to ask for the change. The patch is fetched anonymously over https; the
hook needs `curl` and `jq`. Upload with `git review` from the worktree as
usual.

Destroying the workspace discards no work. Uncommitted changes, untracked
files included, go to a stash in `repo` ("workbench: <branch> workspace
destroyed", see `git stash list`). The branch is deleted only if the
workspace created it and nothing was committed on it since. If the hook
fails, the workspace is kept and the app asks whether to destroy it anyway.

## lustre-review

The `lustre-review` template takes the same change and ticket as `lustre-fix`
but only opens the pages: the change in Gerrit and, with a ticket, its Jira
page. Nothing is checked out and it has no hooks, so destroying the workspace
touches no repo. It is marked `default: true`, which lists it ahead of
`lustre-fix` wherever both apply: a right-clicked Gerrit link or ticket, the
preset picker, and the template of "Workspaces from Gerrit".

## Home

The "Home" button in the sidebar shows the home view in place of a workspace;
it is also what the app starts on. The sidebar with the workspaces stays
visible. Home holds the two spawners, one above the other: "Single workspace",
the preset picker, and below it "Workspaces from Gerrit", which spawns a
collection of workspaces, one per open Gerrit change.

"Workspaces from Gerrit" lists the open changes of fs/lustre-release master,
queried anonymously when the app starts ("Refresh" asks again), with the filters
of the ktest status site: profiles ("Timothy Day - Need review", "Review group
- Need review", "All"), authors, ready to land, and Maloo result. The profiles
and the filter itself are the site's own code, `ci-lustre/static-site/gerrit-filter.js`,
bundled into the app; the query follows `pk patch-status` (`src/core/gerrit.ts`).
The site's filters on ktest runs are left out, as workbench has no run data.

Every listed change that is ticked becomes a workspace of the chosen
template, filled in exactly as if its Gerrit link had been right-clicked (see
Context menus): subject as the text, change page as the URL. Only templates
that this fills in completely are offered, `lustre-review` being the one
selected at first; it and `lustre-fix` get the change and the ticket of the
subject. A change that already has such a workspace names it
in the last column and starts unticked. Workspaces are created one after
another, after a confirmation, and appear in the sidebar as they are made. At
the end the first one is opened, unless one failed (the errors are then listed)
or you have opened a workspace meanwhile.

## Context menus

Right-click works everywhere, with native menus:

- Web page: link (open in new tab, copy address), image, selection (copy, web
  search), text field (undo/cut/copy/paste, spelling), page (back, forward,
  reload, copy address), Inspect Element.
- Sidebar: open, copy path or destroy a workspace; new workspace from any template.
  Destroying the workspace on view moves to the one below it in the sidebar
  (above it for the last one, home when it was the only one).
- Browser tab: new, duplicate, close, close others, copy address.
- Bookmark: open, open in new tab, copy address.
- URL bar and form fields: editing, plus "Paste and Go" in the URL bar.
- Terminal: copy, paste, select all, clear, open a selected URL in a new tab.
  A program that tracks the mouse (vim, tmux) gets the click itself;
  Shift+right-click forces the menu.

Links, pages, selections (page or terminal), tabs and bookmarks also offer a
"New Workspace" submenu listing every template. The workspace is spawned from
what was clicked:

- A variable with a `pattern` takes the first match in the clicked text
  (selection, link text, page title), else in the URL. Selecting `LU-12345`, or
  right-clicking a link to `.../browse/LU-12345`, offers "lustre-review: LU-12345"
  and "lustre-fix: LU-12345".
- A variable with `from: text` or `from: url` and no pattern takes that whole
  value (text is cut to one line of 80 characters). With a pattern, `from`
  limits the search to that source.
- `match` is a regex searched in place of `pattern`; its first capture group is
  the value. It picks a value out of a URL whose pattern alone would be
  ambiguous, e.g. the change number of a Gerrit link.
- A clicked URL opens as the new workspace's first tab, ahead of the template's own.
  A template tab the URL already shows is left out: one on the same host whose
  variables the URL fills as the tab's own address does, e.g. the change's tab
  for a link to one of its patchsets or files.

If that leaves no variable without a value or default (`optional` ones
aside), the workspace is created and selected at once; otherwise the creation
form opens with the matches filled in. From the sidebar there is no context,
so a template with variables always shows its form.

The clicked text and URL come from a web page. A variable filled from them
that ends up in a hook or a pane command should have a `pattern`, which is
checked against the whole value before anything runs.

## Browser tabs

Each workspace has its own tab strip. Template `browser.tabs`, followed by any
`browser.bookmarks`, are opened on first show; afterwards the open tabs are
saved to `workspace.json` and restored next time (lazily: a restored tab loads when first activated).
Links that open a new window, and Ctrl- or middle-clicked bookmarks, open in a
new tab. Shortcuts, also while a page has focus: Ctrl+T new tab, Ctrl+W close,
Ctrl+Tab / Ctrl+Shift+Tab switch, Ctrl+L focus the URL bar, Ctrl+= / Ctrl+- /
Ctrl+0 zoom the page (Chromium remembers zoom per site within a workspace's
profile). A workspace always keeps at least one tab.

Full screen mode (the ⛶ button or F11; F11 leaves, as does Escape outside the
terminal) puts the whole app window fullscreen. A page's own fullscreen request, such as a video
player's button, additionally hides the shell so the page fills the screen.

In a terminal, Ctrl+= / Ctrl+- / Ctrl+0 change the font size (default 15px,
remembered across runs).

## Firefox bookmarks

The Firefox "Bookmarks Toolbar" folder is rendered as a toolbar under the
URL bar, read-only, via Node's built-in sqlite. Folders open as native
dropdown menus; items that do not fit collapse into a » menu. The bar
refreshes when the window regains focus. The profile
is found through `profiles.ini` under `~/.config/mozilla/firefox` (Firefox
139+), `~/.mozilla/firefox`, or the snap and flatpak locations; set `WORKBENCH_FIREFOX_DIR` to point elsewhere. The
database is snapshotted before reading, so it works while Firefox is running.

## Firefox cookies

The first time a workspace's browser opens in an app run, Firefox's
first-party persistent cookies are copied into that workspace's session
partition, so cookie-based logins carry over. The "Cookies" button re-imports
and reloads. Session cookies (no expiry) and cookies Firefox partitioned under
another site or container are not copied. Disable per template with
`browser.cookies: none`.

## uBlock Origin

```
npm run fetch-ublock      # downloads uBlock Origin into vendor/ublock (gitignored)
```

uBlock is loaded into every persistent browser partition on first open. The
"uBlock" button opens its dashboard (filter lists, trusted sites, my filters)
for the current workspace; each isolated profile has its own uBlock state.
Disable per template with `browser.adblock: false`.

How it works: Electron supports `chrome.webRequest` blocking, runtime
messaging with a real `sender.tab`, `tabs.executeScript/get/query` and
`storage.local`, but not `browserAction`, `webNavigation`, `contextMenus`,
`privacy`, `windows`, the `tabs.*` events or `tabs.insertCSS`, and it exposes a
`browser` alias that uBlock adopts. `vendor/ublock-shim.js` is patched in as
the first background script: it stubs the missing namespaces on both aliases,
implements `insertCSS`/`removeCSS` via `executeScript`, and synthesizes
`webNavigation.onCommitted` and `tabs.onUpdated` from each content script
connection. Those events are what make uBlock create a page store for the
tab, so cosmetic filtering and scriptlet injection (which is what removes
YouTube ads) work, not only network blocking. On first run in a partition
uBlock fetches its lists and restarts itself, so filtering starts a few
seconds after the first page.

Constraints: never register `session.webRequest` handlers on a workspace
session (Electron then bypasses the extension's listeners entirely), and
extensions cannot load into ephemeral (non-persist) partitions. The toolbar
popup and badge are unavailable; use the dashboard. Scriptlets are injected
slightly later than in Chrome (on content script connect rather than on
navigation commit), so a page that runs its ad code inline before that may
still show an ad occasionally.

## Next steps

1. tmux-backed panes so terminals survive app restarts
2. Unix-socket API and `wsctl` CLI
3. Container-backed terminals (podman) as a template option
