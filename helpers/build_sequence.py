"""Build intro/outro EDL sections snapped to music beats.

Takes a beat grid (from beats.py) + b-roll sources and produces an EDL
`intro` or `outro` block with one clip per beat segment. Title cards become
overlay specs for render.py.

Usage:
    python helpers/build_sequence.py --beats edit/beats_intro.json \\
        --sources broll_1 broll_2 broll_3 --type intro --music music/intro.mp3

    python helpers/build_sequence.py --beats edit/beats_outro.json \\
        --sources broll_4 broll_5 --type outro --music music/outro.mp3 \\
        --titles "SUBSCRIBE" "THANKS FOR WATCHING" -o edit/sequence_outro.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_sequence(
    beats: dict,
    source_names: list[str],
    source_paths: dict[str, str],
    seq_type: str,
    music_file: str,
    music_start: float = 0.0,
    music_volume: float = 1.0,
    titles: list[dict] | None = None,
    max_segments: int | None = None,
) -> dict:
    """Build an intro or outro block for the EDL."""
    segments = beats.get("segments", [])
    if max_segments:
        segments = segments[:max_segments]
    if not segments:
        sys.exit("no beat segments in beats JSON — run beats.py first")

    ranges: list[dict] = []
    for i, seg in enumerate(segments):
        src = source_names[i % len(source_names)]
        # Use full source clip, trimmed to beat duration at head
        ranges.append({
            "source": src,
            "start": 0.0,
            "end": seg["duration"],
            "type": seq_type,
            "beat_index": seg["beat_index"],
            "beat": f"{seq_type.upper()}_{i + 1}",
            "reason": f"Beat {seg['beat_index']+1} — {seg['duration']:.2f}s montage cut",
        })

    total_dur = sum(s["duration"] for s in segments)
    beat_times = beats.get("beats", [])

    title_specs: list[dict] = []
    if titles:
        # Distribute titles across beats
        for i, t in enumerate(titles):
            if isinstance(t, str):
                t = {"text": t}
            # Place on beat i (or spread evenly)
            beat_idx = min(i, len(beat_times) - 1)
            at = beat_times[beat_idx] if beat_idx < len(beat_times) else 0.0
            title_specs.append({
                "text": t["text"],
                "subtitle": t.get("subtitle"),
                "at": at,
                "duration": t.get("duration", 1.5),
            })

    block = {
        "music": {
            "file": music_file,
            "start": music_start,
            "volume": music_volume,
            "replace": True,
        },
        "beats_file": beats.get("source"),
        "bpm": beats.get("bpm"),
        "duration": round(total_dur, 3),
        "ranges": ranges,
        "titles": title_specs,
        "sources_needed": {name: source_paths[name] for name in source_names if name in source_paths},
    }
    return block


def main() -> None:
    ap = argparse.ArgumentParser(description="Build beat-synced intro/outro EDL block")
    ap.add_argument("--beats", type=Path, required=True, help="beats.json from beats.py")
    ap.add_argument("--sources", nargs="+", required=True, help="B-roll source names (cycle across beats)")
    ap.add_argument("--source-dir", type=Path, default=None, help="Videos dir to resolve source paths")
    ap.add_argument("--type", choices=["intro", "outro"], default="intro")
    ap.add_argument("--music", type=str, required=True, help="Music file path for this sequence")
    ap.add_argument("--music-start", type=float, default=0.0)
    ap.add_argument("--volume", type=float, default=1.0)
    ap.add_argument("--max-segments", type=int, default=None, help="Cap number of beat cuts")
    ap.add_argument("--titles", nargs="*", default=[], help="Title texts (one per early beat)")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Write JSON block")
    args = ap.parse_args()

    beats = json.loads(args.beats.read_text())
    source_dir = (args.source_dir or args.beats.parent.parent).resolve()

    source_paths: dict[str, str] = {}
    for name in args.sources:
        for ext in (".mp4", ".mov", ".MP4", ".MOV", ".mkv"):
            p = source_dir / f"{name}{ext}"
            if p.exists():
                source_paths[name] = str(p.resolve())
                break
            p = source_dir / "broll" / f"{name}{ext}"
            if p.exists():
                source_paths[name] = str(p.resolve())
                break
        if name not in source_paths:
            print(f"warning: could not find source file for {name}")

    titles = [{"text": t} for t in args.titles]
    block = build_sequence(
        beats=beats,
        source_names=args.sources,
        source_paths=source_paths,
        seq_type=args.type,
        music_file=args.music,
        music_start=args.music_start,
        music_volume=args.volume,
        titles=titles or None,
        max_segments=args.max_segments,
    )

    payload = json.dumps({args.type: block}, indent=2)
    if args.output:
        args.output.write_text(payload)
        print(f"{args.type} block → {args.output}  ({block['duration']:.1f}s, {len(block['ranges'])} cuts)")
    else:
        print(payload)


if __name__ == "__main__":
    main()
