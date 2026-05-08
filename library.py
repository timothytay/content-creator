"""
library.py — SQLite-backed persistent clip library.

Schema
------
sources   : one row per ingested source MP4 file (keyed by SHA-256)
clips     : one row per 4-second segment extracted from a source
clip_tags : vision output (description + tag list) for each clip

Clip paths are stored relative to LIBRARY_ROOT so the library is portable.
"""

import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Clip:
    id: str
    source_id: str
    clip_path: str          # absolute path (resolved at read time)
    start_sec: float
    end_sec: float
    tagged: bool = False
    description: str = ""
    tags: list[str] = field(default_factory=list)


# ── Path helpers ───────────────────────────────────────────────────────────────

def _to_relative(path: str) -> str:
    """Convert an absolute clip path to one relative to LIBRARY_ROOT."""
    p = Path(path)
    if not p.is_absolute():
        return path
    try:
        return str(p.relative_to(config.LIBRARY_ROOT.resolve()))
    except ValueError:
        return path  # not under LIBRARY_ROOT — store as-is


def _to_absolute(path: str) -> str:
    """Resolve a stored relative path to an absolute path."""
    p = Path(path)
    if p.is_absolute():
        return path
    return str(config.LIBRARY_ROOT.resolve() / p)


# ── Connection helper ──────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Initialisation ─────────────────────────────────────────────────────────────

def init_db():
    config.LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    config.FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    config.BLANKS_DIR.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id          TEXT PRIMARY KEY,
                filename    TEXT NOT NULL,
                date_added  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration    REAL
            );

            CREATE TABLE IF NOT EXISTS clips (
                id          TEXT PRIMARY KEY,
                source_id   TEXT NOT NULL REFERENCES sources(id),
                clip_path   TEXT NOT NULL,
                start_sec   REAL NOT NULL,
                end_sec     REAL NOT NULL,
                tagged      INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS clip_tags (
                clip_id     TEXT PRIMARY KEY REFERENCES clips(id),
                description TEXT,
                tags        TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_clips_source   ON clips(source_id);
            CREATE INDEX IF NOT EXISTS idx_clips_tagged   ON clips(tagged);
        """)


# ── Migration ──────────────────────────────────────────────────────────────────

def migrate_to_relative_paths():
    """Convert any absolute clip_path values to paths relative to LIBRARY_ROOT."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, clip_path FROM clips").fetchall()
        updated = 0
        for row in rows:
            rel = _to_relative(row["clip_path"])
            if rel != row["clip_path"]:
                conn.execute("UPDATE clips SET clip_path = ? WHERE id = ?", (rel, row["id"]))
                updated += 1
    if updated:
        print(f"  Migrated {updated} clip paths to relative format.")


# ── Sources ────────────────────────────────────────────────────────────────────

def source_exists(source_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    return row is not None


def insert_source(source_id: str, filename: str, duration: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sources (id, filename, duration) VALUES (?,?,?)",
            (source_id, filename, duration),
        )


# ── Clips ──────────────────────────────────────────────────────────────────────

def insert_clip(clip: Clip):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO clips (id, source_id, clip_path, start_sec, end_sec, tagged)
               VALUES (?,?,?,?,?,?)""",
            (clip.id, clip.source_id, _to_relative(clip.clip_path),
             clip.start_sec, clip.end_sec, int(clip.tagged)),
        )


def mark_clip_tagged(clip_id: str, description: str, tags: list[str]):
    with get_conn() as conn:
        conn.execute("UPDATE clips SET tagged = 1 WHERE id = ?", (clip_id,))
        conn.execute(
            """INSERT OR REPLACE INTO clip_tags (clip_id, description, tags)
               VALUES (?,?,?)""",
            (clip_id, description, json.dumps(tags)),
        )


def _row_to_clip(row) -> Clip:
    return Clip(
        id=row["id"],
        source_id=row["source_id"],
        clip_path=_to_absolute(row["clip_path"]),
        start_sec=row["start_sec"],
        end_sec=row["end_sec"],
    )


def get_all_tagged_clips(exclude_ids: Optional[set[str]] = None) -> list[Clip]:
    exclude_ids = exclude_ids or set()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.source_id, c.clip_path, c.start_sec, c.end_sec,
                      ct.description, ct.tags
               FROM clips c
               JOIN clip_tags ct ON c.id = ct.clip_id
               WHERE c.tagged = 1"""
        ).fetchall()

    clips = []
    for row in rows:
        if row["id"] in exclude_ids:
            continue
        c = _row_to_clip(row)
        c.tagged      = True
        c.description = row["description"]
        c.tags        = json.loads(row["tags"] or "[]")
        clips.append(c)
    return clips


def get_untagged_clips() -> list[Clip]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source_id, clip_path, start_sec, end_sec FROM clips WHERE tagged = 0"
        ).fetchall()
    return [_row_to_clip(r) for r in rows]


