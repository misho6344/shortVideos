#!/usr/bin/env python3
"""
make_trailer.py - generate a YouTube Shorts style trailer (9:16, max 3 minutes)
from a movie that is packed in an archive (.zip, .tar*, .rar, .7z).

Pipeline
  1. Extract the archive and find the main video (and an optional .srt subtitle).
  2. Analyse the film: audio loudness per second, scene cuts (ffmpeg) and
     dialogue density (from subtitles, if present).
  3. Score every second, pick the most engaging non-overlapping moments.
  4. Extend each moment to keep its context (snap to scene cuts / subtitle lines).
  5. Render each moment as 1080x1920 with a blurred background, fades,
     light colour grading, normalised audio and (optionally) burned-in subtitles.
  6. Optionally prepend a title card and concatenate everything into one MP4.

Requirements: Python 3.9+, ffmpeg and ffprobe on PATH. No third-party packages.

Example
  python3 make_trailer.py movie.zip -o out/trailer.mp4 --title "Movie Name" \
      --font /usr/share/fonts/truetype/noto/NotoSansGeorgian-Regular.ttf
"""

from __future__ import annotations

import argparse
import array
import bisect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}
SUB_EXTS = {".srt"}
OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30
MAX_SHORT_SECONDS = 180.0          # YouTube Shorts upper limit (3 minutes)
TITLE_SECONDS = 2.5
SAMPLE_RATE = 8000                 # mono analysis rate for loudness


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"error: '{tool}' was not found on PATH")


@dataclass
class Sub:
    start: float
    end: float
    text: str


@dataclass
class Clip:
    start: float
    end: float
    score: float

    @property
    def duration(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------- #
# 1. Archive extraction and media discovery
# --------------------------------------------------------------------------- #

def _is_within(base: Path, target: Path) -> bool:
    base = base.resolve()
    target = target.resolve()
    return os.path.commonpath([str(base), str(target)]) == str(base)


def extract_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if not _is_within(dest, dest / member):
                    raise ValueError(f"unsafe path inside archive: {member}")
            zf.extractall(dest)

    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not _is_within(dest, dest / member.name):
                    raise ValueError(f"unsafe path inside archive: {member.name}")
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:
                tf.extractall(dest)

    elif name.endswith((".rar", ".7z")):
        if shutil.which("7z"):
            cmd = ["7z", "x", "-y", f"-o{dest}", str(archive)]
        elif shutil.which("unrar"):
            cmd = ["unrar", "x", "-y", str(archive), str(dest) + os.sep]
        elif shutil.which("bsdtar"):
            cmd = ["bsdtar", "-xf", str(archive), "-C", str(dest)]
        else:
            sys.exit("error: .rar/.7z needs one of: 7z, unrar, bsdtar")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)

    else:
        raise ValueError(f"unsupported archive type: {archive.name}")


def find_media(root: Path, subs_override: Path | None) -> tuple[Path, Path | None]:
    files = [p for p in root.rglob("*") if p.is_file()]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
    if not videos:
        raise FileNotFoundError("no video file found inside the archive")
    video = max(videos, key=lambda p: p.stat().st_size)

    if subs_override:
        return video, subs_override

    subs = [p for p in files if p.suffix.lower() in SUB_EXTS]
    same_name = [s for s in subs if s.stem.lower() == video.stem.lower()]
    if same_name:
        return video, same_name[0]
    if len(subs) == 1:
        return video, subs[0]
    return video, None


# --------------------------------------------------------------------------- #
# 2. Analysis
# --------------------------------------------------------------------------- #

def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout
    info = json.loads(out)
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    if "video" not in kinds or "audio" not in kinds:
        raise ValueError("input must contain both a video and an audio stream")
    return float(info["format"]["duration"])


def detect_scene_cuts(video: Path, threshold: float) -> list[float]:
    log("Detecting scene cuts (this decodes the whole film, it can take a while)...")
    vf = f"scale=320:-2,select='gt(scene,{threshold})',showinfo"
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
         "-an", "-vf", vf, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    cuts = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", res.stderr)]
    return sorted(cuts)


