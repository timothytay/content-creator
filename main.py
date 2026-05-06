"""
main.py — CLI entry point for the b-roll pipeline.

Usage
─────
  # Add b-roll source files to the persistent library (only done once per file):
  python main.py ingest footage/ocean.mp4 footage/wildlife/

  # Produce a video from a voiceover:
  python main.py produce voiceover.mp3 --output my_video.mp4

  # Show library stats:
  python main.py stats
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="broll",
        description="Agentic b-roll pipeline — ingest sources, produce videos.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── ingest ──────────────────────────────────────────────────────────────
    p_ingest = sub.add_parser(
        "ingest",
        help="Add video source files to the clip library (split + vision-tag).",
    )
    p_ingest.add_argument(
        "files", nargs="+",
        help="MP4 or MOV files, or directories containing them.",
    )

    # ── produce ─────────────────────────────────────────────────────────────
    p_produce = sub.add_parser(
        "produce",
        help="Generate a b-roll video from a voiceover MP3.",
    )
    p_produce.add_argument("voiceover", help="Input MP3 voiceover file.")
    p_produce.add_argument(
        "--output", default="output.mp4",
        help="Output MP4 path (default: output.mp4).",
    )

    # ── stats ────────────────────────────────────────────────────────────────
    sub.add_parser("stats", help="Show library statistics.")

    args = parser.parse_args()

    # ── Dispatch ─────────────────────────────────────────────────────────────
    if args.command == "ingest":
        from ingest import run_ingest
        run_ingest(args.files)

    elif args.command == "produce":
        from produce import run_produce
        run_produce(args.voiceover, args.output)

    elif args.command == "stats":
        import library
        library.init_db()
        s = library.library_stats()
        print(f"\nB-Roll Library")
        print(f"  Sources : {s['sources']}")
        print(f"  Clips   : {s['clips']}  ({s['tagged']} tagged, {s['clips']-s['tagged']} pending)")
        print(f"  DB path : {library.config.DB_PATH}\n")


if __name__ == "__main__":
    main()
