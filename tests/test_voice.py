import hashlib
import io
import sys
from threading import Event
from time import monotonic, sleep
import wave

import pytest
from fastapi.testclient import TestClient

from clips.main import create_app
from clips.vox import Cancelled, VoxClient


def tone():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x30\x00\xd0\xff" * 2400)
    return stream.getvalue()


@pytest.fixture
def fake_worker(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("""
import base64, hashlib, json, sys, time
data = bytes.fromhex(DATA)
def emit(**event):
    print(json.dumps(event), flush=True)
emit(event="hello", protocol=1)
for count, line in enumerate(sys.stdin):
    job = json.loads(line)
    key = job["id"]
    text = job["text"]
    if not count:
        emit(event="status", id=key, phase="loading")
    if text == "bad order":
        emit(event="status", id=key, phase="loading")
        continue
    emit(event="status", id=key, phase="synthesizing")
    if text == "hang":
        time.sleep(10)
    if text == "failure":
        emit(event="error", id=key, message="worker unavailable")
        break
    reference = job["reference"]
    emit(event="done", id="other" if text == "wrong id" else key, sha256=hashlib.sha256(data).hexdigest(),
         audio=base64.b64encode(data).decode(), reused=count > 0,
         effective_text=f"({job['description']}){text}" if job["description"] else text,
         reference_sha256=hashlib.sha256(base64.b64decode(reference)).hexdigest() if reference else None)
""".replace("DATA", repr(tone().hex())), encoding="utf-8")
    return [sys.executable, "-u", str(script)]


def wait(client, response, outcome="ready"):
    assert response.status_code == 202, response.text
    return await_job(client, response.json()["id"], outcome)


def await_job(client, job_id, outcome="ready"):
    job = client.get(f"/api/audio-jobs/{job_id}").json()
    deadline = monotonic() + 4
    while job["state"] in ("queued", "running") and monotonic() < deadline:
        sleep(0.015)
        job = client.get(f"/api/audio-jobs/{job['id']}").json()
    assert job["state"] == outcome, job
    return job


def test_design_clone_playback_and_restart(tmp_path, fake_worker):
    database = tmp_path / "clips.sqlite3"
    original = tone()
    key = hashlib.sha256(original).hexdigest()
    with TestClient(create_app(database, VoxClient(fake_worker))) as client:
        assert client.post("/api/previews/design", json={"text": "A", "description": "(bad)"}).status_code == 422
        assert client.post("/api/previews/clone?text=A", content=b"bad").status_code == 422
        design = wait(client, client.post("/api/previews/design", json={"text": "A", "description": "Calm"}))
        receipt = design["result"]
        assert receipt["effective_text"] == "(Calm)A"
        assert receipt["reference_sha256"] is None
        assert receipt["reused"] is False
        clone = wait(client, client.post("/api/previews/clone?text=B", content=original))
        assert clone["result"]["reference_sha256"] == key
        assert clone["result"]["reused"] is True
        assert clone["request"]["upload_sha256"] == key
        first = client.post("/api/voices", json={"preview_id": design["id"], "name": "Design"}).json()
        second = client.post("/api/voices", json={"preview_id": clone["id"], "name": "Clone"}).json()
        assert first["mode"] == "design" and second["mode"] == "clone"
        assert client.post("/api/voices", json={"preview_id": clone["id"], "name": "other"}).json() == second
        draft = client.post("/api/clips").json()
        clip_url = f"/api/clips/{draft['id']}"
        ready = client.patch(clip_url, json={
            "base_revision": draft["revision"], "voice_id": second["id"],
            "edits": [{"start": 0, "end": 0, "text": "One.\nTwo."}],
        }).json()
        job_url = clip_url + "/generations"
        payload = {"revision": ready["revision"], "segments": [ready["spans"][2]["segment_id"]],
                   "mode": "selected", "key": "initial"}
        assert client.post(job_url, json={**payload, "revision": draft["revision"]}).status_code == 409
        batch = client.post(job_url, json=payload).json()
        built = await_job(client, batch["jobs"][0]["id"])
        assert built["request"]["text"] == "Two."
        assert built["result"]["reference_sha256"] == key
        assert client.post(job_url, json=payload).json()["jobs"] == [built]
        response = client.get(f"/api/audio/{key}", headers={"Range": "bytes=0-43"})
        assert response.status_code == 206 and response.content == original[:44]
    with TestClient(create_app(database, VoxClient(fake_worker))) as reopened:
        assert reopened.get(clip_url).json() == ready
        assert reopened.get("/api/voices").json() == [second, first]
        assert reopened.get(clip_url + "/audio-jobs").json() == [built]
        assert reopened.get(f"/api/audio/{key}").content == original


def test_worker_failure_is_persisted_and_next_request_restarts(tmp_path, fake_worker):
    with TestClient(create_app(tmp_path / "clips.sqlite3", VoxClient(fake_worker))) as client:
        failed = wait(client, client.post("/api/previews/design", json={
            "text": "failure", "description": "Calm",
        }), "failed")
        assert failed["error"] == "worker unavailable"
        assert client.post("/api/voices", json={"preview_id": failed["id"], "name": "Bad"}).status_code == 422
        retried = wait(client, client.post("/api/previews/design", json={"text": "OK", "description": "Calm"}))
        assert retried["result"]["reused"] is False


@pytest.mark.parametrize(("text", "error"), [
    ("wrong id", "другого задания"), ("bad order", "порядок"), ("hang", "время ожидания"),
])
def test_worker_rejects_invalid_protocol_and_times_out(fake_worker, text, error):
    client = VoxClient(fake_worker, timeout=0.6)
    with pytest.raises((RuntimeError, ValueError), match=error):
        client.generate("id", {"text": text, "description": "", "seed": 42},
                        None, lambda _: None, lambda: False)
    assert client.process is None


def test_cancel_during_inference_closes_worker(fake_worker):
    client = VoxClient(fake_worker)
    state = []
    with pytest.raises(Cancelled):
        client.generate("id", {"text": "hang", "description": "", "seed": 42},
                        None, state.append, lambda: "synthesizing" in state)
    assert state[-1] == "synthesizing" and client.process is None


class ControlledVox:
    def __init__(self):
        self.entered = Event()
        self.release = Event()

    def generate(self, key, payload, reference, progress, cancelled):
        progress("synthesizing")
        if payload["text"] == "Hold.":
            self.entered.set()
            while not self.release.wait(0.02):
                if cancelled():
                    raise Cancelled()
        if payload["text"] == "Fail.":
            raise RuntimeError("test failure")
        return tone(), {"reused": True}

    def close(self):
        pass


def setup_clip(client, text):
    preview = wait(client, client.post("/api/previews/design", json={"description": "Calm", "text": "Probe"}))
    voice = client.post("/api/voices", json={"preview_id": preview["id"], "name": "Voice"}).json()
    clip = client.post("/api/clips").json()
    url = "/api/clips/" + clip["id"]
    ready = client.patch(url, json={
        "base_revision": clip["revision"], "voice_id": voice["id"],
        "edits": [{"start": 0, "end": 0, "text": text}],
    }).json()
    return url, ready, [span["segment_id"] for span in ready["spans"] if span["segment_id"]]


def batch(client, url, clip, key, mode="missing", segments=(), **extra):
    response = client.post(url + "/generations", json={
        "revision": clip["revision"], "key": key, "mode": mode, "segments": segments, **extra,
    })
    assert response.status_code == 202, response.text
    return response.json()


def test_doubles_review_and_idempotency_survive_restart(tmp_path, fake_worker):
    db = tmp_path / "clips.sqlite3"
    with TestClient(create_app(db, VoxClient(fake_worker))) as client:
        url, clip, targets = setup_clip(client, "One.\nTwo.\nThree.")
        initial = batch(client, url, clip, "initial")
        first = [await_job(client, j["id"]) for j in initial["jobs"]]
        choice_before = client.get(url + "/takes").json()["selections"]
        replay = batch(client, url, clip, "initial")
        assert replay["id"] == initial["id"] and replay["jobs"] == first
        assert client.post(url + "/generations", json={
            "revision": clip["revision"], "key": "initial", "mode": "selected", "segments": targets,
        }).status_code == 409
        assert batch(client, url, clip, "already-covered")["jobs"] == []
        sid = targets[1]
        base = first[1]
        variant = batch(client, url, clip, "v", "variant", [sid], source_take=base["id"])["jobs"][0]
        variant = await_job(client, variant["id"])
        repeat = batch(client, url, clip, "r", "repeat", [sid], source_take=base["id"])["jobs"][0]
        repeat = await_job(client, repeat["id"])
        assert variant["request"]["seed"] != base["request"]["seed"] == repeat["request"]["seed"]
        assert base["result"]["asset"]["sha256"] == repeat["result"]["asset"]["sha256"]
        assert len({base["id"], variant["id"], repeat["id"]}) == 3
        assert client.get(url + "/takes").json()["selections"] == choice_before

        selection_url = url + "/take-selection"
        choose = {"take_id": variant["id"], "version": 1}
        saved = client.put(selection_url, json=choose).json()
        assert saved == {"current": variant["id"], "previous": base["id"], "version": 2}
        assert client.put(selection_url, json=choose).json() == saved
        assert client.put(selection_url, json={"take_id": repeat["id"], "version": 1}).status_code == 409
        restored = client.put(selection_url, json={"take_id": base["id"], "version": 2}).json()
        assert restored["current"] == base["id"]

        review_url = url + "/takes/" + base["id"] + "/review"
        assert client.patch(review_url, json={"decision": "accepted"}).status_code == 422
        assert client.patch(review_url, json={"listened": True, "decision": "accepted", "note": "Лучший"}).status_code == 200
        assert client.patch(url + "/takes/" + repeat["id"] + "/review", json={"decision": "rejected"}).status_code == 200
        snapshot = client.get(url + "/takes").json()
        assert [c for c in snapshot["selections"] if c["segment_id"] != sid] == [
            c for c in choice_before if c["segment_id"] != sid
        ]
        assert client.get("/api/audio-jobs/" + base["id"]).json() == base
    with TestClient(create_app(db, VoxClient(fake_worker))) as reopened:
        assert reopened.get(url + "/takes").json() == snapshot
        assert batch(reopened, url, clip, "r", "repeat", [sid], source_take=base["id"])["jobs"] == [repeat]


def test_edit_during_synthesis_leaves_stale_take_and_keeps_other_choices(tmp_path):
    vox = ControlledVox()
    with TestClient(create_app(tmp_path / "clips.sqlite3", vox)) as client:
        url, clip, (lead, old, end) = setup_clip(client, "Lead.\nHold.\nEnd.")
        queued = batch(client, url, clip, "all")
        first, holding, last = queued["jobs"]
        assert vox.entered.wait(4)
        updated = client.patch(url, json={
            "base_revision": clip["revision"], "edits": [{"start": 6, "end": 10, "text": "New"}],
        }).json()
        vox.release.set()
        await_job(client, last["id"])
        workspace = client.get(url + "/takes").json()
        assert {c["segment_id"]: c["current"] for c in workspace["selections"]} == {
            lead: first["id"], end: last["id"]
        }
        assert await_job(client, holding["id"])["revision_id"] == clip["revision"]
        assert client.put(url + "/take-selection", json={"take_id": holding["id"], "version": 0}).status_code == 409
        missing = batch(client, url, updated, "missing")
        assert [j["request"]["text"] for j in missing["jobs"]] == ["New."]
        assert [j["segment_id"] for j in missing["jobs"]] != [old]


@pytest.mark.parametrize("stop", ["cancel", "restart", "failure"])
def test_partial_batch_preserves_completed_items(tmp_path, stop):
    db = tmp_path / "clips.sqlite3"
    vox = ControlledVox()
    with TestClient(create_app(db, vox)) as client:
        middle = "Fail." if stop == "failure" else "Hold."
        url, clip, targets = setup_clip(client, f"Lead.\n{middle}\nEnd.")
        initial = batch(client, url, clip, "all")
        lead, next_job, end = initial["jobs"]
        first = await_job(client, lead["id"])
        if stop == "failure":
            await_job(client, next_job["id"], "failed")
            await_job(client, end["id"])
        else:
            assert vox.entered.wait(4)
            if stop == "cancel":
                response = client.post("/api/generations/" + initial["id"] + "/cancel").json()
                assert [j["state"] for j in response["jobs"]] == ["ready", "cancelled", "cancelled"]
    with TestClient(create_app(db, ControlledVox())) as reopened:
        assert await_job(reopened, lead["id"]) == first
        if stop == "restart":
            for job in (next_job, end):
                await_job(reopened, job["id"], "interrupted")
        retry = batch(reopened, url, clip, "retry")
        assert [j["segment_id"] for j in retry["jobs"]] == (targets[1:2] if stop == "failure" else targets[1:])
