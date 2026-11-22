import hashlib
import io
import sys
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
    job = response.json()
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
        job_url = clip_url + "/audio-jobs"
        payload = {"revision": ready["revision"], "segment_id": ready["spans"][2]["segment_id"]}
        assert client.post(job_url, json={**payload, "revision": draft["revision"]}).status_code == 409
        built = wait(client, client.post(job_url, json=payload))
        assert built["request"]["text"] == "Two."
        assert built["result"]["reference_sha256"] == key
        assert client.post(job_url, json=payload).json()["id"] == built["id"]
        response = client.get(f"/api/audio/{key}", headers={"Range": "bytes=0-43"})
        assert response.status_code == 206 and response.content == original[:44]
    with TestClient(create_app(database, VoxClient(fake_worker))) as reopened:
        assert reopened.get(clip_url).json() == ready
        assert reopened.get("/api/voices").json() == [second, first]
        assert reopened.get(job_url).json() == [built]
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
