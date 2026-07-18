"""Scan a videos directory and classify assets for editing.

Distinguishes main takes, meme/reaction clips, music beds, and B-roll so
transcription and the editor brief can treat each type appropriately.

Convention (flexible — any one signal is enough):
  - `memes/` or `reactions/` subfolder → meme clips
  - `music/` or `audio/` subfolder → music beds
  - Filename prefix `meme_`, `react_`, `sfx_` → meme/reaction
  - Audio-only files (.mp3, .wav, .m4a, .aac) in music/ or named `music_*` → music
  - Everything else at the root → main takes

Writes `edit/inventory.json` for the session.

Usage:
    python helpers/inventory.py <videos_dir>
    python helpers/inventory.py <videos_dir> --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".MP4", ".MOV", ".MKV"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".MP3", ".WAV", ".M4A"}
MEME_PREFIXES = ("meme_", "react_", "reaction_", "sfx_", "vine_", "clip_")
MUSIC_PREFIXES = ("music_", "bgm_", "beat_", "track_")
MEME_DIRS = {"memes", "reactions", "meme", "reaction", "sfx"}
MUSIC_DIRS = {"music", "audio", "sounds", "bgm"}


def probe_video(path: Path) -> dict:
    """Return duration, resolution, fps, has_audio via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {"duration": None, "width": None, "height": None, "fps": None, "has_audio": False}

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    duration = None
    if fmt.get("duration"):
        duration = float(fmt["duration"])
    elif streams and streams[0].get("duration"):
        duration = float(streams[0]["duration"])

    width = height = fps = None
    if streams:
        width = streams[0].get("width")
        height = streams[0].get("height")
        rfr = streams[0].get("r_frame_rate", "0/1")
        if "/" in rfr:
            num, den = rfr.split("/")
            if float(den) > 0:
                fps = round(float(num) / float(den), 2)

    # Check audio stream
    audio_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
    ]
    try:
        has_audio = bool(subprocess.check_output(audio_cmd, stderr=subprocess.DEVNULL).strip())
    except subprocess.CalledProcessError:
        has_audio = False

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": has_audio,
    }


def classify_file(path: Path, videos_dir: Path) -> str:
    """Return asset type: take | meme | music | broll."""
    rel = path.relative_to(videos_dir)
    parts = [p.lower() for p in rel.parts[:-1]]
    name_lower = path.stem.lower()

    if path.suffix in AUDIO_EXTS:
        if any(d in MUSIC_DIRS for d in parts) or name_lower.startswith(MUSIC_PREFIXES):
            return "music"
        return "music"  # audio-only at root still counts as music candidate

    if any(d in MEME_DIRS for d in parts):
        return "meme"
    if any(d in MUSIC_DIRS for d in parts):
        return "music"
    if name_lower.startswith(MEME_PREFIXES):
        return "meme"
    if name_lower.startswith(MUSIC_PREFIXES):
        return "music"
    if "broll" in parts or name_lower.startswith("broll_"):
        return "broll"

    return "take"


