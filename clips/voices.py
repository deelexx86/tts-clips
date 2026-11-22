"""Persistent voice previews and synthesis jobs."""

from contextlib import closing
from hashlib import sha256
import json
from queue import Queue
from threading import Event, Thread
from uuid import uuid4

from .audio import Assets, normalize_reference
from .qc import QualityControl
from .takes import Takes, choose_first, migrate
from .vox import Cancelled

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_job (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    clip_id TEXT REFERENCES clip(id),
    revision_id TEXT REFERENCES revision(id),
    segment_id TEXT,
    text_hash TEXT,
    voice_id TEXT REFERENCES voice(id),
    request TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    phase TEXT,
    result TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS voice (
    id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL UNIQUE REFERENCES voice_job(id),
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    reference TEXT NOT NULL
);
"""


class VoiceJobs:
    def __init__(self, store, client, recognizer=None):
        self.store = store
        self.takes = Takes(self)
        self.qc = QualityControl(self, recognizer)
        self.assets = Assets(store.path.with_suffix(".audio"))
        self.client = client
        self.queue = Queue()
        self.stopping = Event()
        self.thread = Thread(target=self.run, name="vox-jobs", daemon=True)

    def start(self):
        with closing(self.store.connect()) as conn, conn:
            conn.executescript(SCHEMA)
            migrate(conn)
            conn.execute("""
                UPDATE voice_job SET state='interrupted', phase=NULL
                WHERE state IN ('queued', 'running')
            """)
        self.qc.start()
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.queue.put(None)
        self.thread.join()
        self.qc.close()

    @staticmethod
    def find(conn, job_id):
        row = conn.execute("SELECT * FROM voice_job WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return VoiceJobs.decode(row)

    @staticmethod
    def decode(row):
        job = dict(row)
        job["request"] = json.loads(job["request"])
        job["result"] = json.loads(job["result"]) if job["result"] else None
        return job

    def get(self, job_id):
        with closing(self.store.connect()) as conn:
            return self.find(conn, job_id)

    def list(self, clip_id=None):
        with closing(self.store.connect()) as conn:
            if clip_id is None:
                rows = conn.execute("SELECT id FROM voice_job WHERE kind='preview' ORDER BY rowid DESC LIMIT 20")
            else:
                rows = conn.execute("""
                    SELECT id FROM voice_job WHERE clip_id=? ORDER BY rowid DESC
                """, (clip_id,))
            return [self.find(conn, row["id"]) for row in rows]

    def new(self, conn, kind, payload, clip_id=None, revision=None, segment=None, text_hash=None, voice_id=None):
        job = uuid4().hex
        conn.execute("""
            INSERT INTO voice_job(id,kind,clip_id,revision_id,segment_id,text_hash,voice_id,request)
            VALUES (?,?,?,?,?,?,?,?)
        """, (job, kind, clip_id, revision, segment, text_hash, voice_id, json.dumps(payload)))
        return self.find(conn, job)

    def preview(self, mode, phrase, description, seed, wav=None):
        payload = {"mode": mode, "text": phrase, "description": description, "seed": seed}
        if wav is not None:
            payload["upload_sha256"] = sha256(wav).hexdigest()
            payload["reference"] = self.assets.publish(normalize_reference(wav))
        with closing(self.store.connect()) as conn, conn:
            job = self.new(conn, "preview", payload)
        self.queue.put(job["id"])
        return job

    def cancel(self, job_id):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("""
                UPDATE voice_job SET state='cancelled', phase=NULL
                WHERE id=? AND state IN ('queued','running')
            """, (job_id,))
            return self.find(conn, job_id)

    def voices(self):
        with closing(self.store.connect()) as conn:
            rows = conn.execute("SELECT * FROM voice ORDER BY rowid DESC")
            return [{**dict(row), "reference": json.loads(row["reference"])} for row in rows]

    def pin(self, preview_id, name):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self.find(conn, preview_id)
            if job["kind"] != "preview" or job["state"] != "ready":
                raise ValueError("Дождитесь готовности голосовой пробы.")
            mode = job["request"]["mode"]
            reference = job["result"]["asset"] if mode == "design" else job["request"]["reference"]
            if reference["seconds"] > 30:
                raise ValueError("Проба длиннее 30 секунд. Используйте более короткий текст.")
            self.assets.read(reference["sha256"])
            conn.execute("""
                INSERT INTO voice VALUES (?,?,?,?,?) ON CONFLICT(preview_id) DO NOTHING
            """, (uuid4().hex, preview_id, name, mode, json.dumps(reference)))
            row = conn.execute("SELECT * FROM voice WHERE preview_id=?", (preview_id,)).fetchone()
            return {**dict(row), "reference": json.loads(row["reference"])}

    def change(self, job_id, state, *, prior="running", phase=None, result=None, error=None):
        check_queued = False
        with closing(self.store.connect()) as conn, conn:
            changed = conn.execute("""
                UPDATE voice_job SET state=?, phase=?, result=?, error=? WHERE id=? AND state=?
            """, (state, phase, json.dumps(result) if result is not None else None,
                  error, job_id, prior)).rowcount == 1
            if changed and state == "ready":
                job = self.find(conn, job_id)
                if job["kind"] == "segment":
                    choose_first(conn, job)
                    self.qc.enqueue(conn, job_id)
                    check_queued = True
        if check_queued:
            self.qc.wake.set()
        return changed

    def run(self):
        try:
            while not self.stopping.is_set():
                job_id = self.queue.get()
                if job_id is None or self.stopping.is_set():
                    break
                if not self.change(job_id, "running", prior="queued"):
                    continue
                self.execute(job_id)
        finally:
            self.client.close()

    def execute(self, job_id):
        def cancelled():
            return self.stopping.is_set() or self.get(job_id)["state"] != "running"

        try:
            job = self.get(job_id)
            request = job["request"]
            reference = self.assets.read(request["reference"]["sha256"]) if "reference" in request else None
            controls = {key: request[key] for key in ("text", "description", "seed")}
            data, receipt = self.client.generate(
                job_id, controls, reference,
                lambda phase: self.change(job_id, "running", phase=phase), cancelled,
            )
            if cancelled():
                raise Cancelled()
            asset = self.assets.publish(data)
            self.change(job_id, "ready", result={"asset": asset, **receipt})
        except Cancelled:
            self.change(job_id, "interrupted" if self.stopping.is_set() else "cancelled")
        except Exception as error:
            self.change(job_id, "failed", error=str(error)[:1000] or type(error).__name__)
