"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip, analyze_segments_consistent  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}

    def analyze_segments_consistent(segments, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98"

try:
    from subtitles import build_master_srt, get_force_style  # same directory
except Exception:
    def get_force_style(style: str) -> str:  # type: ignore
        return (
            "FontName=Helvetica,FontSize=18,Bold=1,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=90"
        )

    def build_master_srt(edl, edit_dir, out_path, style=None):  # type: ignore
        raise RuntimeError("subtitles module not available")


# Default subtitle style — overridden by EDL `subtitle_style` via subtitles.py
SUB_FORCE_STYLE = get_force_style("bold-overlay")

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto' / 'auto_consistent', returns a sentinel resolved per-segment or once.
    """
    if not grade_field:
        return ""
    if grade_field in ("auto", "auto_consistent"):
        return f"__{grade_field.upper()}__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def flatten_edl(edl: dict, edit_dir: Path) -> tuple[dict, list[dict], list[dict]]:
    """Merge intro/outro into flat ranges; collect music-replace sections + title specs.

    Returns (normalized_edl, music_sections, title_specs).
    music_sections: [{from_output, to_output, file, music_start, volume}]
    title_specs: [{text, subtitle, at_output, duration}]
    """
    edl = json.loads(json.dumps(edl))  # deep copy
    all_ranges: list[dict] = []
    music_sections: list[dict] = []
    title_specs: list[dict] = []
    offset = 0.0

    def _absorb_section(section: dict | None, section_type: str) -> None:
        nonlocal offset
        if not section:
            return

        # Merge any sources declared in the section block
        for name, path in (section.get("sources_needed") or {}).items():
            edl.setdefault("sources", {})[name] = path

        section_ranges = section.get("ranges") or []
        section_dur = sum(float(r["end"]) - float(r["start"]) for r in section_ranges)
        if section_dur <= 0:
            return

        music = section.get("music") or {}
        if music.get("file"):
            music_sections.append({
                "from_output": offset,
                "to_output": offset + section_dur,
                "file": music["file"],
                "music_start": float(music.get("start", 0)),
                "volume": float(music.get("volume", 1.0)),
            })

        for t in section.get("titles") or []:
            title_specs.append({
                "text": t["text"],
                "subtitle": t.get("subtitle"),
                "at_output": offset + float(t.get("at", 0)),
                "duration": float(t.get("duration", 1.5)),
            })

        for r in section_ranges:
            rr = dict(r)
            rr["type"] = rr.get("type", section_type)
            rr["_section"] = section_type
            all_ranges.append(rr)
        offset += section_dur

    _absorb_section(edl.pop("intro", None), "intro")

    for r in edl.get("ranges") or []:
        all_ranges.append(r)
        offset += float(r["end"]) - float(r["start"])

    _absorb_section(edl.pop("outro", None), "outro")

    edl["ranges"] = all_ranges
    return edl, music_sections, title_specs


def generate_title_overlays(title_specs: list[dict], edit_dir: Path) -> list[dict]:
    """Render title card MP4s and return overlay dicts for the compositor."""
    if not title_specs:
        return []

    try:
        from title_card import render_title_video
    except ImportError:
        print("warning: title_card module not available, skipping title renders")
        return []

    titles_dir = edit_dir / "titles"
    titles_dir.mkdir(parents=True, exist_ok=True)
    overlays: list[dict] = []

    for i, t in enumerate(title_specs):
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", t["text"][:30])
        out = titles_dir / f"title_{i:02d}_{safe}.mp4"
        render_title_video(
            title=t["text"],
            out_path=out,
            duration=t["duration"],
            subtitle=t.get("subtitle"),
        )
        overlays.append({
            "file": str(out.relative_to(edit_dir)),
            "start_in_output": t["at_output"],
            "duration": t["duration"],
        })
        print(f"  title card: \"{t['text']}\" @ {t['at_output']:.2f}s")

    return overlays


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def is_portrait_source(video: Path) -> bool:
    """Return True if the video's height > width (portrait / vertical)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        w, h = map(int, out.stdout.strip().split(","))
        return h > w
    except Exception:
        return False


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    mute_audio: bool = False,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    portrait = is_portrait_source(source)
    if draft:
        scale = "scale=-2:1280" if portrait else "scale=1280:-2"
    else:
        scale = "scale=-2:1920" if portrait else "scale=1920:-2"

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    if mute_audio:
        af = "volume=0"
    else:
        af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _extract_one_segment(
    i: int,
    r: dict,
    sources: dict,
    edit_dir: Path,
    resolved: str,
    is_auto: bool,
    consistent_filter: str,
    clips_dir: Path,
    preview: bool,
    draft: bool,
) -> tuple[int, Path]:
    """Worker for parallel segment extraction. Returns (index, path)."""
    src_name = r["source"]
    src_path = resolve_path(sources[src_name], edit_dir)
    start = float(r["start"])
    end = float(r["end"])
    duration = end - start
    out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

    if consistent_filter:
        seg_filter = consistent_filter
    elif is_auto:
        seg_filter, _ = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
    else:
        seg_filter = resolved

    # Meme/insert/broll/intro/outro audio handling
    seg_type = r.get("type", "take")
    mute = False
    if seg_type in ("intro", "outro"):
        mute = True  # music-led — source audio always muted
    elif seg_type in ("meme", "insert", "broll"):
        keep_audio = r.get("keep_audio")
        if keep_audio is True:
            mute = False
        elif keep_audio is False:
            mute = True
        elif seg_type == "broll":
            mute = True
        else:
            mute = False  # memes: keep audio (sound memes are the point)

    extract_segment(
        src_path, start, duration, seg_filter, out_path,
        preview=preview, draft=draft, mute_audio=mute,
    )
    return i, out_path


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    workers: int | None = None,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    is_auto_consistent = resolved == "__AUTO_CONSISTENT__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    # Pre-compute one unified grade for auto_consistent mode
    consistent_filter = ""
    if is_auto_consistent:
        segs = []
        for r in ranges:
            src_path = resolve_path(sources[r["source"]], edit_dir)
            segs.append((src_path, float(r["start"]), float(r["end"]) - float(r["start"])))
        consistent_filter = analyze_segments_consistent(segs, verbose=True)
        print(f"  consistent grade: {consistent_filter or '(none)'}")

    if workers is None:
        workers = min(4, max(1, (os.cpu_count() or 4) // 2))

    seg_paths: list[Path | None] = [None] * len(ranges)
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/ ({workers} workers)")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")

    if workers <= 1 or len(ranges) == 1:
        for i, r in enumerate(ranges):
            note = r.get("beat") or r.get("note") or ""
            start, end = float(r["start"]), float(r["end"])
            print(f"  [{i:02d}] {r['source']}  {start:7.2f}-{end:7.2f}  ({end-start:5.2f}s)  {note}")
            _, path = _extract_one_segment(
                i, r, sources, edit_dir, resolved, is_auto, consistent_filter,
                clips_dir, preview, draft,
            )
            seg_paths[i] = path
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _extract_one_segment,
                    i, r, sources, edit_dir, resolved, is_auto, consistent_filter,
                    clips_dir, preview, draft,
                ): (i, r)
                for i, r in enumerate(ranges)
            }
            for fut in as_completed(futures):
                i, r = futures[fut]
                try:
                    idx, path = fut.result()
                    note = r.get("beat") or r.get("note") or ""
                    start, end = float(r["start"]), float(r["end"])
                    print(f"  [{idx:02d}] {r['source']}  {start:7.2f}-{end:7.2f}  ({end-start:5.2f}s)  {note}")
                    seg_paths[idx] = path
                except Exception as e:
                    print(f"  [{i:02d}] FAILED: {e}")
                    raise

    return [p for p in seg_paths if p is not None]


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Music bed mixing ---------------------------------------------------