def scan_directory(videos_dir: Path) -> dict:
    """Walk videos_dir and build inventory."""
    videos_dir = videos_dir.resolve()
    assets: list[dict] = []

    for path in sorted(videos_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(videos_dir).parts
        # Skip edit output, hidden dirs, and dependency folders
        if any(
            p.startswith(".") or p in ("edit", "node_modules", ".venv", "venv", "__pycache__")
            for p in rel_parts[:-1]
        ):
            continue
        if "edit" in rel_parts:
            continue
        if path.suffix not in VIDEO_EXTS and path.suffix not in AUDIO_EXTS:
            continue

        asset_type = classify_file(path, videos_dir)
        entry: dict = {
            "name": path.stem,
            "path": str(path.resolve()),
            "type": asset_type,
            "ext": path.suffix,
        }

        if path.suffix in VIDEO_EXTS:
            meta = probe_video(path)
            entry.update(meta)
            entry["portrait"] = (
                meta.get("height") and meta.get("width")
                and meta["height"] > meta["width"]
            )
        else:
            # Audio file — probe duration only
            try:
                dur_out = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                    stderr=subprocess.DEVNULL,
                )
                entry["duration"] = float(dur_out.decode().strip())
            except Exception:
                entry["duration"] = None
            entry["has_audio"] = True

        assets.append(entry)

    by_type: dict[str, list] = {t: [] for t in ("take", "meme", "music", "broll")}
    for a in assets:
        by_type.setdefault(a["type"], []).append(a["name"])

    return {
        "videos_dir": str(videos_dir),
        "assets": assets,
        "summary": {
            "takes": len(by_type.get("take", [])),
            "memes": len(by_type.get("meme", [])),
            "music": len(by_type.get("music", [])),
            "broll": len(by_type.get("broll", [])),
            "total": len(assets),
        },
        "transcribe": [a["name"] for a in assets if a["type"] in ("take", "broll")],
        "meme_pool": [a["name"] for a in assets if a["type"] == "meme"],
        "music_pool": [a["name"] for a in assets if a["type"] == "music"],
    }


def render_markdown(inv: dict) -> str:
    lines = [
        "# Asset inventory",
        "",
        f"**Takes:** {inv['summary']['takes']}  |  "
        f"**Memes:** {inv['summary']['memes']}  |  "
        f"**Music:** {inv['summary']['music']}  |  "
        f"**B-roll:** {inv['summary']['broll']}",
        "",
    ]

    for asset_type, label in [
        ("take", "Main takes (transcribe)"),
        ("meme", "Memes / reactions (insert on beat)"),
        ("music", "Music beds (mix under dialogue)"),
        ("broll", "B-roll (optional transcript)"),
    ]:
        items = [a for a in inv["assets"] if a["type"] == asset_type]
        if not items:
            continue
        lines.append(f"## {label}")
        for a in items:
            dur = a.get("duration")
            dur_s = f"{dur:.1f}s" if dur else "?"
            if a["ext"] in AUDIO_EXTS:
                lines.append(f"- **{a['name']}** ({dur_s}) — audio")
            else:
                w, h = a.get("width"), a.get("height")
                res = f"{w}×{h}" if w and h else "?"
                lines.append(f"- **{a['name']}** ({dur_s}, {res})")
        lines.append("")

    if inv["meme_pool"]:
        lines.append("## Meme insert tips")
        lines.append("- Drop reaction clips in `memes/` or prefix with `meme_`")
        lines.append("- Insert as EDL ranges at punchlines, fails, or `(laughs)` beats")
        lines.append("- Set `\"type\": \"meme\"` and `\"keep_audio\": true` for sound memes")
        lines.append("")

    if inv["music_pool"]:
        lines.append("## Music")
        lines.append("- Reference in EDL `music` block; render.py ducks under speech")
        lines.append("- Drag tracks into `music/` folder or root as `.mp3`/`.wav`")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify video folder assets")
    ap.add_argument("videos_dir", type=Path, help="Directory containing source footage")
    ap.add_argument("--json", action="store_true", help="Print JSON to stdout only")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit directory (default: <videos_dir>/edit)",
    )
    args = ap.parse_args()

    videos_dir = args.videos_dir.resolve()
    if not videos_dir.is_dir():
        sys.exit(f"not a directory: {videos_dir}")

    inv = scan_directory(videos_dir)
    edit_dir = (args.edit_dir or (videos_dir / "edit")).resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)

    json_path = edit_dir / "inventory.json"
    json_path.write_text(json.dumps(inv, indent=2))
    md_path = edit_dir / "inventory.md"
    md_path.write_text(render_markdown(inv))

    if args.json:
        print(json.dumps(inv, indent=2))
        return

    print(f"inventory → {json_path.name}, {md_path.name}")
    s = inv["summary"]
    print(f"  {s['takes']} takes, {s['memes']} memes, {s['music']} music, {s['broll']} broll")


if __name__ == "__main__":
    main()
