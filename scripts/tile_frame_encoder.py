#!/usr/bin/env python3
"""Encode deterministic tile-index frame sequences into a compact artifact.

Input contract (encoder-owned seam): a directory containing ``*.bin`` files,
ordered by filename, each containing exactly ``width * height`` tile indices.
Optional ``manifest.json`` may declare ``width``, ``height``, ``tile_size``,
``fps`` and ``palette_entries``; defaults describe the instance surface.

The artifact is a versioned, independently compressed chunk stream. Each chunk
starts with a full keyframe and then carries sparse ``uint16 cell + uint8
value`` deltas. Chunks are zlib level 1 so a partial artifact remains bounded
and cheap to inspect/decode without competing with the bridge.
"""
from __future__ import annotations

import argparse
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Iterable

MAGIC = b"EEBTILE1"
FORMAT_VERSION = 1
DEFAULT_WIDTH = 128
DEFAULT_HEIGHT = 75
DEFAULT_TILE_SIZE = 8
DEFAULT_FPS = 5
DEFAULT_PALETTE_ENTRIES = 16
DEFAULT_CHUNK_FRAMES = 60
MAX_HEADER_BYTES = 64 * 1024
FRAME_RECORD = struct.Struct("<BII")  # kind, frame index, payload length
CHUNK_HEADER = struct.Struct("<II")  # first frame index, compressed length
DELTA_COUNT = struct.Struct("<H")
DELTA_ITEM = struct.Struct("<HB")


class TileArtifactError(ValueError):
    """Invalid input or artifact data."""


def _read_manifest(input_dir: Path) -> dict[str, Any]:
    path = input_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TileArtifactError(f"invalid manifest.json: {exc}") from exc
    if not isinstance(value, dict):
        raise TileArtifactError("manifest.json must contain an object")
    return value


def _positive_int(manifest: dict[str, Any], key: str, default: int) -> int:
    value = manifest.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TileArtifactError(f"{key} must be a positive integer")
    return value


def _frame_paths(input_dir: Path) -> list[Path]:
    paths = sorted(path for path in input_dir.glob("*.bin") if path.is_file())
    if not paths:
        raise TileArtifactError(f"no .bin frames found in {input_dir}")
    return paths


def read_frames(input_dir: Path) -> tuple[dict[str, Any], list[bytes]]:
    """Read and validate the encoder input contract."""
    if not input_dir.is_dir():
        raise TileArtifactError(f"input directory not found: {input_dir}")
    manifest = _read_manifest(input_dir)
    width = _positive_int(manifest, "width", DEFAULT_WIDTH)
    height = _positive_int(manifest, "height", DEFAULT_HEIGHT)
    tile_size = _positive_int(manifest, "tile_size", DEFAULT_TILE_SIZE)
    fps = _positive_int(manifest, "fps", DEFAULT_FPS)
    palette_entries = _positive_int(manifest, "palette_entries", DEFAULT_PALETTE_ENTRIES)
    frame_bytes = width * height
    frames: list[bytes] = []
    for path in _frame_paths(input_dir):
        data = path.read_bytes()
        if len(data) != frame_bytes:
            raise TileArtifactError(
                f"{path.name} must contain {frame_bytes} bytes, got {len(data)}"
            )
        if any(value >= palette_entries for value in data):
            raise TileArtifactError(f"{path.name} contains an index outside palette_entries")
        frames.append(data)
    metadata = {
        "format_version": FORMAT_VERSION,
        "width": width,
        "height": height,
        "tile_size": tile_size,
        "fps": fps,
        "palette_entries": palette_entries,
        "frame_count": len(frames),
        "frame_bytes": frame_bytes,
        "chunk_frames": _positive_int(manifest, "chunk_frames", DEFAULT_CHUNK_FRAMES),
        "compression": "zlib-1",
        "frame_names": [path.name for path in _frame_paths(input_dir)],
    }
    return metadata, frames


def _delta(previous: bytes, current: bytes) -> bytes:
    changes = [(index, value) for index, (old, value) in enumerate(zip(previous, current)) if old != value]
    if len(changes) > 0xFFFF:
        raise TileArtifactError("a frame has more than 65535 changed cells")
    return DELTA_COUNT.pack(len(changes)) + b"".join(
        DELTA_ITEM.pack(index, value) for index, value in changes
    )