def audio_energy_per_second(video: Path) -> list[float]:
    """RMS loudness for every second of the film (mono, 8 kHz)."""
    log("Measuring audio energy...")
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    step = SAMPLE_RATE * 2                 # bytes per second of 16-bit mono
    energies: list[float] = []
    pending = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(step * 60)
        if not chunk:
            break
        pending += chunk
        full = len(pending) - len(pending) % step
        for off in range(0, full, step):
            sec = array.array("h")
            sec.frombytes(pending[off:off + step])
            if sec:
                energies.append(math.sqrt(sum(x * x for x in sec) / len(sec)))
            else:
                energies.append(0.0)
        pending = pending[full:]
    proc.wait()
    return energies


TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _parse_time(s: str) -> float:
    m = TIME_RE.search(s)
    if not m:
        raise ValueError(f"bad SRT timestamp: {s!r}")
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def load_subs(path: Path) -> list[Sub]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")

    subs: list[Sub] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                left, right = line.split("-->", 1)
                try:
                    start, end = _parse_time(left), _parse_time(right)
                except ValueError:
                    break
                body = "\n".join(lines[i + 1:]).strip()
                body = re.sub(r"<[^>]+>|\{[^}]+\}", "", body)
                if end > start and body:
                    subs.append(Sub(start, end, body))
                break
    subs.sort(key=lambda s: s.start)
    log(f"Loaded {len(subs)} subtitle lines from {path.name}")
    return subs


# --------------------------------------------------------------------------- #
# 3. Scoring and moment selection
# --------------------------------------------------------------------------- #

def build_scores(duration: float, energies: list[float],
                 cuts: list[float], subs: list[Sub]) -> list[float]:
    """Interest score for every second of the film, roughly in [0, 1]."""
    n = int(duration)

    # Loudness relative to the film's own 95th percentile (robust to peaks).
    if energies:
        ordered = sorted(energies)
        ref = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] or 1.0
    else:
        ref = 1.0
    e_norm = [min(1.0, energies[t] / ref) if t < len(energies) else 0.0 for t in range(n)]

    # Scene-cut density in a +-2 second neighbourhood.
    cut_count = [0] * n
    for c in cuts:
        if 0 <= int(c) < n:
            cut_count[int(c)] += 1
    cut_norm = [0.0] * n
    for t in range(n):
        lo, hi = max(0, t - 2), min(n, t + 3)
        cut_norm[t] = min(1.0, sum(cut_count[lo:hi]) / 2.0)

    # Dialogue presence, weighted by how much is being said.
    speech = [0.0] * n
    for s in subs:
        val = 0.5 + 0.5 * min(1.0, len(s.text) / 80.0)
        for t in range(max(0, int(s.start)), min(n, math.ceil(s.end))):
            speech[t] = max(speech[t], val)

    if subs:
        w_e, w_c, w_s = 0.45, 0.25, 0.30
    else:
        w_e, w_c, w_s = 0.60, 0.40, 0.0

    return [w_e * e_norm[t] + w_c * cut_norm[t] + w_s * speech[t] for t in range(n)]


def pick_clips(score: list[float], duration: float, target: float,
               clip_len: float, gap: float) -> list[Clip]:
    """Greedy pick of the best non-overlapping windows until `target` seconds."""
    prefix = [0.0]
    for v in score:
        prefix.append(prefix[-1] + v)

    length = max(1, int(round(clip_len)))
    lo = int(duration * 0.03)          # skip most of the opening credits
    hi = int(duration * 0.92)          # skip most of the end credits

    candidates = []
    for s in range(lo, hi - length):
        mean = (prefix[s + length] - prefix[s]) / length
        candidates.append((mean, s))
    candidates.sort(reverse=True)

    chosen: list[Clip] = []
    total = 0.0
    for mean, s in candidates:
        e = s + length
        if any(s < c.end + gap and c.start < e + gap for c in chosen):
            continue
        chosen.append(Clip(float(s), float(e), mean))
        total += length
        if total >= target:
            break
    return chosen


def expand_context(clip: Clip, cuts: list[float], subs: list[Sub],
                   duration: float, min_len: float, max_len: float) -> Clip:
    """Grow a moment so it starts and ends at natural boundaries."""
    start, end = clip.start, clip.end

    # Start on a scene cut shortly before, if there is one.
    i = bisect.bisect_right(cuts, start)
    if i > 0 and start - cuts[i - 1] <= 4.0:
        start = cuts[i - 1]
    # Do not begin in the middle of a subtitle line.
    for s in subs:
        if s.start < start < s.end:
            start = max(0.0, s.start - 0.2)
            break

    # End on a scene cut shortly after, if there is one.
    j = bisect.bisect_left(cuts, end)
    if j < len(cuts) and cuts[j] - end <= 4.0:
        end = cuts[j]
    # Do not cut a subtitle line in half.
    for s in subs:
        if s.start < end < s.end:
            end = min(duration, s.end + 0.2)
            break

    if end - start > max_len:
        end = start + max_len
    if end - start < min_len:
        end = min(duration, start + min_len)
    return Clip(start, end, clip.score)


