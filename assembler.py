"""
assembler.py — Build the final video from a ScheduledGroup list.

Output structure (repeating):
  [clip_1][clip_2]...[clip_N]  ← group of 3-5 × 4 s clips
  [blank gap 4-8 s]            ← placeholder for AI face cam
  [clip_1][clip_2]...          ← next group
  ...
"""

import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import config
from scheduler import ScheduledGroup


# ── Blank clip generation ──────────────────────────────────────────────────────

def _blank_clip_path(duration: float) -> Path:
    """
    Return path to a black video clip of the requested duration.
    Clips are cached by duration so we don't regenerate on every produce run.
    """
    config.BLANKS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.BLANKS_DIR / f"blank_{duration:.1f}s.mp4"

    if not path.exists():
        w, h = config.OUTPUT_RESOLUTION.split("x")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s={w}x{h}:r={config.OUTPUT_FPS}",
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ], check=True, capture_output=True)

    return path


# ── Re-encode a single clip to a uniform spec ──────────────────────────────────

def _normalise_clip(src: str, dst: Path):
    """Re-encode src to match OUTPUT_RESOLUTION / FPS for seamless concat."""
    w, h = config.OUTPUT_RESOLUTION.split("x")
    subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={config.OUTPUT_FPS}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-an",          # b-roll has no audio
        str(dst),
    ], check=True, capture_output=True)


# ── Assemble ───────────────────────────────────────────────────────────────────

def _fcpxml_time(seconds: float) -> str:
    """Convert seconds to a frame-accurate FCPXML rational time string."""
    fps = config.OUTPUT_FPS
    frames = round(seconds * fps)
    return f"{frames}/{fps}s" if frames else "0s"


def export_fcpxml(
    schedule: list[ScheduledGroup],
    fcpxml_path: Path,
    voiceover_path: Path | None = None,
):
    """Write an FCPXML 1.9 timeline that opens directly in DaVinci Resolve."""
    fps = config.OUTPUT_FPS
    clip_dur = config.CLIP_DURATION
    w, h = config.OUTPUT_RESOLUTION.split("x")

    # Unique clip paths in timeline order
    seen: set[str] = set()
    ordered_clips: list[str] = []
    for group in schedule:
        for p in group.clip_paths:
            if p not in seen:
                ordered_clips.append(p)
                seen.add(p)

    # ── Resources ────────────────────────────────────────────────────────────────
    root = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(root, "resources")

    ET.SubElement(resources, "format", {
        "id": "r1",
        "name": f"FFVideoFormat{h}p{fps}",
        "frameDuration": f"1/{fps}s",
        "width": w,
        "height": h,
    })

    asset_id: dict[str, str] = {}
    next_id = 2

    for clip_path in ordered_clips:
        rid = f"r{next_id}"; next_id += 1
        asset_id[clip_path] = rid
        src = "file://" + quote(clip_path, safe="/:@")
        a = ET.SubElement(resources, "asset", {
            "id": rid,
            "name": Path(clip_path).stem,
            "src": src,
            "start": "0s",
            "duration": _fcpxml_time(clip_dur),
            "hasVideo": "1",
            "hasAudio": "0",
        })
        ET.SubElement(a, "media-rep", {"kind": "original-media", "src": src})

    vo_rid = None
    if voiceover_path and voiceover_path.exists():
        vo_rid = f"r{next_id}"
        vo_src = "file://" + quote(str(voiceover_path), safe="/:@")
        total_vo = schedule[-1].vo_end
        a = ET.SubElement(resources, "asset", {
            "id": vo_rid,
            "name": voiceover_path.stem,
            "src": vo_src,
            "start": "0s",
            "duration": _fcpxml_time(total_vo),
            "hasVideo": "0",
            "hasAudio": "1",
        })
        ET.SubElement(a, "media-rep", {"kind": "original-media", "src": vo_src})

    # ── Library → Event → Project → Sequence ─────────────────────────────────────
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "B-Roll Export"})
    project = ET.SubElement(event, "project", {"name": fcpxml_path.stem})

    total_dur = sum(len(g.clip_paths) * clip_dur + g.gap_duration for g in schedule)
    sequence = ET.SubElement(project, "sequence", {
        "format": "r1",
        "duration": _fcpxml_time(total_dur),
        "tcStart": "0s",
        "tcFormat": "NDF",
        "audioLayout": "stereo",
        "audioRate": "48k",
    })
    spine = ET.SubElement(sequence, "spine")

    # ── Spine: video clips interleaved with face-cam gap placeholders ─────────────
    cursor = 0.0
    first_clip_elem = None

    for group in schedule:
        for clip_path in group.clip_paths:
            elem = ET.SubElement(spine, "asset-clip", {
                "ref": asset_id[clip_path],
                "offset": _fcpxml_time(cursor),
                "name": Path(clip_path).stem,
                "duration": _fcpxml_time(clip_dur),
                "start": "0s",
            })
            if first_clip_elem is None:
                first_clip_elem = elem
            cursor += clip_dur

        ET.SubElement(spine, "gap", {
            "name": f"FACE CAM – {group.vo_topic}",
            "offset": _fcpxml_time(cursor),
            "duration": _fcpxml_time(group.gap_duration),
            "start": "0s",
        })
        cursor += group.gap_duration

    # Voiceover attached as connected audio on lane -1 (below primary storyline)
    if vo_rid and first_clip_elem is not None:
        ET.SubElement(first_clip_elem, "audio", {
            "lane": "-1",
            "offset": "0s",
            "ref": vo_rid,
            "duration": _fcpxml_time(schedule[-1].vo_end),
            "start": "0s",
            "role": "dialogue",
        })

    # ── Write ─────────────────────────────────────────────────────────────────────
    ET.indent(root, space="  ")
    xml_str = ET.tostring(root, encoding="unicode")
    fcpxml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + xml_str,
        encoding="utf-8",
    )
    print(f"  FCPXML  → {fcpxml_path}")


