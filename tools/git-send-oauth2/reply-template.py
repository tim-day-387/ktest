#!/usr/bin/env python3
"""Generate a git-send-email reply template from an email.

The input identifies the message you want to answer. It can be:

  - a lore.kernel.org message permalink, e.g.
      https://lore.kernel.org/some-list/<msgid>/
  - a bare Message-ID (with or without angle brackets),
  - a path to an .eml or mbox file, or
  - '-' to read one raw message from stdin.

The output is a ready-to-edit .eml with the From/To/Cc/Subject and the
In-Reply-To/References threading headers filled in for a reply-all, plus
the original message quoted. Edit the body between the quote and the
signature, then send it with git-send-oauth2.sh:

    reply-template.py <msgid|url|file> -o reply.eml
    # edit reply.eml
    ./git-send-oauth2.sh --dry-run reply.eml
    ./git-send-oauth2.sh reply.eml

Uses only the Python standard library.
"""

import argparse
import email
import email.policy
import email.utils
import mailbox
import os
import subprocess
import sys
import urllib.parse
import urllib.request

POLICY = email.policy.default


def git_config(key):
    """Return a git config value, or None if unset."""
    try:
        out = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return out or None
    except OSError:
        return None


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "reply-template"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_message(src, select):
    """Load the message to reply to as an email.message.EmailMessage."""
    if src == "-":
        return email.message_from_binary_file(sys.stdin.buffer, policy=POLICY)

    if src.startswith(("http://", "https://")):
        url = src.split("#", 1)[0].split("?", 1)[0]
        if not url.rstrip("/").endswith("/raw"):
            url = url.rstrip("/") + "/raw"
        return email.message_from_bytes(fetch(url), policy=POLICY)

    if os.path.exists(src):
        with open(src, "rb") as f:
            is_mbox = f.read(5) == b"From "
        if is_mbox:
            return pick_from_mbox(src, select)
        with open(src, "rb") as f:
            return email.message_from_binary_file(f, policy=POLICY)

    # Otherwise treat it as a Message-ID and resolve it on lore.
    mid = src.strip().lstrip("<").rstrip(">")
    url = "https://lore.kernel.org/all/%s/raw" % urllib.parse.quote(mid)
    return email.message_from_bytes(fetch(url), policy=POLICY)


def pick_from_mbox(path, select):
    """Choose one message from an mbox: --select by Message-ID, else the last."""
    msgs = list(mailbox.mbox(path))
    if not msgs:
        sys.exit("error: no messages found in %s" % path)
    if select:
        want = select.strip().lstrip("<").rstrip(">")
        for m in msgs:
            mid = (m.get("Message-ID") or "").strip().lstrip("<").rstrip(">")
            if mid == want:
                return email.message_from_bytes(m.as_bytes(), policy=POLICY)
        sys.exit("error: no message with Message-ID <%s> in %s" % (want, path))
    m = msgs[-1]
    if len(msgs) > 1:
        subj = " ".join((m.get("Subject", "") or "").split())
        print("note: %d messages in mbox; replying to the last one:\n"
              "      %s\n      (use --select <msgid> to choose another)"
              % (len(msgs), subj), file=sys.stderr)
    return email.message_from_bytes(m.as_bytes(), policy=POLICY)


def my_addresses():
    """Addresses that identify us, so we never To:/Cc: ourselves."""
    addrs = set()
    for key in ("sendemail.smtpuser", "user.email"):
        val = git_config(key)
        if val:
            addrs.add(val.lower())
    return addrs


def my_from():
    """The From: line for our reply (must match the auth account)."""
    addr = git_config("sendemail.smtpuser") or git_config("user.email") or "you@example.com"
    name = git_config("user.name") or ""
    return email.utils.formataddr((name, addr))


def reply_recipients(msg, mine):
    """(to, cc) for a reply-all: author -> To, everyone else -> Cc."""
    to = msg.get("From", "").strip()

    seen = set(a.lower() for _, a in email.utils.getaddresses([to]))
    seen |= mine
    cc = []
    for name, addr in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])):
        low = addr.lower()
        if not addr or low in seen:
            continue
        seen.add(low)
        cc.append(email.utils.formataddr((name, addr)))
    return to, cc


def reply_subject(msg):
    subj = (msg.get("Subject", "") or "").strip()
    subj = " ".join(subj.split())  # unfold
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    return subj


def reply_references(msg):
    """Parent's References chain plus its own Message-ID, in order."""
    mid = (msg.get("Message-ID") or "").strip()
    refs = (msg.get("References") or "").split()
    out = []
    for r in refs + ([mid] if mid else []):
        if r and r not in out:
            out.append(r)
    return out, mid


def quoted_body(msg):
    """The parent's plain-text body, each line prefixed for quoting."""
    try:
        body = msg.get_body(preferencelist=("plain",))
        text = body.get_content() if body else ""
    except (KeyError, LookupError, ValueError):
        text = ""
    if not text:
        text = "(could not extract a text/plain body to quote)"
    lines = text.replace("\r\n", "\n").rstrip().split("\n")
    return "\n".join(("> " + ln).rstrip() for ln in lines)


def fold_addrs(addrs):
    """One address per line after the first, indented like git format-patch."""
    return ",\n        ".join(addrs)


def build(msg):
    mine = my_addresses()
    to, cc = reply_recipients(msg, mine)
    refs, mid = reply_references(msg)

    who = email.utils.parseaddr(msg.get("From", ""))
    who = who[0] or who[1] or "the author"
    when = (msg.get("Date", "") or "").strip()
    attribution = "On %s, %s wrote:" % (when, who) if when else "%s wrote:" % who

    lines = []
    lines.append("From: %s" % my_from())
    lines.append("To: %s" % to)
    if cc:
        lines.append("Cc: %s" % fold_addrs(cc))
    lines.append("Subject: %s" % reply_subject(msg))
    if mid:
        lines.append("In-Reply-To: %s" % mid)
    if refs:
        lines.append("References: " + "\n ".join(refs))
    lines.append("")
    lines.append(attribution)
    lines.append(quoted_body(msg))
    lines.append("")
    lines.append("<< write your reply here >>")
    lines.append("")
    lines.append("Thanks,")
    lines.append((git_config("user.name") or "").split(" ")[0] or "")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(
        description="Generate a git-send-email reply template from an email.")
    ap.add_argument("source",
                    help="lore URL, Message-ID, .eml/mbox path, or '-' for stdin")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="write template here (default: stdout)")
    ap.add_argument("--select", metavar="MSGID",
                    help="when SOURCE is an mbox, reply to this Message-ID")
    args = ap.parse_args()

    try:
        msg = load_message(args.source, args.select)
    except urllib.error.URLError as e:
        sys.exit("error: could not fetch message: %s" % e)

    template = build(msg)

    if args.output:
        with open(args.output, "w") as f:
            f.write(template)
        print("wrote %s" % args.output, file=sys.stderr)
    else:
        sys.stdout.write(template)


if __name__ == "__main__":
    main()