def resolve_overlaps(clips: list[Clip]) -> list[Clip]:
    """Keep the higher scored clip when two expanded clips overlap."""
    kept: list[Clip] = []
    for c in sorted(clips, key=lambda c: c.score, reverse=True):
        if all(c.end <= k.start or c.start >= k.end for k in kept):
            kept.append(c)
    return sorted(kept, key=lambda c: c.start)


def fit_to_target(clips: list[Clip], target: float, min_len: float) -> list[Clip]:
    out: list[Clip] = []
    total = 0.0
    for c in clips:
        if total + c.duration <= target:
            out.append(c)
            total += c.duration
            continue
        remaining = target - total
        if remaining >= min_len:
            out.append(Clip(c.start, c.start + remaining, c.score))
        break
    return out


# --------------------------------------------------------------------------- #
# 4. Rendering
# --------------------------------------------------------------------------- #

def fmt_srt_time(t: float) -> str:
    total_ms = int(round(t * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_clip_srt(subs: list[Sub], start: float, end: float, path: Path) -> bool:
    entries = []
    for s in subs:
        if s.end <= start or s.start >= end:
            continue
        entries.append((max(s.start, start) - start, min(s.end, end) - start, s.text))
    if not entries:
        return False
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, text) in enumerate(entries, 1):
            f.write(f"{i}\n{fmt_srt_time(a)} --> {fmt_srt_time(b)}\n{text}\n\n")
    return True


def render_clip(video: Path, clip: Clip, out_name: str, workdir: Path,
                sub_name: str | None, font_name: str, sub_margin: int,
                sub_size: int) -> None:
    """Render one moment as a 1080x1920 segment (paths are relative to workdir)."""
    dur = clip.duration
    fade_out = max(0.0, dur - 0.5)

    video_graph = (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},boxblur=20:2[bg];"
        f"[fgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        "eq=contrast=1.05:saturation=1.1,"
        f"fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out:.3f}:d=0.5"
    )
    if sub_name:
        # libass scales against the 1080x1920 canvas, so sizes are kept modest.
        style = (f"FontName={font_name},FontSize={sub_size},Alignment=2,"
                 f"MarginV={sub_margin},Outline=2,Shadow=0")
        video_graph += (f",subtitles={sub_name}:original_size={OUT_W}x{OUT_H}"
                        f":force_style='{style}'")
    video_graph += "[v]"

    audio_graph = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,"
        f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out:.3f}:d=0.5,"
        "loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{clip.start:.3f}", "-i", str(video.resolve()),
        "-t", f"{dur:.3f}",
        "-filter_complex", f"{video_graph};{audio_graph}",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(OUT_FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_name,
    ]
    subprocess.run(cmd, check=True, cwd=workdir)


def render_title_card(title: str, font_src: Path, workdir: Path, out_name: str) -> None:
    shutil.copy(font_src, workdir / "title_font.ttf")
    (workdir / "title.txt").write_text(title, encoding="utf-8")
    vf = ("drawtext=fontfile=title_font.ttf:textfile=title.txt:fontcolor=white:"
          "fontsize=72:x=(w-text_w)/2:y=(h-text_h)/2,"
          f"fade=t=in:st=0:d=0.5,fade=t=out:st={TITLE_SECONDS - 0.5}:d=0.5")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={OUT_W}x{OUT_H}:d={TITLE_SECONDS}:r={OUT_FPS}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", vf, "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        out_name,
    ]
    subprocess.run(cmd, check=True, cwd=workdir)


