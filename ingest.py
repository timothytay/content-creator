"""
ingest.py — Ingest source MP4/MOV files into the persistent clip library.

For each new source file:
  1. Hash it (SHA-256) — skip if already ingested
  2. Split into 4-second segments with FFmpeg
  3. Extract 3 frames per segment
  4. Tag each segment with Claude Vision (async, batched)
  5. Write everything to the SQLite library
"""

import asyncio
import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

import openai
from tqdm import tqdm

import config
import library


# ── Helpers ────────────────────────────────────────────────────────────────────

def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def probe_duration(path: str | Path) -> float:
    """Return video duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def split_into_clips(source_path: Path, dest_dir: Path, source_id: str) -> list[library.Clip]:
    """
    Split source_path into CLIP_DURATION-second segments.
    Returns a list of Clip objects (not yet tagged).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(dest_dir / "clip_%04d.mp4")

    subprocess.run([
        "ffmpeg", "-i", str(source_path),
        "-c", "copy",
        "-map", "0:v:0",            # video only — no audio in b-roll clips
        "-segment_time", str(config.CLIP_DURATION),
        "-reset_timestamps", "1",
        "-f", "segment",
        pattern,
    ], check=True, capture_output=True)

    clips = []
    for idx, clip_file in enumerate(sorted(dest_dir.glob("clip_*.mp4"))):
        clip_id   = f"{source_id[:8]}__{idx:04d}"
        start_sec = idx * config.CLIP_DURATION
        end_sec   = start_sec + config.CLIP_DURATION
        clips.append(library.Clip(
            id=clip_id,
            source_id=source_id,
            clip_path=str(clip_file.resolve()),
            start_sec=start_sec,
            end_sec=end_sec,
        ))

    return clips


def extract_frames(clip_path: str, n: int = config.FRAMES_PER_CLIP) -> list[Path]:
    """
    Extract n evenly-spaced JPEG frames from clip_path.
    Returns list of temp file paths (caller must clean up).
    """
    tmpdir = Path(tempfile.mkdtemp(dir=config.FRAMES_DIR))

    # Use actual clip duration so short tail clips still produce frames.
    # fps=n/duration spreads exactly n frames across however long the clip is.
    duration = max(probe_duration(clip_path), 0.1)
    fps_expr = f"{n}/{duration}"

    subprocess.run([
        "ffmpeg", "-i", clip_path,
        "-vf", f"fps={fps_expr},scale=640:-1",
        "-frames:v", str(n),   # cap output in case of rounding
        "-q:v", "3",
        str(tmpdir / "frame_%02d.jpg"),
    ], check=True, capture_output=True)

    return sorted(tmpdir.glob("frame_*.jpg"))


def frames_to_b64(frame_paths: list[Path]) -> list[dict]:
    """Convert frame files to OpenAI-compatible image content blocks."""
    content = []
    for p in frame_paths:
        data = base64.standard_b64encode(p.read_bytes()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{data}"},
        })
    return content


def cleanup_frames(frame_paths: list[Path]):
    for p in frame_paths:
        try:
            p.unlink(missing_ok=True)
            p.parent.rmdir()
        except Exception:
            pass


# ── Claude Vision tagging ──────────────────────────────────────────────────────

TAGGING_PROMPT = """You are tagging 4-second video clips for a b-roll library.
These frames are evenly spaced through a single 4-second clip.

Respond ONLY with valid JSON (no markdown, no preamble):
{
  "description": "2-3 sentence visual description of what is shown",
  "tags": ["tag1", "tag2", ..., "tag8"]
}

Tags should be concrete, searchable nouns and concepts useful for matching
this clip to voiceover topics (e.g. "ocean", "plastic waste", "seagull",
"beach", "pollution", "waves", "wildlife", "coastline")."""


