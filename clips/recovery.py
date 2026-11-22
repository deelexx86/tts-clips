"""Bounded recovery using the existing Vox and completion QC queues."""

from contextlib import closing
from io import BytesIO
import json
from secrets import randbelow
from uuid import uuid4
import wave

from .audio import RATE
from .completion import POLICY, edge_dbfs
from .store import Conflict
from .takes import MAX_SEED, CommandConflict, current_material, selection
from .text import SENTENCE, digest
from .vox import Cancelled

RULES = "books-recovery-v1"
GAP_FRAMES = RATE // 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_run (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clip(id),
    revision_id TEXT NOT NULL REFERENCES revision(id),
    command_key TEXT NOT NULL,
    command TEXT NOT NULL,
    UNIQUE (clip_id, command_key)
);
CREATE TABLE IF NOT EXISTS recovery_item (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recovery_run(id),
    source_id TEXT NOT NULL REFERENCES voice_job(id),
    baseline TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    outcome TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_source_recovery ON recovery_item(source_id)
    WHERE state='active';
CREATE TABLE IF NOT EXISTS recovery_attempt (
    item_id TEXT NOT NULL REFERENCES recovery_item(id),
    strategy TEXT NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES voice_job(id),
    assessment TEXT,
    PRIMARY KEY (item_id, strategy)
);
CREATE TABLE IF NOT EXISTS recovery_part (
    job_id TEXT NOT NULL REFERENCES voice_job(id),
    number INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (job_id, number)
);
"""


def split_at_sentence(text):
    # From TTS Books b553710. Only consider sentence edges in the middle 40%.
    boundaries = [m.end() for m in SENTENCE.finditer(text)
                  if 0.30 * len(text) <= m.end() <= 0.70 * len(text)]
    return min(boundaries, key=lambda index: abs(index - len(text) / 2), default=None)


def pcm(data):
    with wave.open(BytesIO(data), "rb") as source:
        return source.readframes(source.getnframes())


def assess(baseline, take, qc):
    """Books' coverage and stitching checks. Pace is a guard for composed takes."""
    report = qc["report"] if qc["state"] == "complete" else None
    result = take["result"]
    issues = []
    ratio = None
    if not result:
        issues.append("synthesis_failed")
    if not report:
        issues.append("qc_failed")
    else:
        if report["status"] != "verified":
            issues.append(report["status"])
        if (report["basis"]["recognizer"], report["basis"]["policy"]) != (
            baseline["recognizer"], baseline["policy"],
        ):
            issues.append("qc_changed")
        if (report["full_coverage"] + 0.06 < baseline["coverage"] or
                report["longest_discrepancy"] >= 3):
            issues.append("body_review")
    if result:
        ratio = result["asset"]["frames"] / baseline["frames"]
        lineage = result["recovery"]
        observed = [part["execution"].get("model") for part in lineage["parts"]]
        expected = baseline["model"]
        if (len(observed) > 1 and observed[1:] != observed[:-1]) or (
            expected is not None and any(model != expected for model in observed)
        ):
            issues.append("synthesis_changed")
        if lineage["strategy"] == "split":
            if not 0.8 <= ratio <= 1.30:
                issues.append("pace_review")
            if any(part["edge_dbfs"] > -32 for part in lineage["parts"][:-1]):
                issues.append("join_review")
    return {
        "policy": RULES, "usable": not issues, "reasons": issues,
        "duration_ratio": ratio, "qc_report": report["id"] if report else None,
        "qc_status": report["status"] if report else None, "error": take["error"] or qc["error"],
    }


def admission(conn, take_id, report_id):
    row = conn.execute("SELECT assessment FROM recovery_attempt WHERE job_id=?", (take_id,)).fetchone()
    if row is None:
        return True
    assessment = json.loads(row["assessment"]) if row["assessment"] else None
    return assessment is not None and assessment["usable"] and assessment["qc_report"] == report_id


class Recovery:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store

    def begin(self, clip_id, command):
        serialized = json.dumps(command, sort_keys=True)
        queued = []
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("""
                SELECT id, command FROM recovery_run WHERE clip_id=? AND command_key=?
            """, (clip_id, command["key"])).fetchone()
            if existing:
                if existing["command"] != serialized:
                    raise CommandConflict("Этот ключ уже использован для другой команды.")
                return self.run_snapshot(conn, existing["id"])
            clip = self.store.snapshot(conn, clip_id)
            if clip["revision"] != command["revision"]:
                raise Conflict(clip)
            if not self.jobs.qc.recognizer:
                raise ValueError("Настройте Whisper для восстановления.")
            if not clip["voice_id"]:
                raise ValueError("Выберите голос клипа.")
            if command["source_take"]:
                targets = [command["source_take"]]
            else:
                targets = [
                    selection(conn, (clip_id, span["segment_id"], span["text_sha256"], clip["voice_id"]))["current"]
                    for span in clip["spans"] if span["segment_id"]
                ]
            run_id = uuid4().hex
            conn.execute("INSERT INTO recovery_run VALUES (?,?,?,?,?)",
                         (run_id, clip_id, clip["revision"], command["key"], serialized))
            for take_id in filter(None, targets):
                source = self.jobs.takes.require_take(conn, clip_id, take_id)
                qc = self.jobs.qc.snapshot(conn, take_id)
                report = qc["report"]
                allowed = ("likely_defect", "suspect") if command["include_suspect"] else ("likely_defect",)
                valid = (current_material(conn, source) and qc["state"] == "complete" and report
                         and report["status"] in allowed and report["basis"]["policy"] == POLICY)
                if not valid:
                    if command["source_take"]:
                        raise ValueError("Дубль должен относиться к текущему сегменту и иметь подходящее замечание QC.")
                    continue
                if conn.execute("SELECT 1 FROM recovery_item WHERE source_id=? AND state='active'",
                                (take_id,)).fetchone():
                    if command["source_take"]:
                        raise CommandConflict("Восстановление этого дубля уже выполняется.")
                    continue
                baseline = {
                    "audio": source["result"]["asset"]["sha256"], "frames": source["result"]["asset"]["frames"],
                    "qc_report": report["id"], "qc_status": report["status"], "coverage": report["full_coverage"],
                    "recognizer": report["basis"]["recognizer"], "policy": report["basis"]["policy"],
                    "model": source["result"].get("model"),
                }
                item = uuid4().hex
                conn.execute("INSERT INTO recovery_item(id,run_id,source_id,baseline) VALUES (?,?,?,?)",
                             (item, run_id, take_id, json.dumps(baseline)))
                seed = (source["request"]["seed"] + 1 + randbelow(MAX_SEED)) % (MAX_SEED + 1)
                queued.append(self.schedule(conn, item, source, clip["revision"], "full", seed))
            response = self.run_snapshot(conn, run_id)
        for job_id in queued:
            self.jobs.queue.put(job_id)
        return response

    def schedule(self, conn, item, source, revision, strategy, seed, cut=None):
        lineage = {"item_id": item, "source_take": source["id"], "strategy": strategy}
        if cut is not None:
            lineage["cut"] = cut
        payload = {key: source["request"][key] for key in ("text", "description", "reference", "mode")}
        payload.update(seed=seed, recovery=lineage)
        job = self.jobs.new(conn, "segment", payload, source["clip_id"], revision,
                            source["segment_id"], source["text_hash"], source["voice_id"])
        conn.execute("INSERT INTO recovery_attempt(item_id,strategy,job_id) VALUES (?,?,?)",
                     (item, strategy, job["id"]))
        return job["id"]

    def advance(self, job_id):
        queued = None
        with closing(self.store.connect()) as conn, conn:
            if not conn.execute("SELECT 1 FROM recovery_attempt WHERE job_id=?", (job_id,)).fetchone():
                return
            conn.execute("BEGIN IMMEDIATE")
            entry = conn.execute("""
                SELECT a.strategy,a.assessment,i.* FROM recovery_attempt a
                JOIN recovery_item i ON i.id=a.item_id WHERE a.job_id=?
            """, (job_id,)).fetchone()
            if entry is None or entry["state"] != "active" or entry["assessment"]:
                return
            take = self.jobs.find(conn, job_id)
            if take["state"] in ("queued", "running"):
                return
            if take["state"] in ("cancelled", "interrupted"):
                self.finish(conn, entry["id"], take["state"], take["state"])
                return
            qc = self.jobs.qc.snapshot(conn, job_id)
            if take["state"] == "ready" and qc["state"] in ("queued", "running"):
                return
            result = assess(json.loads(entry["baseline"]), take, qc)
            conn.execute("UPDATE recovery_attempt SET assessment=? WHERE job_id=?", (json.dumps(result), job_id))
            if not current_material(conn, take):
                self.finish(conn, entry["id"], "stale", "material_changed")
            elif result["usable"]:
                self.finish(conn, entry["id"], "recovered", "candidate_ready")
            elif entry["strategy"] == "split":
                self.finish(conn, entry["id"], "exhausted", "attempts_exhausted")
            else:
                cut = split_at_sentence(take["request"]["text"])
                if cut is None:
                    self.finish(conn, entry["id"], "exhausted", "no_sentence_boundary")
                else:
                    source = self.jobs.find(conn, entry["source_id"])
                    queued = self.schedule(conn, entry["id"], source, take["revision_id"],
                                           "split", take["request"]["seed"], cut)
        if queued is not None:
            self.jobs.queue.put(queued)

    @staticmethod
    def finish(conn, item, state, reason):
        conn.execute("UPDATE recovery_item SET state=?,outcome=? WHERE id=?", (state, reason, item))

    def resume(self):
        with closing(self.store.connect()) as conn:
            rows = conn.execute("""
                SELECT a.job_id,j.state FROM recovery_attempt a
                JOIN recovery_item i ON i.id=a.item_id JOIN voice_job j ON j.id=a.job_id
                WHERE i.state='active' AND a.assessment IS NULL ORDER BY a.rowid
            """).fetchall()
        for row in rows:
            if row["state"] == "queued":
                self.jobs.queue.put(row["job_id"])
            else:
                self.advance(row["job_id"])

    def cancel(self, run_id):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM recovery_run WHERE id=?", (run_id,)).fetchone():
                raise KeyError(run_id)
            conn.execute("""
                UPDATE voice_job SET state='cancelled',phase=NULL WHERE state IN ('queued','running')
                AND id IN (SELECT a.job_id FROM recovery_attempt a JOIN recovery_item i ON i.id=a.item_id
                           WHERE i.run_id=? AND i.state='active')
            """, (run_id,))
            conn.execute("UPDATE recovery_item SET state='cancelled',outcome='cancelled' WHERE run_id=? AND state='active'",
                         (run_id,))
            return self.run_snapshot(conn, run_id)

    def list(self, conn, clip_id):
        runs = conn.execute("SELECT id FROM recovery_run WHERE clip_id=? ORDER BY rowid DESC", (clip_id,))
        return [self.run_snapshot(conn, run["id"]) for run in runs]

    def run_snapshot(self, conn, run_id):
        run = dict(conn.execute("SELECT * FROM recovery_run WHERE id=?", (run_id,)).fetchone())
        items = []
        for source in conn.execute("SELECT * FROM recovery_item WHERE run_id=? ORDER BY rowid", (run_id,)):
            item = dict(source)
            item["baseline"] = json.loads(item["baseline"])
            item["text"] = self.jobs.find(conn, item["source_id"])["request"]["text"]
            item["attempts"] = []
            for row in conn.execute("SELECT * FROM recovery_attempt WHERE item_id=? ORDER BY rowid", (item["id"],)):
                attempt = dict(row)
                job = self.jobs.find(conn, attempt["job_id"])
                qc = self.jobs.qc.snapshot(conn, job["id"])
                attempt.update(state=job["state"], qc_state=qc["state"], phase=job["phase"], error=job["error"],
                               seed=job["request"]["seed"], segment_id=job["segment_id"],
                               assessment=json.loads(attempt["assessment"]) if attempt["assessment"] else None,
                               parts=[json.loads(part["record"]) for part in conn.execute(
                                   "SELECT record FROM recovery_part WHERE job_id=? ORDER BY number", (job["id"],)
                               )])
                item["attempts"].append(attempt)
            items.append(item)
        return {"id": run_id, "revision": run["revision_id"], "items": items}

    def synthesize(self, job, controls, reference, progress, cancelled):
        request = job["request"]["recovery"]
        text = controls["text"]
        offsets = [0, request["cut"], len(text)] if request["strategy"] == "split" else [0, len(text)]
        parts = []
        for number, (start, stop) in enumerate(zip(offsets, offsets[1:]), 1):
            phrase = text[start:stop]
            key = f"{job['id']}-{number}" if len(offsets) > 2 else job["id"]
            data, receipt = self.jobs.client.generate(
                key, {**controls, "text": phrase}, reference, progress, cancelled,
            )
            asset = self.jobs.assets.publish(data)
            part = {"number": number, "start": start, "end": stop, "text": phrase,
                    "text_sha256": digest(phrase), "asset": asset, "edge_dbfs": edge_dbfs(pcm(data)),
                    "execution": receipt}
            with closing(self.store.connect()) as conn, conn:
                conn.execute("INSERT INTO recovery_part VALUES (?,?,?)", (job["id"], number, json.dumps(part)))
            parts.append(part)
            if cancelled():
                raise Cancelled()
        if request["strategy"] == "split":
            output = BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                for index, part in enumerate(parts):
                    if index:
                        wav.writeframesraw(bytes(2 * GAP_FRAMES))
                    wav.writeframesraw(pcm(self.jobs.assets.read(part["asset"]["sha256"])))
            data = output.getvalue()
        return data, {
            **(parts[0]["execution"] if len(parts) == 1 else {}),
            "reused": all(part["execution"]["reused"] for part in parts),
            "model": parts[0]["execution"].get("model"),
            "recovery": {**request, "policy": RULES, "seed": controls["seed"],
                         "cfg": 2.0, "steps": 10, "gap_frames": GAP_FRAMES if len(parts) > 1 else 0,
                         "parts": parts},
        }