def get_untagged_clips_for_source(source_id: str) -> list[Clip]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source_id, clip_path, start_sec, end_sec
               FROM clips WHERE tagged = 0 AND source_id = ?""",
            (source_id,),
        ).fetchall()
    return [_row_to_clip(r) for r in rows]


def library_stats() -> dict:
    with get_conn() as conn:
        n_sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        n_clips   = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        n_tagged  = conn.execute("SELECT COUNT(*) FROM clips WHERE tagged=1").fetchone()[0]
    return {"sources": n_sources, "clips": n_clips, "tagged": n_tagged}


# ── Export ─────────────────────────────────────────────────────────────────────

def export_library(dest_zip: Path):
    """
    Zip the database and all clip files into a portable archive.
    Paths in the DB are relative so the zip works on any machine.
    """
    migrate_to_relative_paths()

    total = sum(1 for _ in config.CLIPS_DIR.rglob("*.mp4"))
    print(f"  Packing {total} clips + database...")

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.write(config.DB_PATH, "clip_library.db")
        for clip_file in sorted(config.CLIPS_DIR.rglob("*.mp4")):
            zf.write(clip_file, clip_file.relative_to(config.LIBRARY_ROOT))

    size_mb = dest_zip.stat().st_size / 1_000_000
    print(f"  Exported → {dest_zip}  ({size_mb:.0f} MB)")


# ── Import ─────────────────────────────────────────────────────────────────────

def _merge_db(source_db: Path):
    """Merge sources, clips, and clip_tags from source_db into the current library."""
    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    with get_conn() as dst:
        for row in src.execute("SELECT * FROM sources"):
            dst.execute(
                "INSERT OR IGNORE INTO sources (id, filename, date_added, duration) VALUES (?,?,?,?)",
                (row["id"], row["filename"], row["date_added"], row["duration"]),
            )
        for row in src.execute("SELECT * FROM clips"):
            dst.execute(
                """INSERT OR IGNORE INTO clips
                   (id, source_id, clip_path, start_sec, end_sec, tagged)
                   VALUES (?,?,?,?,?,?)""",
                (row["id"], row["source_id"], row["clip_path"],
                 row["start_sec"], row["end_sec"], row["tagged"]),
            )
        for row in src.execute("SELECT * FROM clip_tags"):
            dst.execute(
                "INSERT OR IGNORE INTO clip_tags (clip_id, description, tags) VALUES (?,?,?)",
                (row["clip_id"], row["description"], row["tags"]),
            )
    src.close()


def import_library(zip_path: Path):
    """
    Import a library zip, merging clips and tags into the current library.
    Existing clips are never overwritten.
    """
    init_db()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        print("  Extracting archive...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp)

        # Copy new clip files
        tmp_clips = tmp / "clips"
        copied = 0
        if tmp_clips.exists():
            for clip_file in sorted(tmp_clips.rglob("*.mp4")):
                dest = config.CLIPS_DIR / clip_file.relative_to(tmp_clips)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(clip_file, dest)
                    copied += 1
        print(f"  Copied {copied} new clip files.")

        # Merge database
        tmp_db = tmp / "clip_library.db"
        if tmp_db.exists():
            _merge_db(tmp_db)
            print("  Database merged.")

    s = library_stats()
    print(f"  Library: {s['tagged']}/{s['clips']} clips tagged across {s['sources']} sources.")
