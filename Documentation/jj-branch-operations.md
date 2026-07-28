# Branch operations in jj (Jujutsu)

A guide to branch operations in jj 0.43. The key mental shift: jj calls them
**bookmarks**, and there is *no concept of a current/checked-out branch*. A
bookmark is just a named pointer to a revision — it never moves automatically
as you commit; you move it explicitly when you want it to point somewhere new.

## The basics

```sh
jj bookmark list              # like `git branch` (alias: jj b l)
jj bookmark list --all        # include remote bookmarks

jj bookmark create foo -r @   # like `git branch foo` at the working-copy commit
jj bookmark move foo --to @   # like `git branch -f foo` (add --allow-backwards
                              #   if the move isn't fast-forward)
jj bookmark rename foo bar
jj bookmark delete foo
```

All of these have one-letter shortcuts: `jj b c`, `jj b m`, `jj b d`, etc.

## "Checking out" a branch

Since there's no current branch, you don't check one out — you start a new
change on top of it:

```sh
jj new main        # like `git switch -c topic main` — new empty change atop main
```

Then work, and when done, move the bookmark up to include your commits:

```sh
jj bookmark move main --to @-    # point main at your finished commit
```

A common feature workflow:

```sh
jj new main                  # start work on top of main
# ...edit files (changes are auto-tracked, no `git add`)...
jj describe -m "my change"   # set the commit message
jj bookmark create my-feature -r @
jj git push                  # pushes bookmarks; --bookmark my-feature to be explicit
```

Note that after more commits, `my-feature` stays where you put it — run
`jj bookmark move my-feature --to @` (or `--to @-`) before pushing again.
Bookmarks *do* follow rebases: if you rewrite a commit a bookmark points at,
the bookmark moves with it.

## Remotes

```sh
jj git fetch                             # like git fetch
jj git push --bookmark foo               # push one bookmark
jj git push --all                        # push all tracked bookmarks
jj git push -c @-                        # create+push a generated bookmark for a commit
```

Fetched remote bookmarks are *not* tracked by default (except the default
branch on clone, and anything you push). To work with a colleague's branch:

```sh
jj bookmark track my-feature --remote=origin   # now fetch updates local my-feature
jj new my-feature                              # start work on top of it
jj bookmark untrack my-feature --remote=origin # stop following it
```

You can reference remote positions directly as revisions, e.g.
`jj new main@origin`.

`jj git push` is inherently `--force-with-lease`-safe: it refuses if the
remote moved from jj's last-seen position — just `jj git fetch`, resolve, and
retry. If `jj log` shows `main*`, the local bookmark is ahead of the remote
(needs a push); `main??` means the bookmark is conflicted (updated both
locally and remotely) — fix with `jj bookmark move` after rebasing or merging
the two sides.

## Choosing which remote to push to

For `jj git push` (and `jj git fetch`), the remote is chosen in this order:

1. **Explicit flag wins**: `jj git push --remote <name>` (see remotes with
   `jj git remote list`).
2. **Config**: the `git.push` setting, if set.
3. **Single remote**: if the repo has exactly one remote, it's used
   automatically.
4. **Fallback**: with multiple remotes and no config, jj assumes `origin`,
   just like Git.

The common case where you want to override this is the GitHub fork workflow —
fetch from the upstream project but push to your fork. Set it per-repo:

```sh
jj git remote add upstream https://github.com/project/project.git
jj config set --repo git.fetch "upstream"   # can also be a list: '["origin", "upstream"]'
jj config set --repo git.push "origin"      # push always goes to your fork
```

`git.fetch` accepts multiple remotes or glob/regex patterns; `git.push` is
currently limited to a single remote (as of jj 0.43).

Beyond picking the remote, which *bookmarks* get pushed also matters: bare
`jj git push` only pushes bookmarks that are tracked on that remote and have
moved. So even with several remotes, a bookmark you've only ever pushed to
your fork won't accidentally go to upstream — it isn't tracked there.
`--bookmark <name>`, `--all`, or `--change/-c` control the selection
explicitly.

## Rebase and merge

```sh
jj rebase -b my-feature -o main    # rebase the branch onto main (like git rebase)
jj new @ other-branch              # merge: new change with two parents
```

## Safety net

Anything you do to bookmarks (or anything else) is undoable: `jj undo`
reverts the last operation, and `jj op log` shows the full operation history.

Further reading in the jj repo: `docs/git-comparison.md` for the full git↔jj
command table and `docs/bookmarks.md` for the tracking semantics in depth.
