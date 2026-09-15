"""Tests for demo byte budget probe per #1619."""
from __future__ import annotations

import json
from pathlib import Path
from scripts.probe_demo_budget import (
    measure_audio_mod,
    measure_font,
    measure_generator,
    measure_unpacked_code,
)


def test_demo_budget_components_measured_and_fit_64k() -> None:
    audio = measure_audio_mod()
    font = measure_font()
    code = measure_unpacked_code()
    gen = measure_generator()

    assert audio["raw_bytes"] > 0 and audio["zlib_bytes"] > 0
    assert font["raw_bytes"] == 1024 and font["zlib_bytes"] > 0
    assert code["raw_bytes"] > 0 and code["zlib_bytes"] > 0
    assert gen["raw_bytes"] > 0 and gen["zlib_bytes"] > 0

    total_compressed = (
        audio["zlib_bytes"]
        + font["zlib_bytes"]
        + code["zlib_bytes"]
        + gen["zlib_bytes"]
    )
    # Total payload must fit comfortably in 64K budget with ample room for image
    assert total_compressed < 4096
    remaining_64k = 65536 - total_compressed
    assert remaining_64k > 60000