async def tag_clip_async(
    clip: library.Clip,
    client: openai.AsyncOpenAI,
    semaphore: asyncio.Semaphore,
) -> tuple[library.Clip, str, list[str]]:
    """Tag a single clip with OpenAI Vision. Returns (clip, description, tags)."""
    async with semaphore:
        frame_paths = extract_frames(clip.clip_path)
        image_blocks = frames_to_b64(frame_paths)
        cleanup_frames(frame_paths)

        for attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model=config.VISION_MODEL,
                    max_completion_tokens=512,
                    messages=[
                        {"role": "system", "content": TAGGING_PROMPT},
                        {"role": "user", "content": image_blocks},
                    ],
                )
                raw = response.choices[0].message.content.strip()
                # Strip accidental markdown fences
                raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()
                parsed = json.loads(raw)
                return clip, parsed["description"], parsed["tags"]

            except (json.JSONDecodeError, KeyError, openai.APIError) as e:
                if attempt == 2:
                    print(f"  ⚠ Failed to tag {clip.id}: {e}")
                    return clip, "", []
                await asyncio.sleep(2 ** attempt)


async def tag_all_clips_async(clips: list[library.Clip]):
    """Tag all clips concurrently, respecting VISION_CONCURRENCY."""
    semaphore = asyncio.Semaphore(config.VISION_CONCURRENCY)

    print(f"  Tagging {len(clips)} clips with OpenAI Vision "
          f"(concurrency={config.VISION_CONCURRENCY})...")

    async with openai.AsyncOpenAI() as client:
        tasks = [tag_clip_async(c, client, semaphore) for c in clips]
        results = []
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), unit="clip"):
            result = await coro
            results.append(result)

    for clip, description, tags in results:
        if description:
            library.mark_clip_tagged(clip.id, description, tags)
        else:
            print(f"  ⚠ Clip {clip.id} left untagged (vision error)")


# ── Public entry point ─────────────────────────────────────────────────────────

def ingest_file(source_path: str | Path):
    source_path = Path(source_path).resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    print(f"\n── Ingesting: {source_path.name}")

    # 1. Hash check
    source_id = sha256_file(source_path)
    if library.source_exists(source_id):
        pending = library.get_untagged_clips_for_source(source_id)
        if not pending:
            print("  Already in library — skipping.")
            return
        print(f"  Already ingested — retrying {len(pending)} untagged clip(s).")
        asyncio.run(tag_all_clips_async(pending))
        stats = library.library_stats()
        print(f"  ✓ Done. Library: {stats['tagged']}/{stats['clips']} clips tagged "
              f"across {stats['sources']} sources.")
        return

    # 2. Probe duration
    duration = probe_duration(source_path)
    print(f"  Duration: {duration:.1f}s  →  ~{int(duration / config.CLIP_DURATION)} clips")

    # 3. Register source
    library.insert_source(source_id, source_path.name, duration)

    # 4. Split into clips
    dest_dir = config.CLIPS_DIR / source_id[:16]
    print("  Splitting into 4-second segments...")
    clips = split_into_clips(source_path, dest_dir, source_id)
    for clip in clips:
        library.insert_clip(clip)
    print(f"  Created {len(clips)} clips.")

    # 5. Tag all clips
    asyncio.run(tag_all_clips_async(clips))

    stats = library.library_stats()
    print(f"  ✓ Done. Library: {stats['tagged']}/{stats['clips']} clips tagged "
          f"across {stats['sources']} sources.")


VIDEO_EXTENSIONS = {".mp4", ".mov"}


def run_ingest(paths: list[str]):
    """CLI entry point — accepts files or directories."""
    library.init_db()
    video_files = []

    for p in paths:
        path = Path(p)
        if path.is_dir():
            for ext in VIDEO_EXTENSIONS:
                video_files.extend(sorted(path.glob(f"**/*{ext}")))
        elif path.suffix.lower() in VIDEO_EXTENSIONS:
            video_files.append(path)
        else:
            print(f"  Skipping unsupported format: {p}")

    if not video_files:
        print("No video files found (supported: .mp4, .mov).")
        return

    print(f"Found {len(video_files)} video file(s) to ingest.")
    for f in video_files:
        ingest_file(f)
