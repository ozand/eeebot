"""The publish guard must reject exactly the encode that uploaded and never played.

On 2026-09-15 a 2,517 B h264-only clip was accepted by the API as `public` and
shown as Public in Studio while never becoming watchable. A 76,536 B clip with a
silent AAC track and `+faststart` processed normally. These tests pin that
difference so the guard cannot quietly stop catching it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "host" / "eeepc" / "scripts" / "youtube_publish.py"
_spec = importlib.util.spec_from_file_location("youtube_publish", _SCRIPT)
assert _spec and _spec.loader
youtube_publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(youtube_publish)


def _probe(*, video: dict | None = None, audio: bool = True) -> dict:
    streams = []
    if video is not None:
        streams.append({"codec_type": "video", **video})
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams}


_GOOD_VIDEO = {"codec_name": "h264", "pix_fmt": "yuv420p"}


def test_the_working_encode_is_accepted():
    assert youtube_publish.check_streams(_probe(video=_GOOD_VIDEO)) == []


def test_the_clip_that_uploaded_and_never_played_is_refused():
    """h264 video, no audio track — the exact shape that was reported public."""
    reasons = youtube_publish.check_streams(_probe(video=_GOOD_VIDEO, audio=False))
    assert reasons == ["no audio stream; add a silent AAC track"]


@pytest.mark.parametrize(
    ("video", "fragment"),
    [
        ({"codec_name": "vp9", "pix_fmt": "yuv420p"}, "video codec"),
        ({"codec_name": "h264", "pix_fmt": "yuv444p"}, "pixel format"),
    ],
)
def test_wrong_codec_or_pixel_format_is_named(video, fragment):
    reasons = youtube_publish.check_streams(_probe(video=video))
    assert len(reasons) == 1 and fragment in reasons[0]


def test_missing_video_stream_is_refused():
    assert youtube_publish.check_streams(_probe(video=None)) == ["no video stream"]


def test_every_reason_is_reported_not_just_the_first():
    """A file can be wrong in several ways; one re-encode should fix all of them."""
    reasons = youtube_publish.check_streams(
        _probe(video={"codec_name": "mpeg4", "pix_fmt": "yuv444p"}, audio=False)
    )
    assert len(reasons) == 3


def test_faststart_ordering():
    assert youtube_publish.moov_precedes_mdat(b"\x00\x00\x00 ftypisom....moov....mdat")
    assert not youtube_publish.moov_precedes_mdat(b"\x00\x00\x00 ftypisom....mdat....moov")
    assert not youtube_publish.moov_precedes_mdat(b"\x00\x00\x00 ftypisom....mdat")


def test_env_file_is_read_and_overridden_by_the_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "youtube.env"
    env_file.write_text(
        "# comment\n"
        "YOUTUBE_CLIENT_ID=from-file\n"
        "YOUTUBE_CLIENT_SECRET=secret\n"
        "YOUTUBE_REFRESH_TOKEN=token\n"
        "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    loaded = youtube_publish.load_env(env_file)
    assert loaded["YOUTUBE_CLIENT_ID"] == "from-file"

    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "from-env")
    assert youtube_publish.load_env(env_file)["YOUTUBE_CLIENT_ID"] == "from-env"


def test_missing_credentials_name_the_file(tmp_path, monkeypatch):
    for key in youtube_publish.REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    missing = tmp_path / "absent.env"
    with pytest.raises(SystemExit) as excinfo:
        youtube_publish.load_env(missing)
    assert str(missing) in str(excinfo.value)


def test_check_only_refuses_without_touching_the_network(tmp_path, monkeypatch):
    """The guard runs before credentials are loaded, so a bad file costs nothing."""
    clip = tmp_path / "bad.mp4"
    clip.write_bytes(b"\x00\x00\x00 ftypisom....moov....mdat" + b"\x00" * 64)
    monkeypatch.setattr(
        youtube_publish, "probe_media", lambda path: _probe(video=_GOOD_VIDEO, audio=False)
    )

    def _no_network(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("credentials were loaded for a file that fails the check")

    monkeypatch.setattr(youtube_publish, "load_env", _no_network)
    description = tmp_path / "d.txt"
    description.write_text("d", encoding="utf-8")

    code = youtube_publish.main([
        "upload", str(clip), "--title", "t", "--description-file", str(description),
    ])
    assert code == 1
