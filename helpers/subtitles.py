"""Smart subtitle generation for video-use.

Builds output-timeline SRT from per-source Scribe transcripts + EDL ranges.
Replaces the naive fixed 2-word UPPERCASE chunker with style-aware grouping
that respects pauses, punctuation, emphasis, and platform safe zones.

Styles (set via EDL `subtitle_style` or --subtitle-style):
  bold-overlay   — short-form social: 2-3 word chunks, UPPERCASE, tight timing
  punch-word     — highlight the most important word per phrase (larger via ASS tags)
  natural        — documentary/education: 4-8 words, sentence case, readable pauses
  comedy         — reaction-friendly: shorter chunks on laughs, emphasis on punchlines

Usage:
    python helpers/subtitles.py --edl edit/edl.json -o edit/master.srt
    python helpers/subtitles.py --edl edit/edl.json --style natural -o edit/master.srt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# libass force_style strings — MarginV is platform safe-zone, not taste.
# Vertical video UI covers ~25-30% of frame bottom; MarginV=90 clears it.
SUB_STYLES: dict[str, str] = {
    "bold-overlay": (
        "FontName=Helvetica,FontSize=18,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,"
        "Alignment=2,MarginV=90"
    ),
    "punch-word": (
        "FontName=Helvetica,FontSize=17,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=1,"
        "Alignment=2,MarginV=90"
    ),
    "natural": (
        "FontName=Helvetica,FontSize=20,Bold=0,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,"
        "BorderStyle=3,Outline=1,Shadow=0,"
        "Alignment=2,MarginV=70"
    ),
    "comedy": (
        "FontName=Helvetica,FontSize=19,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,"
        "Alignment=2,MarginV=85"
    ),
}

PUNCT_BREAK = set(".,!?;:")
FILLER_WORDS = {
    "um", "uh", "uhh", "umm", "like", "you know", "i mean", "sort of", "kind of",
}
EMPHASIS_MARKERS = re.compile(r"\b(never|always|literally|actually|insane|crazy|wild|best|worst|huge|massive)\b", re.I)
LAUGH_EVENT = re.compile(r"\(laugh", re.I)

# Minimum/maximum cue duration (seconds)
MIN_CUE = 0.35
MAX_CUE = 4.5
# Gap between words that suggests a natural phrase break (seconds)
PHRASE_GAP = 0.28
# Longer gap → stronger break
STRONG_GAP = 0.55


def get_force_style(style: str) -> str:
    if style not in SUB_STYLES:
        raise KeyError(f"unknown subtitle style '{style}'. Available: {', '.join(SUB_STYLES)}")
    return SUB_STYLES[style]


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") not in ("word", "audio_event"):
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def _clean_word(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _is_filler(text: str) -> bool:
    return _clean_word(text).lower().rstrip(".,!?") in FILLER_WORDS


def _gap_before(words: list[dict], idx: int) -> float:
    if idx <= 0:
        return 0.0
    prev_end = words[idx - 1].get("end", words[idx - 1].get("start", 0.0))
    cur_start = words[idx].get("start", prev_end)
    return max(0.0, cur_start - prev_end)


def _score_emphasis(word: str) -> int:
    w = word.lower().rstrip(".,!?;:")
    if w in {"not", "never", "always", "literally", "actually", "insane", "crazy", "wild"}:
        return 3
    if w in {"best", "worst", "huge", "massive", "insane", "wild", "crazy"}:
        return 2
    if word.isupper() and len(word) > 2:
        return 2
    if word.endswith("!"):
        return 2
    return 0


def _chunk_words(words: list[dict], style: str) -> list[list[dict]]:
    """Group words into subtitle cues based on style and natural pauses."""
    if not words:
        return []

    # Filter fillers for non-comedy styles (comedy may want to show reactions)
    filtered: list[dict] = []
    for w in words:
        if w.get("type") == "audio_event":
            filtered.append(w)
            continue
        text = _clean_word(w.get("text") or "")
        if not text:
            continue
        if style != "comedy" and _is_filler(text):
            continue
        filtered.append(w)

    if not filtered:
        return []

    max_words = {"bold-overlay": 3, "punch-word": 4, "natural": 8, "comedy": 3}[style]
    min_words = {"bold-overlay": 1, "punch-word": 2, "natural": 3, "comedy": 1}[style]

    chunks: list[list[dict]] = []
    current: list[dict] = []

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = []

    for i, w in enumerate(filtered):
        text = _clean_word(w.get("text") or "")
        gap = _gap_before(filtered, i)

        # Audio events (laughs) get their own cue in comedy mode
        if w.get("type") == "audio_event" and style == "comedy":
            flush()
            chunks.append([w])
            continue

        if current and gap >= STRONG_GAP:
            flush()
        elif current and gap >= PHRASE_GAP and len(current) >= min_words:
            flush()

        current.append(w)

        ends_punct = bool(text) and text[-1] in PUNCT_BREAK
        at_max = len(current) >= max_words

        if style == "punch-word":
            # Break on punctuation or when we have enough context for a punch
            if ends_punct or at_max:
                flush()
        elif style == "natural":
            if ends_punct and len(current) >= min_words:
                flush()
            elif at_max:
                flush()
        elif style == "comedy":
            # Shorter chunks around emphasis / laughs
            emphasis = _score_emphasis(text)
            if ends_punct or at_max or (emphasis >= 2 and len(current) >= 2):
                flush()
        else:  # bold-overlay
            if ends_punct or at_max or len(current) >= 2:
                flush()

    flush()
    return chunks


def _format_cue_text(chunk: list[dict], style: str) -> str:
    parts: list[str] = []
    for w in chunk:
        t = w.get("type", "word")
        raw = _clean_word(w.get("text") or "")
        if not raw:
            continue
        if t == "audio_event":
            if not raw.startswith("("):
                raw = f"({raw})"
            parts.append(raw.upper() if style != "natural" else raw)
            continue
        parts.append(raw)

    text = " ".join(parts)
    text = text.replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!")

    if style == "natural":
        # Sentence case: capitalize first letter only
        if text:
            text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
        return text

    if style == "punch-word":
        words = text.split()
        if not words:
            return text
        # Highlight the highest-scored word with ASS override (yellow + scale)
        scores = [(_score_emphasis(w), i) for i, w in enumerate(words)]
        best_idx = max(scores, key=lambda x: (x[0], -x[1]))[1]
        if scores[best_idx][0] > 0:
            highlighted = []
            for i, w in enumerate(words):
                if i == best_idx:
                    highlighted.append(r"{\c&H00FFFF&\b1}" + w.upper() + r"{\r}")
                else:
                    highlighted.append(w.upper())
            return " ".join(highlighted)
        return text.upper()

    if style == "comedy":
        # Uppercase punchlines; keep (LAUGHS) readable
        if LAUGH_EVENT.search(text):
            return text.upper()
        words = text.split()
        if any(_score_emphasis(w) >= 2 for w in words):
            return text.upper()
        return text.upper()

    # bold-overlay default
    text = text.rstrip(",;:")
    return text.upper()


def build_master_srt(
    edl: dict,
    edit_dir: Path,
    out_path: Path,
    style: str | None = None,
) -> int:
    """Build output-timeline SRT. Returns number of cues written."""
    style = style or edl.get("subtitle_style") or "bold-overlay"
    transcripts_dir = edit_dir / "transcripts"

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        # Skip subtitle generation for music-led / visual-only segments
        if r.get("type") in ("meme", "insert", "broll", "intro", "outro") and not r.get("caption"):
            seg_offset += seg_duration
            continue

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)
        chunks = _chunk_words(words_in_seg, style)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset

            # Extend short cues slightly for readability; cap long ones
            duration = out_end - out_start
            if duration < MIN_CUE:
                out_end = out_start + MIN_CUE
            elif duration > MAX_CUE:
                out_end = out_start + MAX_CUE

            text = _format_cue_text(chunk, style)
            if not text.strip():
                continue
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues, style={style})")
    return len(entries)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build smart subtitles from EDL + transcripts")
    ap.add_argument("--edl", type=Path, required=True, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output .srt path")
    ap.add_argument(
        "--style",
        type=str,
        default=None,
        choices=list(SUB_STYLES),
        help="Subtitle style (overrides EDL subtitle_style)",
    )
    ap.add_argument("--list-styles", action="store_true", help="List styles and exit")
    args = ap.parse_args()

    if args.list_styles:
        for name, fs in SUB_STYLES.items():
            print(f"{name}:")
            print(f"  {fs}")
            print()
        return

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    build_master_srt(edl, edl_path.parent, args.output.resolve(), style=args.style)


if __name__ == "__main__":
    main()
