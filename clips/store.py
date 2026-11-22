"""SQLite repository for immutable clip revisions."""

from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .text import Edit, LEGACY_POLICY, POLICY, Span, digest, revise

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip (
    id TEXT PRIMARY KEY,
    current_revision TEXT REFERENCES revision(id),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clip(id),
    parent_id TEXT REFERENCES revision(id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    policy TEXT NOT NULL,
    language TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS span (
    revision_id TEXT NOT NULL REFERENCES revision(id),
    ordinal INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    segment_id TEXT,
    text_sha256 TEXT,
    PRIMARY KEY (revision_id, ordinal),
    UNIQUE (revision_id, segment_id)
);
"""
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
KEEP_VOICE = object()


class Conflict(Exception):
    def __init__(self, current: dict):
        self.current = current


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            if "voice_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(revision)")}:
                conn.execute("ALTER TABLE revision ADD COLUMN voice_id TEXT")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def list_clips(self) -> list[dict]:
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute("""
                SELECT c.id, r.title, c.updated_at FROM clip c
                JOIN revision r ON r.id = c.current_revision
                ORDER BY c.updated_at DESC, c.rowid DESC
            """)]

    def get(self, clip_id: str) -> dict:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN")
            return self.snapshot(conn, clip_id)

    @staticmethod
    def snapshot(conn: sqlite3.Connection, clip_id: str) -> dict:
        row = conn.execute("""
            SELECT c.id, c.updated_at, r.id AS revision, r.title, r.text,
                   r.text_sha256, r.policy, r.language, r.voice_id
            FROM clip c JOIN revision r ON r.id = c.current_revision WHERE c.id = ?
        """, (clip_id,)).fetchone()
        if row is None:
            raise KeyError(clip_id)
        spans = conn.execute("""
            SELECT ordinal, start, end, segment_id, text_sha256
            FROM span WHERE revision_id = ? ORDER BY ordinal
        """, (row["revision"],))
        return {**dict(row), "spans": [dict(span) for span in spans]}

    def create(self) -> dict:
        clip_id = uuid4().hex
        with closing(self.connect()) as conn, conn:
            conn.execute(f"INSERT INTO clip VALUES (?, NULL, {NOW})", (clip_id,))
            self.publish(conn, clip_id, None, "", "", [], POLICY, None)
            return self.snapshot(conn, clip_id)

    def update(self, clip_id: str, base: str, title: str | None, edits: list[Edit],
               voice_id=KEEP_VOICE) -> dict:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self.snapshot(conn, clip_id)
            if current["revision"] != base:
                raise Conflict(current)
            if voice_id is KEEP_VOICE:
                voice_id = current["voice_id"]
            elif voice_id is not None and not conn.execute(
                "SELECT 1 FROM voice WHERE id=?", (voice_id,)
            ).fetchone():
                raise ValueError("Сохранённый голос не найден.")
            new_title = current["title"] if title is None else title
            policy = current["policy"]
            text, spans = revise(current["text"], self.spans(current), edits, policy)
            if text == current["text"] and new_title == current["title"] and voice_id == current["voice_id"]:
                return current
            self.publish(conn, clip_id, base, new_title, text, spans, policy, voice_id)
            return self.snapshot(conn, clip_id)

    @staticmethod
    def spans(snapshot):
        return [Span(row["start"], row["end"], row["segment_id"]) for row in snapshot["spans"]]

    def upgrade_segmentation(self, clip_id: str, base: str) -> dict:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self.snapshot(conn, clip_id)
            if current["revision"] != base:
                raise Conflict(current)
            if current["policy"] != LEGACY_POLICY:
                return current
            text, spans = revise(current["text"], self.spans(current), [], POLICY)
            self.publish(conn, clip_id, base, current["title"], text, spans, POLICY, current["voice_id"])
            return self.snapshot(conn, clip_id)

    @staticmethod
    def publish(conn, clip_id, parent, title, text, spans, policy, voice_id):
        revision = uuid4().hex
        conn.execute("INSERT INTO revision VALUES (?, ?, ?, ?, ?, ?, ?, 'ru', ?)",
                     (revision, clip_id, parent, title, text, digest(text), policy, voice_id))
        conn.executemany("INSERT INTO span VALUES (?, ?, ?, ?, ?, ?)", [
            (revision, ordinal, span.start, span.end, span.segment_id,
             digest(text[span.start : span.end]) if span.segment_id else None)
            for ordinal, span in enumerate(spans)
        ])
        conn.execute(f"UPDATE clip SET current_revision = ?, updated_at = {NOW} WHERE id = ?",
                     (revision, clip_id))