def assemble(
    schedule: list[ScheduledGroup],
    output_path: str | Path,
    voiceover_path: str | Path | None = None,
    schedule_json_path: str | Path | None = None,
):
    """
    Assemble the full b-roll video from a resolved schedule.

    Steps:
      1. Re-encode every source clip to a uniform resolution/fps
      2. Cache blank gap clips
      3. Write a concat list
      4. Run ffmpeg -f concat to produce the output
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="broll_norm_") as tmpdir:
        tmp = Path(tmpdir)
        concat_lines: list[str] = []
        total_clips = sum(len(g.clip_paths) for g in schedule)

        print(f"  Normalising {total_clips} clips → {config.OUTPUT_RESOLUTION} @ {config.OUTPUT_FPS}fps")

        for group in schedule:
            for idx, clip_path in enumerate(group.clip_paths):
                norm_path = tmp / f"g{group.index:03d}_c{idx:02d}.mp4"
                _normalise_clip(clip_path, norm_path)
                concat_lines.append(f"file '{norm_path}'")

            # Blank gap after every group
            blank = _blank_clip_path(group.gap_duration)
            concat_lines.append(f"file '{blank}'")

        # Write concat list
        concat_file = tmp / "concat.txt"
        concat_file.write_text("\n".join(concat_lines) + "\n")

        print(f"  Concatenating {len(concat_lines)} segments → {output_path.name}")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ], check=True)

    # Optionally save schedule JSON alongside the video
    if schedule_json_path:
        schedule_data = [
            {
                "group": g.index,
                "topic": g.vo_topic,
                "vo_window": [g.vo_start, g.vo_end],
                "clips": g.clip_ids,
                "gap_sec": g.gap_duration,
            }
            for g in schedule
        ]
        Path(schedule_json_path).write_text(
            json.dumps({"groups": schedule_data}, indent=2)
        )
        print(f"  Schedule saved → {schedule_json_path}")

    print(f"  ✓ Output: {output_path}")
    _print_timeline(schedule)

    export_fcpxml(
        schedule,
        output_path.with_suffix(".fcpxml"),
        voiceover_path=Path(voiceover_path) if voiceover_path else None,
    )


def _print_timeline(schedule: list[ScheduledGroup]):
    print("\n  ── Timeline ──────────────────────────────────────────")
    cursor = 0.0
    for g in schedule:
        clip_sec = len(g.clip_ids) * config.CLIP_DURATION
        print(f"  {cursor:6.1f}s  [{g.vo_topic[:30]:30s}]  "
              f"{len(g.clip_ids)} clips × 4s = {clip_sec}s  │  "
              f"{g.gap_duration}s blank gap")
        cursor += clip_sec + g.gap_duration
    print(f"  {cursor:6.1f}s  END")
    print("  ─────────────────────────────────────────────────────")
