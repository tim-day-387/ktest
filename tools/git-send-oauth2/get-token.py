#!/usr/bin/env python3
"""Print a fresh Microsoft OAuth2 access token for SMTP (XOAUTH2).

First run performs an interactive device-code login and caches the refresh
token. Subsequent runs refresh silently and print only the access token to
stdout (all prompts go to stderr), so it can be fed straight to
git send-email's --smtp-pass.
"""
import os
import sys
import atexit

import msal

# Thunderbird's public client ID — a well-known public client that Microsoft
# accepts for mail OAuth2 without needing your own Azure app registration.
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
# "organizations" targets Azure AD work/school accounts (custom O365 domains).
AUTHORITY = "https://login.microsoftonline.com/organizations"
# SMTP.Send is the send scope. Do NOT list offline_access/openid/profile here:
# MSAL injects those reserved OIDC scopes itself (and errors if you pass them),
# so the refresh token still comes back.
SCOPES = ["https://outlook.office365.com/SMTP.Send"]

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "token-cache.json"
)


def main():
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        cache.deserialize(open(CACHE_FILE).read())

    def save_cache():
        if cache.has_state_changed:
            fd = os.open(CACHE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(cache.serialize())

    atexit.register(save_cache)

    app = msal.PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            print("Failed to start device flow: %s" % flow, file=sys.stderr)
            sys.exit(1)
        print(flow["message"], file=sys.stderr)
        sys.stderr.flush()
        result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        print(
            "Auth failed: %s: %s"
            % (result.get("error"), result.get("error_description")),
            file=sys.stderr,
        )
        sys.exit(1)

    print(result["access_token"])


if __name__ == "__main__":
    main()