def _chunk_payload(frames: list[bytes], start: int) -> bytes:
    payload = bytearray()
    previous: bytes | None = None
    for offset, frame in enumerate(frames):
        index = start + offset
        keyframe = previous is None
        encoded = frame if keyframe else _delta(previous, frame)
        payload.extend(FRAME_RECORD.pack(0 if keyframe else 1, index, len(encoded)))
        payload.extend(encoded)
        previous = frame
    return bytes(payload)


def encode_frames(input_dir: Path, output_path: Path) -> dict[str, Any]:
    """Encode the input directory and return deterministic artifact metadata."""
    metadata, frames = read_frames(input_dir)
    chunks: list[tuple[int, bytes]] = []
    chunk_frames = metadata["chunk_frames"]
    for start in range(0, len(frames), chunk_frames):
        raw = _chunk_payload(frames[start:start + chunk_frames], start)
        chunks.append((start, zlib.compress(raw, level=1)))
    metadata = {**metadata, "chunk_count": len(chunks)}
    header = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(header) > MAX_HEADER_BYTES:
        raise TileArtifactError("artifact header exceeds maximum size")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as stream:
        stream.write(MAGIC)
        stream.write(struct.pack("<I", len(header)))
        stream.write(header)
        for start, compressed in chunks:
            stream.write(CHUNK_HEADER.pack(start, len(compressed)))
            stream.write(compressed)
    return {**metadata, "output_bytes": output_path.stat().st_size}


def _apply_delta(previous: bytearray, encoded: bytes) -> bytearray:
    if len(encoded) < DELTA_COUNT.size:
        raise TileArtifactError("truncated delta record")
    count = DELTA_COUNT.unpack_from(encoded)[0]
    expected = DELTA_COUNT.size + count * DELTA_ITEM.size
    if len(encoded) != expected:
        raise TileArtifactError("delta record length mismatch")
    result = bytearray(previous)
    offset = DELTA_COUNT.size
    for _ in range(count):
        index, value = DELTA_ITEM.unpack_from(encoded, offset)
        if index >= len(result):
            raise TileArtifactError("delta cell index out of range")
        result[index] = value
        offset += DELTA_ITEM.size
    return result


def decode_artifact(artifact_path: Path) -> tuple[dict[str, Any], list[bytes]]:
    """Decode an artifact for inspection and round-trip tests."""
    with artifact_path.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise TileArtifactError("invalid artifact magic")
        header_len_raw = stream.read(4)
        if len(header_len_raw) != 4:
            raise TileArtifactError("truncated artifact header length")
        header_len = struct.unpack("<I", header_len_raw)[0]
        if header_len > MAX_HEADER_BYTES:
            raise TileArtifactError("artifact header too large")
        try:
            metadata = json.loads(stream.read(header_len))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TileArtifactError("invalid artifact header") from exc
        if metadata.get("format_version") != FORMAT_VERSION:
            raise TileArtifactError("unsupported artifact format version")
        frames: list[bytes] = []
        for _ in range(metadata["chunk_count"]):
            chunk_header = stream.read(CHUNK_HEADER.size)
            if len(chunk_header) != CHUNK_HEADER.size:
                raise TileArtifactError("truncated chunk header")
            _start, compressed_len = CHUNK_HEADER.unpack(chunk_header)
            compressed = stream.read(compressed_len)
            if len(compressed) != compressed_len:
                raise TileArtifactError("truncated chunk")
            try:
                raw = zlib.decompress(compressed)
            except zlib.error as exc:
                raise TileArtifactError("invalid compressed chunk") from exc
            offset = 0
            previous: bytes | None = None
            while offset < len(raw):
                if offset + FRAME_RECORD.size > len(raw):
                    raise TileArtifactError("truncated frame record")
                kind, _index, payload_len = FRAME_RECORD.unpack_from(raw, offset)
                offset += FRAME_RECORD.size
                payload = raw[offset:offset + payload_len]
                if len(payload) != payload_len:
                    raise TileArtifactError("truncated frame payload")
                offset += payload_len
                if kind == 0:
                    frame = payload
                elif kind == 1 and previous is not None:
                    frame = bytes(_apply_delta(bytearray(previous), payload))
                else:
                    raise TileArtifactError("invalid frame record sequence")
                if len(frame) != metadata["frame_bytes"]:
                    raise TileArtifactError("decoded frame size mismatch")
                frames.append(frame)
                previous = frame
    if len(frames) != metadata["frame_count"]:
        raise TileArtifactError("decoded frame count mismatch")
    return metadata, frames


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        metadata = encode_frames(args.input_dir, args.output)
    except (OSError, TileArtifactError) as exc:
        parser.error(str(exc))
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
