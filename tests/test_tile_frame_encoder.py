from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.tile_frame_encoder import TileArtifactError, decode_artifact, encode_frames


def _fixture(tmp_path: Path, count: int = 300) -> tuple[Path, list[bytes]]:
    source = tmp_path / "frames"
    source.mkdir()
    width, height = 128, 75
    frames: list[bytes] = []
    current = bytearray(width * height)
    for frame_index in range(count):
        for offset in range(16 + (frame_index % 4) * 16):
            cell = (frame_index * 997 + offset * 37) % len(current)
            current[cell] = (current[cell] + 1) % 64
        frame = bytes(current)
        frames.append(frame)
        (source / f"frame-{frame_index:04d}.bin").write_bytes(frame)
    (source / "manifest.json").write_text(json.dumps({
        "width": width, "height": height, "tile_size": 8,
        "fps": 5, "palette_entries": 64, "chunk_frames": 60,
    }), encoding="utf-8")
    return source, frames


def test_round_trip_preserves_synthetic_tile_sequence(tmp_path: Path):
    source, frames = _fixture(tmp_path)
    output = tmp_path / "surface.eebtile"
    metadata = encode_frames(source, output)

    decoded_metadata, decoded = decode_artifact(output)

    assert metadata["compression"] == "zlib-1"
    assert metadata["frame_count"] == 300
    assert metadata["chunk_count"] == 5
    assert decoded_metadata["format_version"] == 1
    assert decoded == frames
    assert output.stat().st_size == metadata["output_bytes"]
    assert metadata["output_bytes"] < sum(len(frame) for frame in frames)


def test_input_frames_are_sorted_and_size_is_enforced(tmp_path: Path):
    source, _ = _fixture(tmp_path, count=2)
    (source / "frame-9999.bin").write_bytes(b"bad")
    with pytest.raises(TileArtifactError, match="must contain"):
        encode_frames(source, tmp_path / "out.eebtile")


def test_unknown_artifact_version_fails_loudly(tmp_path: Path):
    source, _ = _fixture(tmp_path, count=1)
    output = tmp_path / "surface.eebtile"
    encode_frames(source, output)
    data = bytearray(output.read_bytes())
    header_len = int.from_bytes(data[8:12], "little")
    header_start = 12
    header = json.loads(bytes(data[header_start:header_start + header_len]))
    header["format_version"] = 9
    replacement = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    assert len(replacement) == header_len
    data[header_start:header_start + header_len] = replacement
    output.write_bytes(data)
    with pytest.raises(TileArtifactError, match="unsupported artifact format"):
        decode_artifact(output)
