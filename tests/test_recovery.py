import io
import wave
from threading import Event
from time import monotonic, sleep

from fastapi.testclient import TestClient
import pytest

from clips.completion import Transcript
from clips.main import create_app
from clips.recovery import GAP_FRAMES, split_at_sentence
from clips.vox import Cancelled
from test_voice import ControlledVox, await_job, batch, qc_state, setup_clip


TEXT = "Утром мы встретили старого друга. Вечером приехала вся семья."
SINGLE = "Сегодня утром мы все вместе пошли гулять к реке."
FULL = "Утром мы встретили старого друга."
FIRST = TEXT[:split_at_sentence(TEXT)]
SECOND = TEXT[len(FIRST):]


def wav(value, frames):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 48000, frames, "NONE", "not compressed"))
        stream.writeframes(sample(value, frames))
    return output.getvalue()


def sample(value, frames):
    return value.to_bytes(2, "little", signed=True) * frames


class RecoveryVox(ControlledVox):
    def __init__(self, *, noisy=False, pause=False):
        super().__init__()
        self.noisy = noisy
        self.pause = pause
        self.calls = []

    def generate(self, key, payload, reference, progress, cancelled):
        text, seed = payload["text"], payload["seed"]
        self.calls.append((text, seed))
        if text in (TEXT, SINGLE):
            value = 100 if seed == 42 else 200
            return wav(value, 48000), {"reused": True}
        if text == FIRST:
            return wav(3000 if self.noisy else 300, 12000), {"reused": True}
        if text == SECOND:
            self.entered.set()
            while self.pause and not self.release.wait(0.02):
                if cancelled():
                    raise Cancelled()
            return wav(400, 12000), {"reused": True}
        return super().generate(key, payload, reference, progress, cancelled)


class RecoveryWhisper:
    def __init__(self, *, heals=False, suspect=False, verified=False):
        self.heals = heals
        self.suspect = suspect
        self.verified = verified

    def identity(self, stopping):
        return {"test": 1}

    def transcribe(self, path, language, stopping):
        with wave.open(str(path), "rb") as audio:
            value = int.from_bytes(audio.readframes(1), "little", signed=True)
        if self.verified or value in (300, 3000) or (value == 200 and self.heals):
            return Transcript(TEXT, TEXT)
        if self.suspect:
            return Transcript(TEXT.replace("семья", "группа"), TEXT.replace("семья", "группа"))
        return Transcript(FULL, FULL)


def prepared(client, text=TEXT):
    url, clip, segments = setup_clip(client, text)
    item = batch(client, url, clip, "original")["jobs"][0]
    source = await_job(client, item["id"])
    qc_state(client, url + "/takes/" + source["id"] + "/qc", "complete")
    return url, clip, source


def command(client, url, clip, key, source=None, include_suspect=False):
    return client.post(url + "/recoveries", json={
        "revision": clip["revision"], "key": key, "source_take": source,
        "include_suspect": include_suspect,
    })


def recovery_state(client, url, run_id, state):
    deadline = monotonic() + 6
    while monotonic() < deadline:
        workspace = client.get(url + "/takes").json()
        run = next(run for run in workspace["recoveries"] if run["id"] == run_id)
        item = run["items"][0]
        if item["state"] == state:
            return workspace, item
        sleep(0.025)
    pytest.fail(f"recovery did not reach {state}: {item}")


def test_split_recovery_has_exact_parts_and_exports_without_auto_select(tmp_path):
    db = tmp_path / "clips.sqlite3"
    voice = RecoveryVox()
    with TestClient(create_app(db, voice, RecoveryWhisper())) as client:
        url, clip, source = prepared(client)
        original = client.get("/api/audio/" + source["result"]["asset"]["sha256"]).content
        run = command(client, url, clip, "repair").json()
        assert command(client, url, clip, "repair", source["id"]).status_code == 409
        workspace, item = recovery_state(client, url, run["id"], "recovered")
        full, split = item["attempts"]
        assert full["strategy"] == "full" and "likely_defect" in full["assessment"]["reasons"]
        assert split["strategy"] == "split" and split["assessment"]["reasons"] == []
        assert len(voice.calls) == 5 and voice.calls[-1][1] == voice.calls[-2][1] == full["seed"] != 42
        assert command(client, url, clip, "repair").json()["id"] == run["id"]
        assert workspace["selections"][0]["current"] == source["id"]
        assert client.get("/api/audio/" + source["result"]["asset"]["sha256"]).content == original
        final = await_job(client, split["job_id"])
        assert final["request"]["text"] == TEXT and final["request"]["recovery"]["source_take"] == source["id"]
        recorded = final["result"]["recovery"]
        assert "".join(part["text"] for part in recorded["parts"]) == TEXT
        assert [(part["start"], part["end"]) for part in recorded["parts"]] == [
            (0, len(FIRST)), (len(FIRST), len(TEXT))
        ]
        assert all(client.get("/api/audio/" + part["asset"]["sha256"]).is_success for part in recorded["parts"])
        audio = client.get("/api/audio/" + final["result"]["asset"]["sha256"]).content
        with wave.open(io.BytesIO(audio), "rb") as inp:
            assert inp.readframes(inp.getnframes()) == sample(300, 12000) + bytes(GAP_FRAMES * 2) + sample(400, 12000)
        assert client.put(url + "/take-selection", json={"take_id": final["id"], "version": 1}).is_success
        options = {"revision": clip["revision"], "mode": "final"}
        plan = client.post(url + "/export-plan", json=options).json()
        assert plan["blockers"] == [] and plan["inputs"][0]["take_id"] == final["id"]
        assert client.post(url + "/exports", json={**options, "plan_id": plan["plan_id"]}).status_code == 201
        snapshot = client.get(url + "/takes").json()
    with TestClient(create_app(db, RecoveryVox(), RecoveryWhisper())) as reopened:
        assert reopened.get(url + "/takes").json() == snapshot


