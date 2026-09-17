#!/usr/bin/env python3
"""One-time OAuth: turn a Google client secret into a long-lived refresh token.

Your subscription list is private data, so there is no API-key shortcut -- the
digest has to act as you. This runs once on your laptop and prints three values
to store as secrets. The token is read-only (`youtube.readonly`): it can list
what you subscribe to and nothing else.

Setup (about five minutes, once):

  1. https://console.cloud.google.com/ -> create or pick a project.
  2. APIs & Services -> Library -> enable "YouTube Data API v3".
  3. APIs & Services -> OAuth consent screen -> External -> fill in the name and
     your email -> add yourself under "Test users". Leave it in Testing mode.
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app.
     Download the JSON as client_secret.json next to this script.
  5. python scripts/auth_youtube.py

Note: a refresh token issued by an app still in "Testing" expires after seven
days. Once the digest is running, publish the consent screen (Publishing status
-> Publish app) to get a token that does not expire. You will see an
"unverified app" warning on the consent screen -- that is expected for a
personal app you wrote yourself, and you can click through it.

If you would rather skip OAuth entirely, export your subscriptions from
https://takeout.google.com (YouTube -> subscriptions) and run:

    digest sources --takeout path/to/subscriptions.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--client-secret",
        default=str(Path(__file__).with_name("client_secret.json")),
        help="Path to the OAuth client JSON downloaded from Google Cloud.",
    )
    parser.add_argument(
        "--console", action="store_true",
        help="Print a URL to paste into a browser instead of opening one "
             "(use on a headless machine).",
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the dependencies first:  pip install -e .", file=sys.stderr)
        return 1

    secret_path = Path(args.client_secret)
    if not secret_path.exists():
        print(f"No client secret at {secret_path}.\n\n{__doc__}", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    if args.console:
        flow.run_console()  # type: ignore[attr-defined]
    else:
        print("Opening your browser to authorise read-only access...\n")
        flow.run_local_server(
            port=0, access_type="offline", prompt="consent",
            authorization_prompt_message="Visit this URL to authorise:\n{url}\n",
            success_message="Authorised. You can close this tab and return to the terminal.",
        )

    credentials = flow.credentials
    if not credentials.refresh_token:
        print(
            "Google did not return a refresh token. Revoke this app at "
            "https://myaccount.google.com/permissions and run again -- Google "
            "only issues one on first consent.",
            file=sys.stderr,
        )
        return 1

    config = json.loads(secret_path.read_text())
    installed = config.get("installed") or config.get("web") or {}

    print("\n" + "=" * 72)
    print("Add these three as GitHub Actions secrets (Settings -> Secrets and")
    print("variables -> Actions), and to your local .env file:")
    print("=" * 72 + "\n")
    print(f"GOOGLE_CLIENT_ID={installed.get('client_id', '')}")
    print(f"GOOGLE_CLIENT_SECRET={installed.get('client_secret', '')}")
    print(f"GOOGLE_REFRESH_TOKEN={credentials.refresh_token}")
    print("\nThen verify with:  digest sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
