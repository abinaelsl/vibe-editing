"""Detect beats in a music track for intro/outro montage cutting.

Uses librosa beat tracking. Outputs a JSON beat grid the editor agent uses to
snap clip boundaries to musical hits.

Usage:
    python helpers/beats.py music/intro_track.mp3
    python helpers/beats.py music/beat.mp3 --start 0 --end 15 -o edit/beats_intro.json
    python helpers/beats.py music/beat.mp3 --every 2          # downsample to every 2nd beat
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import librosa
import numpy as np


def audio_to_wav(audio_path: Path, dest: Path, start: float = 0.0, duration: float | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(audio_path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", str(dest)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def detect_beats(
    audio_path: Path,
    start: float = 0.0,
    duration: float | None = None,
    every_nth: int = 1,
) -> dict:
    """Return beat timestamps relative to `start` in the source file."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "probe.wav"
        audio_to_wav(audio_path, wav, start=start, duration=duration)

        y, sr = librosa.load(str(wav), sr=22050, mono=True)
        if y.size == 0:
            return {"bpm": 120.0, "beats": [0.0], "segments": [], "duration": 0.0}

        track_duration = len(y) / sr
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)

        # Ensure beat 0 exists for clean segment math
        if beat_times.size == 0 or beat_times[0] > 0.05:
            beat_times = np.insert(beat_times, 0, 0.0)

        if every_nth > 1:
            beat_times = beat_times[::every_nth]

        # Segment durations between consecutive beats (what intro/outro cutting uses)
        segments: list[dict] = []
        for i in range(len(beat_times) - 1):
            a, b = float(beat_times[i]), float(beat_times[i + 1])
            segments.append({
                "beat_index": i,
                "start": round(a, 4),
                "end": round(b, 4),
                "duration": round(b - a, 4),
            })

        bpm = float(tempo) if np.isscalar(tempo) else float(tempo[0])

        return {
            "source": str(audio_path.resolve()),
            "offset_in_source": start,
            "duration": round(track_duration, 4),
            "bpm": round(bpm, 2),
            "beat_count": len(beat_times),
            "beats": [round(float(t), 4) for t in beat_times],
            "segments": segments,
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect beats in a music track")
    ap.add_argument("audio", type=Path, help="Music file (.mp3, .wav, .m4a)")
    ap.add_argument("--start", type=float, default=0.0, help="Analyze from this offset (seconds)")
    ap.add_argument("--end", type=float, default=None, help="Analyze until this offset (seconds)")
    ap.add_argument("--every", type=int, default=1, help="Keep every Nth beat (2 = half tempo cuts)")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Write JSON (default: stdout)")
    args = ap.parse_args()

    audio = args.audio.resolve()
    if not audio.exists():
        sys.exit(f"audio not found: {audio}")

    duration = None
    if args.end is not None:
        duration = args.end - args.start

    result = detect_beats(audio, start=args.start, duration=duration, every_nth=args.every)
    payload = json.dumps(result, indent=2)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
        print(f"beats → {args.output}  ({result['beat_count']} beats @ {result['bpm']} BPM, {result['duration']:.1f}s)")
    else:
        print(payload)


if __name__ == "__main__":
    main()