@pytest.mark.parametrize(("text", "heals", "noisy", "state", "reason"), [
    (TEXT, True, False, "recovered", "candidate_ready"),
    (SINGLE, False, False, "exhausted", "no_sentence_boundary"),
    (TEXT, False, True, "exhausted", "attempts_exhausted"),
])
def test_recovery_limits_and_stitch_guard(tmp_path, text, heals, noisy, state, reason):
    with TestClient(create_app(tmp_path / "app.sqlite3", RecoveryVox(noisy=noisy),
                               RecoveryWhisper(heals=heals))) as client:
        url, clip, source = prepared(client, text)
        run = command(client, url, clip, "repair", source["id"]).json()
        _, item = recovery_state(client, url, run["id"], state)
        assert item["outcome"] == reason
        assert len(item["attempts"]) == (2 if noisy else 1)
        if noisy:
            split = item["attempts"][-1]
            assert split["assessment"]["qc_status"] == "verified"
            assert split["assessment"]["reasons"] == ["join_review"]
            assert client.put(url + "/take-selection", json={"take_id": split["job_id"], "version": 1}).is_success
            payload = {"revision": clip["revision"], "mode": "final"}
            proposal = client.post(url + "/export-plan", json=payload).json()
            assert proposal["blockers"][0]["code"] == "recovery_review"
            approved = client.patch(url + "/takes/" + split["job_id"] + "/review",
                                    json={"listened": True, "decision": "accepted"})
            assert approved.is_success
            assert client.post(url + "/export-plan", json=payload).json()["blockers"] == []


@pytest.mark.parametrize("stop", ["cancel", "restart"])
def test_partial_split_survives_stop(tmp_path, stop):
    db = tmp_path / "clips.sqlite3"
    voice = RecoveryVox(pause=True)
    with TestClient(create_app(db, voice, RecoveryWhisper())) as client:
        url, clip, source = prepared(client)
        run = command(client, url, clip, "repair", source["id"]).json()
        assert voice.entered.wait(6)
        if stop == "cancel":
            assert client.post("/api/recoveries/" + run["id"] + "/cancel").is_success
    next_voice = RecoveryVox()
    with TestClient(create_app(db, next_voice, RecoveryWhisper())) as reopened:
        state = "cancelled" if stop == "cancel" else "interrupted"
        workspace, item = recovery_state(reopened, url, run["id"], state)
        full, split = item["attempts"]
        assert full["state"] == "ready" and len(split["parts"]) == 1
        assert reopened.get("/api/audio/" + split["parts"][0]["asset"]["sha256"]).is_success
        assert workspace["selections"][0]["current"] == source["id"]
        assert next_voice.calls == []


def test_suspect_requires_opt_in_and_verified_is_ineligible(tmp_path):
    for kind in ("suspect", "verified"):
        recognizer = RecoveryWhisper(suspect=kind == "suspect", verified=kind == "verified")
        with TestClient(create_app(tmp_path / (kind + ".sqlite3"), RecoveryVox(), recognizer)) as client:
            url, clip, source = prepared(client)
            assert command(client, url, clip, "batch").json()["items"] == []
            assert command(client, url, clip, "repair", source["id"]).status_code == 422
            explicit = command(client, url, clip, "allow", source["id"], True)
            assert explicit.status_code == (202 if kind == "suspect" else 422)


def test_recovery_queued_before_shutdown_resumes(tmp_path):
    db = tmp_path / "clips.sqlite3"
    voice = RecoveryVox()
    with TestClient(create_app(db, voice, RecoveryWhisper())) as client:
        url, clip, segments = setup_clip(client, TEXT + "\nHold.")
        first = batch(client, url, clip, "baseline", "selected", segments[:1])["jobs"][0]
        source = await_job(client, first["id"])
        qc_state(client, url + "/takes/" + source["id"] + "/qc", "complete")
        batch(client, url, clip, "hold", "selected", segments[1:])
        assert voice.entered.wait(6)
        run = command(client, url, clip, "queued-repair", source["id"]).json()
        assert run["items"][0]["attempts"][0]["state"] == "queued"
        attempt = run["items"][0]["attempts"][0]["job_id"]
    with TestClient(create_app(db, RecoveryVox(), RecoveryWhisper(heals=True))) as reopened:
        workspace, item = recovery_state(reopened, url, run["id"], "recovered")
        assert [part["job_id"] for part in item["attempts"]] == [attempt]
        assert workspace["selections"][0]["current"] == source["id"]
