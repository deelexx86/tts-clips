"""Persistent generation batches, takes and independent listening decisions."""

from contextlib import closing
import json
from secrets import randbelow
from uuid import uuid4

from .speech_ready import require_preparation
from .store import Conflict

MAX_SEED = 2**31 - 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS generation (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clip(id),
    command_key TEXT NOT NULL,
    command TEXT NOT NULL,
    UNIQUE (clip_id, command_key)
);
CREATE TABLE IF NOT EXISTS generation_item (
    job_id TEXT PRIMARY KEY REFERENCES voice_job(id),
    generation_id TEXT NOT NULL REFERENCES generation(id)
);
CREATE INDEX IF NOT EXISTS generation_items ON generation_item(generation_id);
CREATE TABLE IF NOT EXISTS take_review (
    take_id TEXT PRIMARY KEY REFERENCES voice_job(id),
    listened INTEGER NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS take_selection (
    clip_id TEXT NOT NULL REFERENCES clip(id),
    segment_id TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    voice_id TEXT NOT NULL REFERENCES voice(id),
    current TEXT NOT NULL REFERENCES voice_job(id),
    previous TEXT REFERENCES voice_job(id),
    version INTEGER NOT NULL,
    PRIMARY KEY (clip_id, segment_id, text_hash, voice_id)
);
"""


class CommandConflict(Exception):
    pass


def migrate(conn):
    conn.executescript(SCHEMA)
    # C1.2 exposed its sole successful result directly. Preserve it as the initial choice.
    conn.execute("""
        INSERT OR IGNORE INTO take_selection
        SELECT j.clip_id,j.segment_id,j.text_hash,j.voice_id,j.id,NULL,1 FROM voice_job j
        WHERE j.kind='segment' AND j.state='ready'
          AND NOT EXISTS (SELECT 1 FROM generation_item i WHERE i.job_id=j.id)
          AND NOT EXISTS (SELECT 1 FROM recovery_attempt a WHERE a.job_id=j.id)
        ORDER BY j.rowid DESC
    """)


def material(job):
    return job["clip_id"], job["segment_id"], job["text_hash"], job["voice_id"]


def selection(conn, key):
    row = conn.execute("""
        SELECT current,previous,version FROM take_selection
        WHERE (clip_id,segment_id,text_hash,voice_id)=(?,?,?,?)
    """, key).fetchone()
    return dict(row) if row else {"current": None, "previous": None, "version": 0}


def review(conn, take_id):
    row = conn.execute("SELECT listened,decision,note FROM take_review WHERE take_id=?", (take_id,)).fetchone()
    return {**dict(row), "listened": bool(row["listened"])} if row else {
        "listened": False, "decision": "pending", "note": "",
    }


def current_material(conn, job):
    return conn.execute("""
        SELECT 1 FROM clip c JOIN revision r ON r.id=c.current_revision
        JOIN span s ON s.revision_id=r.id
        WHERE c.id=? AND s.segment_id=? AND s.text_sha256=? AND r.voice_id=?
    """, material(job)).fetchone() is not None


def choose_first(conn, job):
    if current_material(conn, job):
        conn.execute("INSERT OR IGNORE INTO take_selection VALUES (?,?,?,?,?,NULL,1)",
                     (*material(job), job["id"]))


class Takes:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store

    def workspace(self, clip_id):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN")
            self.store.snapshot(conn, clip_id)
            rows = conn.execute("""
                SELECT j.*,i.generation_id FROM voice_job j
                LEFT JOIN generation_item i ON i.job_id=j.id
                WHERE j.clip_id=? ORDER BY j.rowid DESC
            """, (clip_id,))
            jobs = []
            for row in rows:
                job = self.jobs.decode(row)
                # The full worker receipt remains available on the individual job.
                if job["result"]:
                    job["result"] = {k: job["result"][k] for k in ("asset", "reused")}
                    job["review"] = review(conn, job["id"])
                    job["qc"] = self.jobs.qc.snapshot(conn, job["id"])
                jobs.append(job)
            selections = conn.execute("SELECT * FROM take_selection WHERE clip_id=?", (clip_id,))
            return {"jobs": jobs, "selections": [dict(row) for row in selections],
                    "recoveries": self.jobs.recovery.list(conn, clip_id)}

    def generate(self, clip_id, command):
        serialized = json.dumps(command, sort_keys=True)
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("""
                SELECT id,command FROM generation WHERE clip_id=? AND command_key=?
            """, (clip_id, command["key"])).fetchone()
            if existing:
                if existing["command"] != serialized:
                    raise CommandConflict("Этот ключ уже использован для другой команды.")
                return self.batch(conn, existing["id"])
            clip = self.store.snapshot(conn, clip_id)
            if clip["revision"] != command["revision"]:
                raise Conflict(clip)
            voice = conn.execute("SELECT * FROM voice WHERE id=?", (clip["voice_id"],)).fetchone()
            if voice is None:
                raise ValueError("Выберите сохранённый голос.")
            spans = {row["segment_id"]: row for row in clip["spans"] if row["segment_id"]}
            targets = command["segments"]
            if len(set(targets)) != len(targets) or not set(targets) <= spans.keys():
                raise ValueError("Укажите разные сегменты текущей редакции.")
            selected_ids = set(targets)
            targets = [sid for sid in spans if sid in selected_ids]
            mode = command["mode"]
            source_id = command["source_take"]
            if mode == "missing":
                if targets or source_id:
                    raise ValueError("Команда недостающих сегментов сама определяет цели.")
                targets = [
                    sid for sid, span in spans.items()
                    if not self.covered(conn, (clip_id, sid, span["text_sha256"], voice["id"]))
                ]
            if mode != "missing" and not targets:
                raise ValueError("Выберите хотя бы один сегмент.")
            if (mode in ("repeat", "variant")) != bool(source_id):
                raise ValueError("Для повтора или варианта укажите исходный дубль.")
            source = self.jobs.find(conn, source_id) if source_id else None
            if source and (
                source["kind"] != "segment" or source["state"] != "ready" or
                len(targets) != 1 or material(source) != (
                    clip_id, targets[0], spans[targets[0]]["text_sha256"], voice["id"]
                )
            ):
                raise ValueError("Исходный дубль должен относиться к выбранному сегменту и голосу.")
            require_preparation(clip, [spans[sid] for sid in targets], command.get("speech_confirmation"))
            base = self.jobs.find(conn, voice["preview_id"])["request"]
            batch = uuid4().hex
            conn.execute("INSERT INTO generation VALUES (?,?,?,?)", (batch, clip_id, command["key"], serialized))
            ids = []
            for sid in targets:
                span = spans[sid]
                seed = source["request"]["seed"] if source else base["seed"]
                if mode == "variant":
                    seed = (seed + 1 + randbelow(MAX_SEED)) % (MAX_SEED + 1)
                payload = {
                    "mode": "clone", "text": clip["text"][span["start"]:span["end"]],
                    "description": "", "seed": seed, "reference": json.loads(voice["reference"]),
                }
                if source:
                    payload["source_take"] = source_id
                job = self.jobs.new(conn, "segment", payload, clip_id, clip["revision"],
                                    sid, span["text_sha256"], voice["id"])
                conn.execute("INSERT INTO generation_item VALUES (?,?)", (job["id"], batch))
                ids.append(job["id"])
            response = self.batch(conn, batch)
        for job_id in ids:
            self.jobs.queue.put(job_id)
        return response

    def covered(self, conn, key):
        selected = selection(conn, key)["current"]
        if selected and review(conn, selected)["decision"] != "rejected":
            asset = self.jobs.find(conn, selected)["result"]["asset"]
            try:
                self.jobs.assets.path(asset["sha256"])
                return True
            except KeyError:
                pass
        return conn.execute("""
            SELECT 1 FROM voice_job WHERE (clip_id,segment_id,text_hash,voice_id)=(?,?,?,?)
            AND state IN ('queued','running')
        """, key).fetchone() is not None

    def batch(self, conn, batch):
        rows = conn.execute("""
            SELECT j.* FROM generation_item i JOIN voice_job j ON j.id=i.job_id
            WHERE i.generation_id=? ORDER BY j.rowid
        """, (batch,))
        return {"id": batch, "jobs": [self.jobs.decode(row) for row in rows]}

    def cancel(self, batch):
        with closing(self.store.connect()) as conn, conn:
            if not conn.execute("SELECT 1 FROM generation WHERE id=?", (batch,)).fetchone():
                raise KeyError(batch)
            conn.execute("""
                UPDATE voice_job SET state='cancelled',phase=NULL
                WHERE state IN ('queued','running') AND id IN
                    (SELECT job_id FROM generation_item WHERE generation_id=?)
            """, (batch,))
            return self.batch(conn, batch)

    def select(self, clip_id, take_id, version):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self.require_take(conn, clip_id, take_id)
            key = material(job)
            old = selection(conn, key)
            if not current_material(conn, job):
                raise CommandConflict("Дубль относится к другой редакции текста или голосу.")
            if old["current"] == take_id:
                return old
            if old["version"] != version:
                raise CommandConflict("Выбор уже изменился. Обновите список дублей.")
            self.jobs.assets.path(job["result"]["asset"]["sha256"])
            conn.execute("""
                INSERT INTO take_selection VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(clip_id,segment_id,text_hash,voice_id)
                DO UPDATE SET current=excluded.current,previous=excluded.previous,version=excluded.version
            """, (*key, take_id, old["current"], version + 1))
            return selection(conn, key)

    def review(self, clip_id, take_id, fields):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.require_take(conn, clip_id, take_id)
            updated = {**review(conn, take_id), **fields}
            if updated["decision"] == "accepted" and not updated["listened"]:
                raise ValueError("Отметьте прослушивание перед принятием дубля.")
            conn.execute("""
                INSERT INTO take_review VALUES (?,?,?,?)
                ON CONFLICT(take_id) DO UPDATE SET
                listened=excluded.listened,decision=excluded.decision,note=excluded.note
            """, (take_id, updated["listened"], updated["decision"], updated["note"]))
            return updated

    def require_take(self, conn, clip_id, take_id):
        job = self.jobs.find(conn, take_id)
        if job["kind"] != "segment" or job["state"] != "ready" or job["clip_id"] != clip_id:
            raise KeyError(take_id)
        return job