def concat_parts(parts: list[str], out: Path, workdir: Path) -> None:
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", "concat.txt",
         "-c", "copy", "-movflags", "+faststart", str(out.resolve())],
        check=True, cwd=workdir,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path, help="archive that contains the movie")
    ap.add_argument("-o", "--output", type=Path, default=Path("trailer.mp4"))
    ap.add_argument("--duration", type=float, default=170.0,
                    help="target length in seconds (capped at 180)")
    ap.add_argument("--clip-length", type=float, default=12.0,
                    help="preferred length of each moment, seconds")
    ap.add_argument("--min-clip", type=float, default=4.0)
    ap.add_argument("--max-clip", type=float, default=20.0)
    ap.add_argument("--gap", type=float, default=8.0,
                    help="minimum distance between two chosen moments, seconds")
    ap.add_argument("--scene-threshold", type=float, default=0.35,
                    help="ffmpeg scene-change sensitivity (0-1, lower = more cuts)")
    ap.add_argument("--subs", type=Path, help="use this .srt instead of auto-detecting")
    ap.add_argument("--no-subs", action="store_true", help="do not burn in subtitles")
    ap.add_argument("--font-name", default="Noto Sans Georgian",
                    help="font family for burned-in subtitles (must support Georgian)")
    ap.add_argument("--sub-margin", type=int, default=180,
                    help="subtitle distance from the bottom edge (libass units)")
    ap.add_argument("--sub-size", type=int, default=20, help="subtitle font size")
    ap.add_argument("--title", help="optional title card shown at the start")
    ap.add_argument("--font", type=Path,
                    help="TTF font file for the title card (must support Georgian)")
    ap.add_argument("--order", choices=["chronological", "score"], default="chronological")
    ap.add_argument("--work-dir", type=Path, help="keep intermediate files here")
    args = ap.parse_args()

    require_tools()
    if not args.archive.is_file():
        sys.exit(f"error: archive not found: {args.archive}")

    target_total = min(args.duration, MAX_SHORT_SECONDS)
    intro = TITLE_SECONDS if args.title else 0.0
    target_clips = target_total - intro

    keep_work = args.work_dir is not None
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="trailer_"))
    work.mkdir(parents=True, exist_ok=True)
    render_dir = work / "render"
    render_dir.mkdir(exist_ok=True)

    try:
        log("Extracting archive...")
        extracted = work / "extracted"
        extract_archive(args.archive, extracted)

        video, sub_file = find_media(extracted, args.subs)
        if args.no_subs:
            sub_file = None
        log(f"Movie: {video.name}" + (f" | subtitles: {sub_file.name}" if sub_file else " | no subtitles"))

        duration = probe_duration(video)
        log(f"Duration: {duration / 60:.1f} min")

        cuts = detect_scene_cuts(video, args.scene_threshold)
        log(f"Scene cuts found: {len(cuts)}")
        energies = audio_energy_per_second(video)
        subs = load_subs(sub_file) if sub_file else []

        score = build_scores(duration, energies, cuts, subs)

        # Pick a little more than needed; fit_to_target trims the rest.
        raw = pick_clips(score, duration, target_clips * 1.1, args.clip_length, args.gap)
        expanded = [expand_context(c, cuts, subs, duration, args.min_clip, args.max_clip)
                    for c in raw]
        clips = fit_to_target(resolve_overlaps(expanded), target_clips, args.min_clip)
        if not clips:
            sys.exit("error: no suitable moments were found")
        if args.order == "score":
            clips.sort(key=lambda c: c.score, reverse=True)

        font_src = args.font
        if args.title and font_src is None:
            log("warning: --title given without --font, the title card is skipped")

        parts: list[str] = []
        if args.title and font_src:
            log("Rendering title card...")
            render_title_card(args.title, font_src, render_dir, "title.mp4")
            parts.append("title.mp4")

        for i, clip in enumerate(clips):
            log(f"Rendering moment {i + 1}/{len(clips)}: "
                f"{clip.start:.1f}s - {clip.end:.1f}s ({clip.duration:.1f}s)")
            sub_name = None
            if subs and write_clip_srt(subs, clip.start, clip.end, render_dir / f"clip{i:02}.srt"):
                sub_name = f"clip{i:02}.srt"
            part = f"part{i:02}.mp4"
            render_clip(video, clip, part, render_dir, sub_name,
                        args.font_name, args.sub_margin, args.sub_size)
            parts.append(part)

        out = args.output.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        log("Joining segments...")
        concat_parts(parts, out, render_dir)

        final_len = probe_duration(out)
        log(f"Done: {out} ({final_len:.1f}s)")
        if final_len > MAX_SHORT_SECONDS:
            log("warning: output is longer than 3 minutes, YouTube Shorts may reject it")

        meta = {
            "source": video.name,
            "output": str(out),
            "duration_seconds": round(final_len, 2),
            "moments": [{"start": round(c.start, 2), "end": round(c.end, 2),
                         "score": round(c.score, 4)} for c in clips],
        }
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
