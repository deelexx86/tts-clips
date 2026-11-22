"""Validate a clip snapshot, assemble its PCM master and publish a durable export."""

from contextlib import closing
from datetime import datetime, timezone
from hashlib import file_digest, sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4
import wave

from .audio import RATE, wav_info
from .completion import POLICY
from . import pronunciation
from .recovery import admission
from .store import Conflict
from .takes import material, review, selection
from .text import digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_export (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clip(id),
    revision_id TEXT NOT NULL REFERENCES revision(id),
    manifest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS exports_by_clip ON clip_export(clip_id);
"""
MP3 = {"encoder": "libmp3lame", "bitrate_kbps": 128, "sample_rate": RATE, "channels": 1}


class Blocked(Exception):
    def __init__(self, plan):
        self.plan = plan


def clip_spans(clip):
    return [span for span in clip["spans"] if span["segment_id"]]


def segment_key(clip, span):
    return clip["id"], span["segment_id"], span["text_sha256"], clip["voice_id"]


def current_choices(conn, clip):
    choices = [selection(conn, segment_key(clip, span)) for span in clip_spans(clip)]
    return [(choice["current"], choice["version"]) for choice in choices]


def inspect_file(path):
    with path.open("rb") as file:
        checksum = file_digest(file, "sha256").hexdigest()
    return {"sha256": checksum, "bytes": path.stat().st_size}


def ffmpeg(*args, timeout=60):
    try:
        return subprocess.run(
            ["ffmpeg", "-nostdin", *args], capture_output=True, text=True, errors="replace",
            check=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except FileNotFoundError:
        raise RuntimeError("FFmpeg не найден. WAV-мастер доступен.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("Истекло время ожидания FFmpeg. WAV-мастер доступен.") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip().splitlines()
        raise RuntimeError("Ошибка FFmpeg. " + (detail[-1][:300] if detail else "WAV-мастер доступен.")) from None


def encode_mp3(master, output):
    version = ffmpeg("-version").splitlines()[0]
    ffmpeg("-v", "error", "-xerror", "-i", str(master), "-map_metadata", "-1",
           "-c:a", MP3["encoder"], "-b:a", str(MP3["bitrate_kbps"]) + "k",
           "-ar", str(RATE), "-ac", "1", str(output))
    ffmpeg("-v", "error", "-xerror", "-i", str(output), "-map", "0:a:0", "-f", "null", "-")
    if not output.stat().st_size:
        raise RuntimeError("FFmpeg вернул пустой MP3. WAV-мастер доступен.")
    return {**MP3, "ffmpeg": version, **inspect_file(output)}


class Exports:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store
        self.directory = self.store.path.with_suffix(".exports")

    def start(self):
        with closing(self.store.connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def capture(self, clip_id, revision):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN")
            clip = self.store.snapshot(conn, clip_id)
            if clip["revision"] != revision:
                raise Conflict(clip)
            inputs = []
            for number, span in enumerate(clip_spans(clip), 1):
                key = segment_key(clip, span)
                choice = selection(conn, key)
                take_id = choice["current"]
                location = {"number": number, "segment_id": span["segment_id"],
                            "text": clip["text"][span["start"]:span["end"]]}
                if not take_id:
                    inputs.append((location, None, None, None, "no_selection"))
                    continue
                try:
                    take = self.jobs.find(conn, take_id)
                except KeyError:
                    inputs.append((location, None, None, None, "unavailable"))
                    continue
                if (material(take) != key or take["kind"] != "segment" or
                        digest(take["request"]["text"]) != span["text_sha256"]):
                    inputs.append((location, take, None, None, "stale"))
                elif take["state"] != "ready" or not take["result"]:
                    inputs.append((location, take, None, None, "unavailable"))
                else:
                    location["selection_version"] = choice["version"]
                    qc = self.jobs.qc.snapshot(conn, take_id)
                    qc["recovery_approved"] = admission(conn, take_id, qc["report"]["id"] if qc["report"] else None)
                    inputs.append((location, take, review(conn, take_id), qc, None))
        return clip, inputs

    def pcm(self, asset):
        data = self.jobs.assets.read(asset["sha256"])
        info = wav_info(data)
        if (info["channels"], info["width"], info["rate"], info["frames"], len(data)) != (
            1, 2, RATE, asset["frames"], asset["bytes"]
        ):
            raise ValueError("Неверный WAV выбранного дубля.")
        return data

    def recognizer(self):
        recognizer = self.jobs.qc.recognizer
        if recognizer is not None:
            try:
                return recognizer.identity(lambda: False)
            except (OSError, ValueError, RuntimeError):
                pass
        return None

    @staticmethod
    def qc_admission(qc, take, clip, identity):
        report = qc["report"]
        if qc["state"] != "complete" or not report:
            return "qc_" + qc["state"]
        if report["status"] != "verified":
            return report["status"]
        if not qc["recovery_approved"]:
            return "recovery_review"
        expected = {
            "audio_sha256": take["result"]["asset"]["sha256"],
            "text_sha256": take["text_hash"], "language": clip["language"],
            "recognizer": identity, "policy": POLICY,
        }
        if identity is None or report["basis"] != expected:
            return "qc_stale"
        integrity = qc["pronunciation_integrity"]
        if not integrity or integrity["basis"]["policy"] != pronunciation.POLICY:
            return "pronunciation_unassessed"
        if integrity["status"] == "review":
            return "pronunciation_review"
        return "verified" if integrity["status"] == "clear" else "pronunciation_unassessed"

    def plan(self, clip_id, options):
        clip, captured = self.capture(clip_id, options["revision"])
        # Only successful automatic reports need a recognizer fingerprint. Manual approval
        # remains usable on a machine without the Whisper runtime.
        identity = self.recognizer() if any(
            qc and qc["state"] == "complete" and qc["report"] and qc["report"]["status"] == "verified"
            and not (decision["decision"] == "accepted" and decision["listened"])
            and decision["decision"] != "rejected" for _, _, decision, qc, _ in captured
        ) else None
        blockers, warnings, inputs = [], [], []
        if not captured:
            blockers.append({"code": "empty_clip"})
        for location, take, decision, qc, problem in captured:
            if problem:
                blockers.append({**location, "code": problem})
                continue
            if decision["decision"] == "rejected":
                blockers.append({**location, "code": "rejected"})
                continue
            asset = take["result"]["asset"]
            try:
                self.pcm(asset)
            except (KeyError, FileNotFoundError):
                blockers.append({**location, "code": "missing_wav"})
                continue
            except (ValueError, OSError):
                blockers.append({**location, "code": "invalid_wav"})
                continue
            approval = ("manual" if decision["decision"] == "accepted" and decision["listened"]
                        else self.qc_admission(qc, take, clip, identity))
            if approval not in ("manual", "verified"):
                messages = blockers if options["mode"] == "final" else warnings
                messages.append({**location, "code": approval})
            inputs.append({
                **location, "take_id": take["id"], "take_revision": take["revision_id"],
                "text_sha256": take["text_hash"], "asset": asset, "approval": approval,
                "review": decision, "qc_state": qc["state"],
                "qc_report": qc["report"]["id"] if qc["report"] else None,
                "qc_basis": qc["report"]["basis"] if qc["report"] else None,
                "pronunciation_integrity": qc["pronunciation_integrity"],
            })

        leading = options["leading_ms"] * (RATE // 1000)
        gap = options["gap_ms"] * (RATE // 1000)
        frames = sum(item["asset"]["frames"] for item in inputs) + leading + gap * max(0, len(inputs) - 1)
        if frames * 2 + 36 > 0xFFFFFFFF:
            blockers.append({"code": "wav_too_large"})
        plan = {
            **options, "clip_id": clip_id, "title": clip["title"], "voice_id": clip["voice_id"],
            "text_sha256": clip["text_sha256"], "language": clip["language"], "inputs": inputs,
            "blockers": blockers, "warnings": warnings, "leading_frames": leading, "gap_frames": gap,
            "frames": frames, "sample_rate": RATE, "seconds": frames / RATE if not blockers else None,
        }
        return {"plan_id": sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(), **plan}

    def assemble(self, clip_id, options, plan_id):
        plan = self.plan(clip_id, options)
        if plan["blockers"] or plan["plan_id"] != plan_id:
            raise Blocked(plan)
        self.directory.mkdir(parents=True, exist_ok=True)
        export_id = uuid4().hex
        with TemporaryDirectory(dir=self.directory, prefix=".staging-") as temporary:
            staging = Path(temporary) / "result"
            staging.mkdir()
            master = staging / "clip.wav"
            with wave.open(str(master), "wb") as output:
                output.setparams((1, 2, RATE, plan["frames"], "NONE", "not compressed"))
                output.writeframesraw(bytes(plan["leading_frames"] * 2))
                for index, item in enumerate(plan["inputs"]):
                    try:
                        data = self.pcm(item["asset"])
                    except (KeyError, OSError, ValueError):
                        item_location = {key: item[key] for key in ("segment_id", "number", "text")}
                        raise Blocked({**plan, "seconds": None, "blockers": [
                            {**item_location, "code": "invalid_wav"},
                        ]}) from None
                    if index:
                        output.writeframesraw(bytes(plan["gap_frames"] * 2))
                    with wave.open(BytesIO(data), "rb") as source:
                        output.writeframesraw(source.readframes(source.getnframes()))

            files = {"wav": {**inspect_file(master), "frames": plan["frames"], "sample_rate": RATE,
                             "channels": 1, "width": 2}}
            mp3_error = None
            if options["format"] == "mp3":
                mp3 = staging / "clip.mp3"
                try:
                    files["mp3"] = encode_mp3(master, mp3)
                except (OSError, RuntimeError) as error:
                    mp3_error = str(error)
                    mp3.unlink(missing_ok=True)
            manifest = {"id": export_id, "created_at": datetime.now(timezone.utc).isoformat(),
                        "plan": plan, "files": files, "mp3_error": mp3_error}
            for format in files:
                with (staging / ("clip." + format)).open("r+b") as file:
                    os.fsync(file.fileno())
            staging.rename(self.directory / export_id)
        with closing(self.store.connect()) as conn, conn:
            conn.execute("INSERT INTO clip_export VALUES (?,?,?,?)",
                         (export_id, clip_id, options["revision"], json.dumps(manifest)))
            clip = self.store.snapshot(conn, clip_id)
            return self.with_current(manifest, clip["revision"], current_choices(conn, clip))

    def list(self, clip_id):
        with closing(self.store.connect()) as conn, conn:
            conn.execute("BEGIN")
            clip = self.store.snapshot(conn, clip_id)
            vector = current_choices(conn, clip)
            rows = conn.execute("SELECT manifest FROM clip_export WHERE clip_id=? ORDER BY rowid DESC", (clip_id,))
            return [self.with_current(json.loads(row["manifest"]), clip["revision"], vector) for row in rows]

    @staticmethod
    def with_current(export, revision, vector):
        plan = export["plan"]
        inputs = [(item["take_id"], item["selection_version"]) for item in plan["inputs"]]
        return {**export, "current": plan["revision"] == revision and inputs == vector}

    def file(self, export_id, format):
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT manifest FROM clip_export WHERE id=?", (export_id,)).fetchone()
        if row is None:
            raise KeyError(export_id)
        export = json.loads(row["manifest"])
        metadata = export["files"].get(format)
        if metadata is None:
            raise KeyError(format)
        path = self.directory / export_id / ("clip." + format)
        if not path.is_file():
            raise KeyError(format)
        if inspect_file(path) != {key: metadata[key] for key in ("sha256", "bytes")}:
            raise ValueError("Файл экспорта повреждён.")
        return path
