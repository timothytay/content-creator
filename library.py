"""
library.py — SQLite-backed persistent clip library.

Schema
------
sources   : one row per ingested source MP4 file (keyed by SHA-256)
clips     : one row per 4-second segment extracted from a source
clip_tags : Claude vision output (description + tag list) for each clip
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Clip:
    id: str                    # "{source_id[:8]}__{index:04d}"
    source_id: str
    clip_path: str
    start_sec: float
    end_sec: float
    tagged: bool = False
    description: str = ""
    tags: list[str] = field(default_factory=list)


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
                tags        TEXT    -- JSON array
            );

            CREATE INDEX IF NOT EXISTS idx_clips_source   ON clips(source_id);
            CREATE INDEX IF NOT EXISTS idx_clips_tagged   ON clips(tagged);
        """)


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
            (clip.id, clip.source_id, clip.clip_path,
             clip.start_sec, clip.end_sec, int(clip.tagged)),
        )


def mark_clip_tagged(clip_id: str, description: str, tags: list[str]):
    with get_conn() as conn:
        conn.execute(
            "UPDATE clips SET tagged = 1 WHERE id = ?", (clip_id,)
        )
        conn.execute(
            """INSERT OR REPLACE INTO clip_tags (clip_id, description, tags)
               VALUES (?,?,?)""",
            (clip_id, description, json.dumps(tags)),
        )


def get_all_tagged_clips(exclude_ids: Optional[set[str]] = None) -> list[Clip]:
    """
    Return all tagged clips, optionally excluding a set of already-used IDs
    (used to prevent within-video repeats while allowing reuse across videos).
    """
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
        clips.append(Clip(
            id=row["id"],
            source_id=row["source_id"],
            clip_path=row["clip_path"],
            start_sec=row["start_sec"],
            end_sec=row["end_sec"],
            tagged=True,
            description=row["description"],
            tags=json.loads(row["tags"] or "[]"),
        ))

    return clips


def get_untagged_clips() -> list[Clip]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source_id, clip_path, start_sec, end_sec
               FROM clips WHERE tagged = 0"""
        ).fetchall()
    return [
        Clip(id=r["id"], source_id=r["source_id"], clip_path=r["clip_path"],
             start_sec=r["start_sec"], end_sec=r["end_sec"])
        for r in rows
    ]


def get_untagged_clips_for_source(source_id: str) -> list[Clip]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source_id, clip_path, start_sec, end_sec
               FROM clips WHERE tagged = 0 AND source_id = ?""",
            (source_id,),
        ).fetchall()
    return [
        Clip(id=r["id"], source_id=r["source_id"], clip_path=r["clip_path"],
             start_sec=r["start_sec"], end_sec=r["end_sec"])
        for r in rows
    ]


def library_stats() -> dict:
    with get_conn() as conn:
        n_sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        n_clips   = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        n_tagged  = conn.execute("SELECT COUNT(*) FROM clips WHERE tagged=1").fetchone()[0]
    return {"sources": n_sources, "clips": n_clips, "tagged": n_tagged}
