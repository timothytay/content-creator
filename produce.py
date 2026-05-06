"""
produce.py — Turn a voiceover MP3 into a fully scheduled b-roll video.

Steps
─────
  1. Speed up the voiceover by 1.1× (FFmpeg atempo)
  2. Transcribe with Whisper (word-level timestamps)
  3. Segment the transcript into topics (Claude)
  4. Schedule clip groups to cover the full duration (scheduler.py)
  5. Assemble the output video (assembler.py)
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

import openai
import whisper

import assembler
import config
import library
import scheduler


# ── Step 1: Speed up voiceover ────────────────────────────────────────────────

def speed_up_audio(input_path: Path, factor: float = config.VOICEOVER_SPEED) -> Path:
    out_path = input_path.with_stem(input_path.stem + f"_{factor}x")
    if out_path.exists():
        print(f"  Sped-up audio already exists: {out_path.name}")
        return out_path

    print(f"  Speeding up audio {factor}× → {out_path.name}")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter:a", f"atempo={factor}",
        str(out_path),
    ], check=True, capture_output=True)

    return out_path


# ── Step 2: Transcribe ────────────────────────────────────────────────────────

def transcribe(audio_path: Path) -> dict:
    """
    Run Whisper and return:
      {
        "text": "...",
        "duration": 123.4,
        "words": [{"word": "hello", "start": 0.0, "end": 0.4}, ...]
      }
    """
    print(f"  Transcribing with Whisper ({config.WHISPER_MODEL})...")
    model  = whisper.load_model(config.WHISPER_MODEL)
    result = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        verbose=False,
    )

    words = []
    for seg in result["segments"]:
        for w in seg.get("words", []):
            words.append({
                "word":  w["word"].strip(),
                "start": w["start"],
                "end":   w["end"],
            })

    duration = words[-1]["end"] if words else result["segments"][-1]["end"]
    print(f"  Transcript: {len(words)} words, {duration:.1f}s")

    return {
        "text":     result["text"],
        "duration": duration,
        "words":    words,
    }


# ── Step 3: Segment into topics ───────────────────────────────────────────────

TOPIC_SYSTEM = """You are a video editor segmenting a voiceover transcript into distinct visual topics.

For each topic segment respond with:
- label: 2-4 word descriptive label (e.g. "ocean plastic pollution")
- start: start time in seconds (float)
- end:   end time in seconds (float)
- keywords: 4-6 concrete visual search keywords for b-roll matching

Respond ONLY with valid JSON (no markdown):
{ "topics": [ {"label": "...", "start": 0.0, "end": 18.5, "keywords": ["..."]} ] }"""


def segment_topics(transcript: dict) -> list[dict]:
    print("  Segmenting transcript into topics with OpenAI...")

    client = openai.OpenAI()

    # Build a readable transcript with timestamps
    readable = " ".join(
        f"[{w['start']:.1f}s] {w['word']}" for w in transcript["words"]
    )

    user_msg = (
        f"Total duration: {transcript['duration']:.1f}s\n\n"
        f"Transcript with word timestamps:\n{readable}\n\n"
        "Segment this into distinct visual topics. Aim for segments of 15-40 seconds each."
    )

    response = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": TOPIC_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()
    parsed = json.loads(raw)

    topics = parsed["topics"]

    # Ensure topics cover the full duration
    if topics and topics[-1]["end"] < transcript["duration"]:
        topics[-1]["end"] = transcript["duration"]

    print(f"  Found {len(topics)} topic segment(s):")
    for t in topics:
        print(f"    [{t['start']:.1f}s – {t['end']:.1f}s]  {t['label']}")

    return topics


# ── Public entry point ────────────────────────────────────────────────────────

def run_produce(voiceover_path: str, output_path: str = "output.mp4"):
    library.init_db()

    stats = library.library_stats()
    if stats["tagged"] == 0:
        raise RuntimeError("Library has no tagged clips. Run `ingest` first.")

    print(f"\n── Produce: {Path(voiceover_path).name}  →  {output_path}")
    print(f"   Library: {stats['tagged']} tagged clips across {stats['sources']} sources\n")

    # 1. Speed up
    sped_up = speed_up_audio(Path(voiceover_path))

    # 2. Transcribe
    transcript = transcribe(sped_up)

    # 3. Topic segments
    topics = segment_topics(transcript)

    # 4. Schedule
    print("\n── Scheduling b-roll groups...")
    used_ids: set[str] = set()
    groups = scheduler.schedule(
        topic_windows=topics,
        word_timestamps=transcript["words"],
        total_duration=transcript["duration"],
        used_clip_ids=used_ids,
    )

    print(f"\n── Assembling {len(groups)} groups ({sum(len(g.clip_ids) for g in groups)} total clips)...")

    # 5. Assemble
    out = Path(output_path)
    assembler.assemble(
        schedule=groups,
        output_path=out,
        voiceover_path=sped_up,
        schedule_json_path=out.with_suffix(".schedule.json"),
    )

    print(f"\n✓ Complete.")
    print(f"  Video:    {out}")
    print(f"  Audio:    {sped_up}")
    print(f"  Schedule: {out.with_suffix('.schedule.json')}")
    print(f"  FCPXML:   {out.with_suffix('.fcpxml')}")
    print(f"\n  Sync the audio in your editor and replace blank gaps with AI face cam.")
