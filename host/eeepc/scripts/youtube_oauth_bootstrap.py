#!/usr/bin/env python3
"""One-time OAuth bootstrap for the channel credential (#1612, ADR-015 rule 3).

Run this ONCE, on a machine with a browser -- not on the eeePC. It produces the
refresh token the publishing unit uses headlessly from then on.

    python3 youtube_oauth_bootstrap.py --client-secret client_secret.json

The scopes are fixed in this file and are the only two the host is ever granted:

    youtube.upload          upload a video and set its title and description
    yt-analytics.readonly   read numbers; the API returns no third-party text

Comment, community and subscription scopes are deliberately absent. That is the
injection defence: the credential cannot address those endpoints, so no rule has
to forbid using them. Broader work -- the channel card, caption tracks -- runs
under a separate operator-held credential that never reaches the host.

Standard library only, so it runs wherever python3 does.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# The complete set. Adding to it is a decision, not a convenience.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)

ENV_PATH = "/etc/eeepc-agent/youtube.env"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the single redirect Google makes back to the loopback address."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        body = b"Authorisation received. Close this tab and return to the terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature fixed by the base class
        """Silence the default per-request stderr logging."""


def _load_client(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # Desktop-app client secrets nest under "installed"; accept "web" too.
    section = data.get("installed") or data.get("web")
    if not section:
        raise SystemExit(
            f"{path}: expected a Desktop app client secret with an 'installed' section"
        )
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not client_id or not client_secret:
        raise SystemExit(f"{path}: client_id or client_secret missing")
    return client_id, client_secret


def _post_form(url: str, fields: dict[str, str]) -> dict:
    payload = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"token endpoint returned {exc.code}: {detail}") from exc


def _authorise(client_id: str, client_secret: str, port: int) -> str:
    """Run the loopback flow and return the refresh token."""
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{port}/"
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # offline + consent together are what actually produce a refresh
            # token; without prompt=consent a re-authorisation returns none.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    url = f"{AUTH_ENDPOINT}?{query}"

    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Opening the consent screen. If nothing opens, visit:\n")
    print(f"  {url}\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise SystemExit("timed out waiting for the redirect")
    if "error" in result:
        raise SystemExit(f"authorisation refused: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("state mismatch; discarding this response")

    tokens = _post_form(
        TOKEN_ENDPOINT,
        {
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "no refresh_token returned. This happens when the app has already "
            "been authorised without prompt=consent; revoke it at "
            "https://myaccount.google.com/permissions and run again."
        )
    return refresh_token


def _verify(client_id: str, client_secret: str, refresh_token: str) -> list[str]:
    """Exchange the refresh token once and return the scopes actually granted."""
    tokens = _post_form(
        TOKEN_ENDPOINT,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    return sorted((tokens.get("scope") or "").split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-secret",
        type=Path,
        required=True,
        help="Desktop app client secret JSON downloaded from Google Cloud",
    )
    parser.add_argument(
        "--channel-id", default="", help="channel ID, written into the env file"
    )
    parser.add_argument("--port", type=int, default=8731, help="loopback port")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("youtube.env"),
        help="local file to write; copy it to the host yourself",
    )
    args = parser.parse_args()

    client_id, client_secret = _load_client(args.client_secret)
    refresh_token = _authorise(client_id, client_secret, args.port)

    granted = _verify(client_id, client_secret, refresh_token)
    expected = sorted(SCOPES)
    if granted != expected:
        print("\nGranted scopes do not match the two this credential may hold:")
        print(f"  granted:  {granted}")
        print(f"  expected: {expected}")
        raise SystemExit(
            "refusing to write the env file. A third scope on the host "
            "credential is the thing ADR-015 rule 3 exists to prevent."
        )

    lines = [
        f"YOUTUBE_CLIENT_ID={client_id}",
        f"YOUTUBE_CLIENT_SECRET={client_secret}",
        f"YOUTUBE_REFRESH_TOKEN={refresh_token}",
        f"YOUTUBE_CHANNEL_ID={args.channel_id}",
        "",
    ]
    # Create at 0600 before writing, so the secret is never briefly world-readable.
    # newline="" keeps LF endings on Windows: the file is sourced by a Linux
    # shell, and a trailing CR rides into every value. The symptom is remote and
    # unhelpful -- Google answers `invalid_client: The OAuth client was not
    # found` for a client that exists and worked moments earlier on the laptop.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines))

    written = Path(args.out).read_bytes()
    if b"\r" in written:
        raise SystemExit(
            f"{args.out} contains a carriage return; the host would read it as part "
            "of a credential. Refusing to hand over a file that cannot work."
        )

    print(f"\nWrote {args.out} (0600). Granted scopes verified: {granted}")
    print("\nInstall it on the host, then delete the local copy:")
    print(f"  scp {args.out} ozand@eeepc-lan:/tmp/youtube.env")
    print(f"  ssh ozand@eeepc-lan 'sudo install -o root -g eeepc-agent -m 0640"
          f" /tmp/youtube.env {ENV_PATH} && rm -f /tmp/youtube.env'")
    print(f"  rm {args.out}")
    print("\nNever print the contents of that file, and never widen it past 0640.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
