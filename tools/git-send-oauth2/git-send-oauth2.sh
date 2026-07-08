#!/usr/bin/env bash
# Wrapper: fetch a fresh Microsoft OAuth2 token, then run git send-email with it.
# Usage: git-send-oauth2.sh [any git send-email args...]
#   e.g. git-send-oauth2.sh --to="~sircmpwn/email-test-drive@lists.sr.ht" HEAD^
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Acquire token (device-code prompt, if any, goes to the terminal via stderr).
TOKEN="$("$DIR/get-token.py")"

exec git send-email \
    --smtp-auth=XOAUTH2 \
    --smtp-pass="$TOKEN" \
    "$@"
