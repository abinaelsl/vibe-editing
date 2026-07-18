"""Generate short title-card overlay videos for intro/outro sequences.

Renders text on a near-black background with fade in/out, exports to MP4 for
use as EDL overlays (PTS-shifted by render.py).

Usage:
    python helpers/title_card.py "MY CHANNEL" -o edit/titles/intro_title.mp4
    python helpers/title_card.py "SUBSCRIBE" --duration 2.0 --subtitle "new videos weekly"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in FONT_CANDIDATES:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size, index=1 if bold and fp.endswith(".ttc") else 0)
            except Exception:
                continue
    return ImageFont.load_default()


def render_frame(
    width: int,
    height: int,
    title: str,
    subtitle: str | None,
    alpha: float,
) -> Image.Image:
    img = Image.new("RGBA", (width, height), (10, 10, 10, int(220 * alpha)))
    draw = ImageDraw.Draw(img)

    title_font = load_font(72 if height >= 1080 else 48)
    sub_font = load_font(36 if height >= 1080 else 24, bold=False)

    # Title
    tb = draw.textbbox((0, 0), title, font=title_font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    tx = (width - tw) // 2
    ty = (height - th) // 2 - (30 if subtitle else 0)
    accent = (255, 90, 0, int(255 * alpha))
    white = (255, 255, 255, int(255 * alpha))
    draw.text((tx, ty), title, fill=white, font=title_font)
    # Accent underline
    draw.rectangle((tx, ty + th + 8, tx + tw, ty + th + 12), fill=accent)

    if subtitle:
        sb = draw.textbbox((0, 0), subtitle, font=sub_font)
        sw = sb[2] - sb[0]
        sx = (width - sw) // 2
        gray = (160, 160, 160, int(255 * alpha))
        draw.text((sx, ty + th + 28), subtitle, fill=gray, font=sub_font)

    return img


def ease_in_out(t: float) -> float:
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2


def render_title_video(
    title: str,
    out_path: Path,
    duration: float = 1.5,
    fps: int = 24,
    width: int = 1920,
    height: int = 1080,
    subtitle: str | None = None,
    fade: float = 0.25,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(duration * fps))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for i in range(n_frames):
            t = i / max(1, n_frames - 1)
            # Fade in/out at edges
            if t < fade / duration:
                alpha = ease_in_out(t * duration / fade)
            elif t > 1 - fade / duration:
                alpha = ease_in_out((1 - t) * duration / fade)
            else:
                alpha = 1.0
            frame = render_frame(width, height, title.upper(), subtitle, alpha)
            frame.save(tmp_dir / f"f_{i:04d}.png")

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-framerate", str(fps),
            "-i", str(tmp_dir / "f_%04d.png"),
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a title card overlay video")
    ap.add_argument("title", type=str, help="Main title text")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--subtitle", type=str, default=None)
    ap.add_argument("--duration", type=float, default=1.5)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()

    render_title_video(
        title=args.title,
        out_path=args.output.resolve(),
        duration=args.duration,
        width=args.width,
        height=args.height,
        subtitle=args.subtitle,
    )
    print(f"title card → {args.output} ({args.duration}s)")


if __name__ == "__main__":
    main()
