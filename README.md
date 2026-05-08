# B-Roll Pipeline

Agentic workflow: ingest b-roll MP4s once → produce unlimited videos from voiceover MP3s.

## Requirements

- Python 3.11+
- FFmpeg (must be on PATH)
- OpenAI API key (`OPENAI_API_KEY` env var)

```bash
pip install -r requirements.txt
```

## Quick Start

### 1. Ingest b-roll (once per source file, ever)
```bash
python main.py ingest footage/ocean.mp4 footage/wildlife/
```
- Accepts `.mp4` and `.mov` files, or directories containing them
- Hashes each file — skips if already in library
- Splits into 4-second clips
- Extracts 3 frames per clip and vision-tags with GPT-4o
- Stores everything in `./broll_library/`

### 2. Produce a video
```bash
python main.py produce voiceover.mp3 --output my_video.mp4
```
Outputs:
- `my_video.mp4` — b-roll track with black gaps for face cam
- `my_video.fcpxml` — DaVinci Resolve timeline (clips, gaps, and voiceover pre-synced)
- `my_video.schedule.json` — full timeline plan for reference
- `voiceover_1.1x.mp3` — sped-up audio used in the timeline

### 3. Check library stats
```bash
python main.py stats
```

## DaVinci Resolve Workflow

Import the FCPXML to get a fully structured timeline in one step:

**File → Import → Timeline → `my_video.fcpxml`**

The imported timeline contains:
- **Video track** — b-roll clips placed at the correct positions
- **Audio track** — sped-up voiceover, pre-synced
- **Gap placeholders** — labeled `FACE CAM – {topic}`, one per group, ready to be filled with face cam footage

The reference `my_video.mp4` shows the intended edit if you need a visual guide.

## Output Structure

```
[3-5 clips × 4s]  ← topically matched to voiceover
[4-8s gap]        ← "FACE CAM – {topic}" placeholder
[3-5 clips × 4s]
[4-8s gap]
...
```

## Configuration

Edit `config.py` to change:

| Setting | Default | Description |
|---|---|---|
| `OPENAI_MODEL` | `gpt-4o` | Model used for vision tagging and scheduling |
| `LIBRARY_ROOT` | `./broll_library` | Clip/DB storage (or set `BROLL_LIBRARY` env var) |
| `GROUP_MIN_CLIPS` / `GROUP_MAX_CLIPS` | `3` / `5` | Clips per group |
| `GAP_MIN_SEC` / `GAP_MAX_SEC` | `4` / `8` | Blank gap range in seconds |
| `VISION_CONCURRENCY` | `12` | Parallel GPT-4o vision calls during ingest |
| `OUTPUT_RESOLUTION` | `1920x1080` | Output video resolution |
| `OUTPUT_FPS` | `30` | Output frame rate |

## Key Behaviours

| Property | Behaviour |
|---|---|
| **Clip reuse** | Each 4-second clip can be used in multiple *different* videos |
| **No within-video repeats** | A clip used in a video cannot appear again in the same video |
| **Grouping** | Clips always appear in groups of 3-5 consecutive clips |
| **Idempotent ingest** | Re-running ingest on the same file is a safe no-op |
| **Library growth** | Add new b-roll at any time; it becomes available for all future videos |
