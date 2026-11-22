"""Durable single-worker queue and content-addressed Whisper results."""

from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
from threading import Event, Thread
import wave

from .completion import POLICY, check
from .text import digest
from .whisper import Interrupted

SCHEMA = """
CREATE TABLE IF NOT EXISTS qc_result (
    id TEXT PRIMARY KEY,
    audio_sha256 TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    language TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS qc_results_by_input ON qc_result(audio_sha256, text_sha256, language);
CREATE TABLE IF NOT EXISTS take_qc (
    take_id TEXT PRIMARY KEY REFERENCES voice_job(id),
    state TEXT NOT NULL,
    result_id TEXT REFERENCES qc_result(id),
    cached INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS qc_by_state ON take_qc(state);
"""
SETUP = "Для Whisper QC задайте пути к локальному Python и модели в настройках запуска."


def take_basis(conn, take):
    language = conn.execute("SELECT language FROM revision WHERE id=?", (take["revision_id"],)).fetchone()
    return take["result"]["asset"]["sha256"], digest(take["request"]["text"]), language["language"]


class QualityControl:
    def __init__(self, jobs, recognizer):
        self.jobs = jobs
        self.store = jobs.store
        self.recognizer = recognizer
        self.wake = Event()
        self.stopping = Event()
        self.thread = Thread(target=self.run, name="whisper-qc", daemon=True)

    def start(self):
        with closing(self.store.connect()) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("UPDATE take_qc SET state='queued' WHERE state='running'")
        self.thread.start()
        self.wake.set()

    def close(self):
        self.stopping.set()
        self.wake.set()
        self.thread.join()

    def enqueue(self, conn, take_id):
        state = "queued" if self.recognizer else "unavailable"
        conn.execute("""
            INSERT INTO take_qc(take_id,state,error) VALUES (?,?,?)
            ON CONFLICT(take_id) DO UPDATE SET state=excluded.state,error=excluded.error,cached=0
            WHERE take_qc.state NOT IN ('queued','running')
        """, (take_id, state, None if self.recognizer else SETUP))

    def submit(self, clip_id, take_id):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.jobs.takes.require_take(conn, clip_id, take_id)
            self.enqueue(conn, take_id)
            snapshot = self.snapshot(conn, take_id)
        self.wake.set()
        return snapshot

    @staticmethod
    def snapshot(conn, take_id):
        row = conn.execute("""
            SELECT q.state,q.cached,q.error,r.id,r.report FROM take_qc q
            LEFT JOIN qc_result r ON r.id=q.result_id WHERE q.take_id=?
        """, (take_id,)).fetchone()
        if row is None:
            return {"state": "unchecked", "cached": False, "error": None, "report": None}
        return {
            "state": row["state"], "cached": bool(row["cached"]), "error": row["error"],
            "report": {"id": row["id"], **json.loads(row["report"])} if row["report"] else None,
        }

    def history(self, clip_id, take_id):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN")
            take = self.jobs.takes.require_take(conn, clip_id, take_id)
            rows = conn.execute("""
                SELECT id,report FROM qc_result WHERE (audio_sha256,text_sha256,language)=(?,?,?)
                ORDER BY rowid DESC
            """, take_basis(conn, take))
            return {"current": self.snapshot(conn, take_id),
                    "reports": [{"id": row["id"], **json.loads(row["report"])} for row in rows]}

    def claim(self):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            queued = conn.execute("SELECT take_id FROM take_qc WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if queued is None:
                return None
            take_id = queued["take_id"]
            conn.execute("UPDATE take_qc SET state='running' WHERE take_id=?", (take_id,))
            return take_id

    def run(self):
        while not self.stopping.is_set():
            self.wake.wait()
            self.wake.clear()
            while not self.stopping.is_set():
                take_id = self.claim()
                if take_id is None:
                    break
                self.execute(take_id)
                self.jobs.recovery.advance(take_id)

    def execute(self, take_id):
        if self.recognizer is None:
            self.state(take_id, "unavailable", SETUP)
            return
        try:
            with closing(self.store.connect()) as conn:
                take = self.jobs.find(conn, take_id)
                audio_hash, text_hash, language = take_basis(conn, take)
            data = self.jobs.assets.read(audio_hash)
            identity = self.recognizer.identity(self.stopping.is_set)
            basis = {"audio_sha256": audio_hash, "text_sha256": text_hash, "language": language,
                     "recognizer": identity, "policy": POLICY}
            key = sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            with closing(self.store.connect()) as conn, conn:
                if conn.execute("SELECT 1 FROM qc_result WHERE id=?", (key,)).fetchone():
                    self.finish(conn, take_id, key, cached=True)
                    return
            expected = take["request"]["text"]
            transcript = self.recognizer.transcribe(self.jobs.assets.path(audio_hash), language, self.stopping.is_set)
            if self.recognizer.identity(self.stopping.is_set) != identity:
                raise RuntimeError("Модель Whisper изменилась во время проверки. Повторите запрос.")
            with wave.open(BytesIO(data), "rb") as wav:
                pcm = wav.readframes(wav.getnframes())
            report = {"basis": basis, "expected": expected, "transcript": asdict(transcript),
                      **asdict(check(expected, transcript, pcm))}
            with closing(self.store.connect()) as conn, conn:
                conn.execute("INSERT INTO qc_result VALUES (?,?,?,?,?)",
                             (key, audio_hash, text_hash, language, json.dumps(report, allow_nan=False)))
                self.finish(conn, take_id, key, cached=False)
        except Interrupted:
            self.state(take_id, "queued", None)
        except Exception as error:
            message = "WAV дубля недоступен." if isinstance(error, KeyError) else str(error)[:600]
            self.state(take_id, "failed", message or type(error).__name__)

    @staticmethod
    def finish(conn, take_id, key, cached):
        conn.execute("""
            UPDATE take_qc SET state='complete',result_id=?,cached=?,error=NULL WHERE take_id=?
        """, (key, cached, take_id))

    def state(self, take_id, state, error):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("UPDATE take_qc SET state=?,error=? WHERE take_id=?", (state, error, take_id))
