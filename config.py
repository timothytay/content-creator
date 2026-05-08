import os
from pathlib import Path

# ── Library paths ──────────────────────────────────────────────────────────────
LIBRARY_ROOT    = Path(os.getenv("BROLL_LIBRARY", "./broll_library"))
DB_PATH         = LIBRARY_ROOT / "clip_library.db"
CLIPS_DIR       = LIBRARY_ROOT / "clips"
FRAMES_DIR      = LIBRARY_ROOT / "frames"
BLANKS_DIR      = LIBRARY_ROOT / "blanks"

# ── Clip settings ──────────────────────────────────────────────────────────────
CLIP_DURATION   = 4          # seconds per clip
FRAMES_PER_CLIP = 3          # frames extracted for vision tagging

# ── Group / blank gap settings ─────────────────────────────────────────────────
GROUP_MIN_CLIPS = 3          # minimum clips per group
GROUP_MAX_CLIPS = 5          # maximum clips per group
GAP_MIN_SEC     = 4          # minimum blank gap after each group
GAP_MAX_SEC     = 8          # maximum blank gap after each group

# ── Voiceover ──────────────────────────────────────────────────────────────────
VOICEOVER_SPEED = 1.1        # playback speed multiplier

# ── Vision tagging ─────────────────────────────────────────────────────────────
VISION_CONCURRENCY  = 12     # parallel OpenAI vision calls during ingest
PREFILTER_TOP_N     = 15     # clips sent to OpenAI per group for final selection

# ── OpenAI models ───────────────────────────────────────────────────────────────
VISION_MODEL    = "gpt-5.4-mini"  # vision tagging during ingest ($0.75/$4.50 per MTok)
REASONING_MODEL = "gpt-5.4"       # topic segmentation + scheduling ($2.50/$15 per MTok)

# ── Video output ───────────────────────────────────────────────────────────────
OUTPUT_RESOLUTION = "1920x1080"
OUTPUT_FPS        = 30

