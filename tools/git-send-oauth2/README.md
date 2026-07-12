# git-send-oauth2

A thin wrapper around `git send-email` that authenticates to Microsoft
Office 365 / Outlook SMTP using OAuth2 (XOAUTH2) instead of a password.
Microsoft has disabled basic auth for SMTP, so a plain
`sendemail.smtppass` no longer works — you need a bearer token. This
tool fetches a fresh token and hands it to `git send-email`.

- `get-token.py` — performs the OAuth2 device-code login (first run),
  caches the refresh token, and prints a fresh access token on stdout.
  Uses Thunderbird's public client ID, so no Azure app registration is
  needed.
- `git-send-oauth2.sh` — calls `get-token.py`, then execs
  `git send-email --smtp-auth=XOAUTH2 --smtp-pass=<token> "$@"`.

Everything you'd pass to `git send-email` (recipients, patch files,
`--dry-run`, ...) you pass to `git-send-oauth2.sh` unchanged.

## One-time setup

1. Install the Python dependency:

   ```sh
   pip install --user msal
   ```

2. Configure the SMTP server and your identity in git. This only needs
   to be done once (it's already set globally on this machine):

   ```sh
   git config --global sendemail.smtpserver     smtp.office365.com
   git config --global sendemail.smtpserverport 587
   git config --global sendemail.smtpencryption tls
   git config --global sendemail.smtpauth       XOAUTH2
   git config --global sendemail.smtpuser       you@example.com
   ```

   Note there is **no** `sendemail.smtppass` — the token is injected by
   the wrapper at send time.

3. First authentication. Run the token helper once on its own; it will
   print a URL and a device code. Open the URL, enter the code, and log
   in with your Office 365 account:

   ```sh
   /path/to/ktest/tools/git-send-oauth2/get-token.py >/dev/null
   ```

   The refresh token is cached in `token-cache.json` (mode 0600, and
   `.gitignore`'d — never commit it). After this, tokens refresh
   silently and no browser step is needed again until the refresh token
   expires.

## Submitting a patch series to the kernel

The example below walks through the ext2 context-analysis series that
lives in `~/patches/ext2-context/` (9 patches + a cover
letter), but the steps are the same for any series.

### 1. Generate the patches

From your kernel tree, on the branch with your commits:

```sh
cd ~/linux
git format-patch --cover-letter -o ~/patches/ext2-context/ \
    -v1 7.2-rc2..HEAD
```

- `--cover-letter` produces `0000-cover-letter.patch` (the `[PATCH 0/N]`
  intro). Edit it to fill in the `*** SUBJECT HERE ***` and
  `*** BLURB HERE ***` placeholders — this is where you explain the
  motivation for the whole series.
- `-v2`, `-v3`, ... on later revisions produces `[PATCH v2 …]` subjects
  and `v2-0001-…` filenames.

### 2. Check the patches

Run checkpatch over the series before sending (there's a helper in this
repo):

```sh
cd ~/linux
/path/to/ktest/tools/checkpatch-parallel ~/patches/ext2-context/0*.patch
```

Fix anything it flags, then re-generate.

### 3. Work out the recipients

Kernel etiquette: put the maintainers who must act on the patch (and the
subsystem list) in **To:**, and everyone else `get_maintainer.pl`
suggests — reviewers, related lists — in **Cc:**.

```sh
cd ~/linux
./scripts/get_maintainer.pl --nogit --nogit-fallback \
    ~/patches/ext2-context/0*.patch
```

For this ext2 series that yields:

```
Subsystem Maintainer <maintainer@example.com>  (maintainer: EXT2 FILE SYSTEM)  -> To
linux-ext4@vger.kernel.org                     (open list:   EXT2 FILE SYSTEM)  -> To
linux-kernel@vger.kernel.org                   (open list)                      -> Cc
LLVM Maintainer <llvm-maint@example.com>        (maintainer:  CLANG/LLVM)        -> Cc
LLVM Reviewer One <llvm-rev1@example.com>        (reviewer: CLANG/LLVM)          -> Cc
LLVM Reviewer Two <llvm-rev2@example.com>        (reviewer:    CLANG/LLVM)       -> Cc
LLVM Reviewer Three <llvm-rev3@example.com>      (reviewer:    CLANG/LLVM)       -> Cc
llvm@lists.linux.dev                           (open list:   CLANG/LLVM)        -> Cc
```

Since this work builds directly on an upstream context-analysis
series, also Cc its author `Series Author <author@example.com>` and,
because it's a filesystem-wide direction, `linux-fsdevel@vger.kernel.org`.

### 4. Dry run

Always dry-run first. This shows the exact envelope (From/To/Cc, the
threading headers, the SMTP conversation) **without sending anything**,
and it still fetches a token so you know auth works:

```sh
cd ~/patches/ext2-context
/path/to/ktest/tools/git-send-oauth2/git-send-oauth2.sh \
    --dry-run \
    --to="Subsystem Maintainer <maintainer@example.com>" \
    --to="linux-ext4@vger.kernel.org" \
    --cc="linux-kernel@vger.kernel.org" \
    --cc="LLVM Maintainer <llvm-maint@example.com>" \
    --cc="LLVM Reviewer One <llvm-rev1@example.com>" \
    --cc="LLVM Reviewer Two <llvm-rev2@example.com>" \
    --cc="LLVM Reviewer Three <llvm-rev3@example.com>" \
    --cc="Series Author <author@example.com>" \
    --cc="llvm@lists.linux.dev" \
    --cc="linux-fsdevel@vger.kernel.org" \
    0*.patch
```

Confirm the cover letter is `0/9`, the patches are `1/9`..`9/9`, and the
`In-Reply-To`/`References` headers chain them all under the cover
letter.

### 5. Send

When the dry run looks right, remove `--dry-run`. Optionally add
`--annotate` to open each message in your editor for one last look
before it goes out:

```sh
cd ~/patches/ext2-context
/path/to/ktest/tools/git-send-oauth2/git-send-oauth2.sh \
    --annotate \
    --to="Subsystem Maintainer <maintainer@example.com>" \
    --to="linux-ext4@vger.kernel.org" \
    --cc="linux-kernel@vger.kernel.org" \
    --cc="LLVM Maintainer <llvm-maint@example.com>" \
    --cc="LLVM Reviewer One <llvm-rev1@example.com>" \
    --cc="LLVM Reviewer Two <llvm-rev2@example.com>" \
    --cc="LLVM Reviewer Three <llvm-rev3@example.com>" \
    --cc="Series Author <author@example.com>" \
    --cc="llvm@lists.linux.dev" \
    --cc="linux-fsdevel@vger.kernel.org" \
    0*.patch
```

`git send-email` automatically Cc's anyone in the patches'
`Signed-off-by`/`Reviewed-by` trailers as well, so late reviewers stay
on thread.

> Tip: for a real first send, mail the series to **yourself only** first
> (`--to=you@example.com`, no other recipients) and read
> it in your mail client. It's the surest way to catch a mangled cover
> letter or broken threading before the list sees it.

### 6. Later revisions (v2, v3, ...)

- Regenerate with `-v2` (see step 1).
- Address the feedback and add a "Changes since v1" section to the cover
  letter.
- Thread the new series under the previous discussion by pointing at the
  Message-ID of the v1 cover letter on lore:

  ```sh
  /path/to/ktest/tools/git-send-oauth2/git-send-oauth2.sh \
      --in-reply-to="<message-id-of-v1-cover@...>" \
      ... v2-0*.patch
  ```

## Troubleshooting

- **Device-code prompt appears every time** — the cache file is missing
  or unwritable. Check that `token-cache.json` exists next to the
  scripts and is mode 0600.
- **`Auth failed: invalid_grant`** — the refresh token expired or was
  revoked. Delete `token-cache.json` and re-run `get-token.py` to log in
  again.
- **`msal` not found** — `pip install --user msal`.
- **Recipients rejected / mail bounces** — confirm `sendemail.smtpuser`
  matches the account you authenticated as; the `From:` on your patches
  must be that same address.
