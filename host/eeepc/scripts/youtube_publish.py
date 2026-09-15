#!/usr/bin/env python3
"""Publish a video to the channel, refusing files YouTube will silently drop (#1612).

Two things this script exists to prevent, both observed on 2026-09-15 during the
first live upload from the host:

1. A clip encoded h264-only, with no audio track, was accepted by the API with
   `privacyStatus: public` and shown as *Public* in Studio. It never became
   watchable. So the encode is checked before the bytes are sent, and a file
   that fails the check is refused rather than uploaded.

2. Two observers reported success over that same video. `privacyStatus` says
   nothing about whether processing finished, so success here means
   `processingStatus: succeeded` read back from the API after the upload, never
   the upload response alone.

Credentials come from /etc/eeepc-agent/youtube.env (root:eeepc-agent 0640) or
from the environment. Nothing is written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

ENV_FILE = Path("/etc/eeepc-agent/youtube.env")
REQUIRED_ENV = (
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
)

# Measured on this channel, not chosen: the failing clip differed from the
# working one in exactly these three properties.
REQUIRED_VIDEO_CODEC = "h264"
REQUIRED_PIX_FMT = "yuv420p"


class MediaRejected(Exception):
    """The file would upload and then never become watchable."""


def load_env(env_file: Path = ENV_FILE) -> dict[str, str]:
    """Read credentials, preferring the process environment over the file."""
    values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith("YOUTUBE_")})
    missing = [key for key in REQUIRED_ENV if not values.get(key)]
    if missing:
        raise SystemExit(f"missing credentials: {', '.join(missing)} (looked in {env_file})")
    return values


def moov_precedes_mdat(head: bytes) -> bool:
    """Is the index at the front of the file (`-movflags +faststart`)?

    Without it the player must fetch the tail before it can start, which is the
    difference between a clip that plays and one that appears to hang.
    """
    moov = head.find(b"moov")
    mdat = head.find(b"mdat")
    if moov == -1:
        return False
    return mdat == -1 or moov < mdat


def check_streams(probe: dict) -> list[str]:
    """Return the reasons this encode would be dropped, empty if it is sound."""
    streams = probe.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    reasons: list[str] = []

    if not video:
        reasons.append("no video stream")
    else:
        codec = video[0].get("codec_name")
        if codec != REQUIRED_VIDEO_CODEC:
            reasons.append(f"video codec is {codec!r}, needs {REQUIRED_VIDEO_CODEC!r}")
        pix_fmt = video[0].get("pix_fmt")
        if pix_fmt != REQUIRED_PIX_FMT:
            reasons.append(f"pixel format is {pix_fmt!r}, needs {REQUIRED_PIX_FMT!r}")

    if not audio:
        # A silent AAC track is enough. The point is that the track exists.
        reasons.append("no audio stream; add a silent AAC track")

    return reasons


def probe_media(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise MediaRejected(f"ffprobe failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "{}")


def assert_publishable(path: Path) -> None:
    reasons = check_streams(probe_media(path))
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
    if not moov_precedes_mdat(head):
        reasons.append("moov atom follows mdat; re-encode with -movflags +faststart")
    if reasons:
        raise MediaRejected("; ".join(reasons))


def access_token(env: dict[str, str]) -> str:
    body = urllib.parse.urlencode({
        "client_id": env["YOUTUBE_CLIENT_ID"],
        "client_secret": env["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": env["YOUTUBE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def _post_json(url: str, token: str, payload: dict, extra: dict[str, str]) -> dict:
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json; charset=UTF-8",
    }
    headers.update(extra)
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return {"headers": dict(response.headers), "body": response.read()}


def upload(path: Path, title: str, description: str, privacy: str, token: str) -> str:
    metadata = {
        "snippet": {"title": title, "description": description, "categoryId": "28"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    size = path.stat().st_size
    try:
        started = _post_json(UPLOAD_URL, token, metadata, {
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        })
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"upload init failed {exc.code}: {exc.read().decode()[:400]}")

    location = started["headers"].get("Location")
    if not location:
        raise SystemExit("no resumable Location header in the init response")

    data = path.read_bytes()
    request = urllib.request.Request(location, data=data, method="PUT", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "video/mp4",
        "Content-Length": str(len(data)),
    })
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"upload failed {exc.code}: {exc.read().decode()[:400]}")
    return result["id"]


def fetch_status(video_id: str, token: str) -> dict:
    url = f"{VIDEOS_URL}?part=status,processingDetails&id={urllib.parse.quote(video_id)}"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    items = payload.get("items") or []
    return items[0] if items else {}


def verify(video_id: str, token: str, timeout_s: int, poll_s: int = 15) -> int:
    """Wait for processing to actually finish. privacyStatus is not the answer."""
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        last = fetch_status(video_id, token)
        status = last.get("status") or {}
        processing = (last.get("processingDetails") or {}).get("processingStatus")
        upload_status = status.get("uploadStatus")
        print(f"uploadStatus={upload_status} processingStatus={processing}")
        if status.get("rejectionReason"):
            print(f"REJECTED: {status['rejectionReason']}")
            return 1
        if upload_status == "processed" and processing == "succeeded":
            print(f"watchable: https://www.youtube.com/watch?v={video_id}")
            return 0
        if upload_status == "failed":
            print(f"FAILED: {status.get('failureReason')}")
            return 1
        time.sleep(poll_s)
    print(f"still not processed after {timeout_s}s; last seen: {json.dumps(last)[:400]}")
    return 2


def set_thumbnail(video_id: str, path: Path, token: str) -> None:
    url = f"{THUMBNAIL_URL}?videoId={urllib.parse.quote(video_id)}&uploadType=media"
    data = path.read_bytes()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        "Content-Length": str(len(data)),
    })
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            json.load(response)
        print("thumbnail set")
    except urllib.error.HTTPError as exc:
        print(f"thumbnail failed {exc.code}: {exc.read().decode()[:300]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="check, upload, then wait for processing")
    up.add_argument("video", type=Path)
    up.add_argument("--title", required=True)
    up.add_argument("--description-file", type=Path, required=True)
    up.add_argument("--privacy", default="unlisted", choices=["private", "unlisted", "public"])
    up.add_argument("--thumbnail", type=Path)
    up.add_argument("--verify-timeout", type=int, default=900)
    up.add_argument("--check-only", action="store_true", help="probe the file and stop")

    ver = sub.add_parser("verify", help="read processing status for an existing video")
    ver.add_argument("video_id")
    ver.add_argument("--verify-timeout", type=int, default=900)

    args = parser.parse_args(argv)

    if args.command == "upload":
        try:
            assert_publishable(args.video)
        except MediaRejected as exc:
            print(f"refusing to upload {args.video}: {exc}")
            return 1
        print(f"encode ok: {args.video} ({args.video.stat().st_size} B)")
        if args.check_only:
            return 0
        env = load_env()
        token = access_token(env)
        video_id = upload(
            args.video, args.title,
            args.description_file.read_text(encoding="utf-8"),
            args.privacy, token,
        )
        print(f"video id: {video_id}")
        if args.thumbnail:
            set_thumbnail(video_id, args.thumbnail, token)
        return verify(video_id, token, args.verify_timeout)

    env = load_env()
    return verify(args.video_id, access_token(env), args.verify_timeout)


if __name__ == "__main__":
    sys.exit(main())