def mix_music_bed(
    base_path: Path,
    music_spec: dict,
    edit_dir: Path,
    out_path: Path,
    preview: bool = False,
) -> None:
    """Mix a background music track under the base video with optional ducking.

    music_spec keys:
      file          — path to audio (mp3/wav/m4a)
      volume        — music level 0.0-1.0 (default 0.22)
      start_at      — when music starts in output timeline (default 0)
      fade_in       — seconds (default 1.0)
      fade_out      — seconds (default 2.0)
      duck_on_speech — lower music under dialogue (default true)
    """
    music_path = resolve_path(music_spec["file"], edit_dir)
    if not music_path.exists():
        print(f"warning: music file not found: {music_path}")
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    volume = float(music_spec.get("volume", 0.22))
    fade_in = float(music_spec.get("fade_in", 1.0))
    fade_out = float(music_spec.get("fade_out", 2.0))
    duck = music_spec.get("duck_on_speech", True)

    # Probe base duration
    dur_out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(base_path)],
        stderr=subprocess.DEVNULL,
    )
    duration = float(dur_out.decode().strip())
    fade_out_start = max(0.0, duration - fade_out)

    # Loop/trim music to match video length, apply volume + fades
    music_chain = (
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:{duration:.3f},"
        f"volume={volume},"
        f"afade=t=in:st=0:d={fade_in},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out}[music]"
    )

    if duck:
        # Sidechain compress music against dialogue — music dips when speech is loud
        filter_complex = (
            f"{music_chain};"
            f"[0:a][music]sidechaincompress=threshold=0.02:ratio=8:attack=5:release=200:makeup=1[ducked];"
            f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    else:
        filter_complex = f"{music_chain};[0:a][music]amix=inputs=2:duration=first:dropout_transition=0[aout]"

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(base_path),
        "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"music mix → {out_path.name} (vol={volume}, duck={'on' if duck else 'off'})")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def apply_music_sections(
    base_path: Path,
    music_sections: list[dict],
    edit_dir: Path,
    out_path: Path,
) -> None:
    """Replace audio in intro/outro sections with music slices (source video muted there)."""
    if not music_sections:
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    mute_expr = "1"
    for sec in sorted(music_sections, key=lambda s: s["from_output"], reverse=True):
        f, t = sec["from_output"], sec["to_output"]
        mute_expr = f"if(between(t,{f:.3f},{t:.3f}),0,{mute_expr})"

    inputs = ["-i", str(base_path)]
    filter_parts = [f"[0:a]volume='{mute_expr}':eval=frame[basea]"]
    mix_labels = ["[basea]"]

    for i, sec in enumerate(music_sections):
        music_path = resolve_path(sec["file"], edit_dir)
        inputs += ["-i", str(music_path)]
        sec_dur = sec["to_output"] - sec["from_output"]
        delay_ms = int(sec["from_output"] * 1000)
        vol = sec.get("volume", 1.0)
        mstart = sec.get("music_start", 0)
        label = f"[m{i}]"
        filter_parts.append(
            f"[{i + 1}:a]atrim=start={mstart:.3f}:duration={sec_dur:.3f},"
            f"asetpts=PTS-STARTPTS,volume={vol},"
            f"adelay={delay_ms}|{delay_ms}{label}"
        )
        mix_labels.append(label)

    n_inputs = len(mix_labels)
    filter_parts.append(
        f"{''.join(mix_labels)}amix=inputs={n_inputs}:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"music sections → {out_path.name} ({len(music_sections)} section(s), replace mode)")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    force_style: str | None = None,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    sub_style = force_style or SUB_FORCE_STYLE
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{sub_style}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel workers for segment extraction (default: min(4, cpu//2))",
    )
    ap.add_argument(
        "--subtitle-style",
        type=str,
        default=None,
        choices=["bold-overlay", "punch-word", "natural", "comedy"],
        help="Subtitle style (overrides EDL subtitle_style)",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # Flatten intro/outro blocks, collect music-replace sections + title specs
    edl, music_sections, title_specs = flatten_edl(edl, edit_dir)
    if music_sections:
        print(f"intro/outro: {len(music_sections)} music-led section(s)")
    if title_specs:
        print(f"generating {len(title_specs)} title card(s)")
        title_overlays = generate_title_overlays(title_specs, edit_dir)
        edl.setdefault("overlays", [])
        edl["overlays"] = title_overlays + edl["overlays"]

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, workers=args.workers,
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 2b. Music-led intro/outro sections (replace source audio with music slices)
    if music_sections:
        sections_out = edit_dir / base_name.replace(".mp4", "_sections.mp4")
        apply_music_sections(base_path, music_sections, edit_dir, sections_out)
        base_path = sections_out

    # 2c. Optional background music bed under dialogue (main body)
    music_spec = edl.get("music")
    if music_spec and music_spec.get("file"):
        music_out = edit_dir / base_name.replace(".mp4", "_music.mp4")
        mix_music_bed(base_path, music_spec, edit_dir, music_out, preview=args.preview)
        base_path = music_out

    # 3. Subtitles: build if requested, resolve final path + style
    subs_path: Path | None = None
    sub_style_name = args.subtitle_style or edl.get("subtitle_style") or "bold-overlay"
    force_style = get_force_style(sub_style_name)
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path, style=sub_style_name)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        build_final_composite(
            base_path, overlays, subs_path, out_path, edit_dir, force_style=force_style,
        )
    else:
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(
            base_path, overlays, subs_path, tmp_composite, edit_dir, force_style=force_style,
        )
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft or args.preview)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
