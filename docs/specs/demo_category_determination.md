# Size Category Determination for eeebot Demo (#1619)

Live measurements performed on eeepc-lan host (Intel Atom N270 @ 1.6GHz, 945GSE GPU, 1GB RAM, Debian Bookworm i686).

## 1. Measured Cost Breakdown

| Component | Raw Bytes | Compressed (zlib -9) |
|---|---:|---:|
| 4-voice Tracker Audio Module | 5,564 | 734 |
| Font (8x8 ASCII 128 chars) | 1,024 | 177 |
| Unpacked Runner Code | 480 | 287 |
| Procedural Visual Generator | 262 | 175 |
| **Total Core Payload** | **7,330** | **1,373** |

## 2. Category Budget Remaining for Imagery (Payload)

| Category | Total Budget | Deducting Sound Only (734 B) | Deducting Full Core (1,373 B) |
|---|---:|---:|---:|
| **64K** (65,536 B) | 65,536 | 64,802 B | **64,163 B** |
| **32K** (32,768 B) | 32,768 | 32,034 B | **31,395 B** |
| **4K** (4,096 B) | 4,096 | 3,362 B | **2,723 B** |

## 3. Critical Unmeasured Parameters (Measured Live on eeepc-lan)

### A. libopenmpt Heap RAM Allocation
- Initial process heap before module: 466,600 bytes
- Module loaded into memory: 477,272 bytes (delta: +10,672 bytes / 10.4 KiB)
- Active mixing (44.1kHz stereo): 481,384 bytes (peak delta: +14,784 bytes / **14.4 KiB**)
- After module destroy: 475,000 bytes (clean reclamation, negligible fragmentation)

### B. CPU Load During Simultaneous Playback & Blitting (Atom N270 1.6GHz)
- Audio synthesis/mixing alone (44.1kHz stereo, 4 channels): **0.58% CPU** (5 seconds mixed in 0.029s)
- Direct framebuffer copy (1024x600x32bpp, 2.45 MB): 1.89 ms/frame (**5.7% CPU at 30 FPS**)
- Simultaneous Audio mixing + 32bpp framebuffer copy: **6.7% CPU at 30 FPS** (throughput: 448 FPS)
- Realistic Pipeline (Audio + 10% Dirty Tiles 8bpp->32bpp LUT expand + blit): **9.4% CPU at 30 FPS** (18.8% at 60 FPS)
- 100% Full-frame software LUT expansion per frame: **207.2% CPU** (CPU wall hit: max 14.5 FPS)

## 4. What Hits the Wall First: Bytes, CPU, or Memory?
- **Memory (RAM): NOT the bottleneck.** Total heap for audio mixing is only ~14.4 KiB; framebuffer is 2.4 MB on a 1 GB RAM machine.
- **Bytes (Storage): NOT the bottleneck.** 64K and 32K easily fit all assets. Even 4K fits compressed payload (1,373 B).
- **CPU: HITS THE WALL FIRST.** On the Atom N270 without hardware vertex/fragment shaders:
  - Full-screen software pixel manipulation at 32bpp costs >40 ms/frame (over 100% CPU of single core).
  - Therefore, tilemap composition and dirty tile bounding (ADR-014/ADR-018) are **mandatory** to stay under 10% CPU.

## 5. Category Conclusion
- **64K**: **Honest, robust, and fully achievable.** Allows uncompressed/procedural artwork, multiple audio patterns, and full tile assets within safe CPU limits.
- **32K**: **Achievable.** Requires compression on disk and procedural asset generation.
- **4K**: **Achievable ONLY as a compressed script payload** (1,373 bytes total); **unachievable as standalone ELF binary** on this host because minimal gcc ELF overhead (10,004 bytes) exceeds 4,096 bytes and UPX is unavailable.
