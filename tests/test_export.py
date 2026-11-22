import hashlib
import io
import shutil
import sqlite3
import subprocess
import wave

from fastapi.testclient import TestClient
import pytest

from clips.completion import Transcript
from clips.main import create_app
from test_voice import ControlledVox, await_job, batch, setup_clip, qc_state


PHRASES = {"One.": (120, 960), "Two.": (230, 1440), "Three.": (-314, 1920)}


def samples(value, frames):
    return value.to_bytes(2, "little", signed=True) * frames


class ExportVoice(ControlledVox):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate(self, key, payload, reference, progress, cancelled):
        self.calls += 1
        if payload["text"] not in PHRASES:
            return super().generate(key, payload, reference, progress, cancelled)
        value, frames = PHRASES[payload["text"]]
        stream = io.BytesIO()
        with wave.open(stream, "wb") as out:
            out.setparams((1, 2, 48000, frames, "NONE", "not compressed"))
            out.writeframes(samples(value, frames))
        return stream.getvalue(), {"reused": True}


class ExportWhisper:
    version = "test-1"
    calls = 0

    def identity(self, stopping):
        return {"model": self.version}

    def transcribe(self, path, language, stopping):
        self.calls += 1
        with wave.open(str(path), "rb") as inp:
            frames = inp.getnframes()
        phrase = next(text for text, (_, count) in PHRASES.items() if count == frames)
        return Transcript(phrase, phrase)


def prepare(client, text="One.\nTwo.\nThree."):
    url, clip, segment_ids = setup_clip(client, text)
    ready = [await_job(client, item["id"]) for item in batch(client, url, clip, "first")["jobs"]]
    return url, clip, segment_ids, ready


def plan(client, url, clip, **changes):
    options = dict(revision=clip["revision"], mode="final", format="wav", leading_ms=23, gap_ms=157)
    options.update(changes)
    response = client.post(url + "/export-plan", json=options)
    assert response.status_code == 200, response.text
    return options, response.json()


def publish(client, url, options, proposal):
    response = client.post(url + "/exports", json={**options, "plan_id": proposal["plan_id"]})
    assert response.status_code == 201, response.text
    return response.json()


def status_codes(proposal, bucket):
    return [item["code"] for item in proposal[bucket]]


def approve(client, url, job, **override):
    updated = client.patch(url + "/takes/" + job["id"] + "/review",
                           json={"listened": True, "decision": "accepted", **override})
    assert updated.status_code == 200


def test_exact_master_snapshots_and_restart(tmp_path):
    voice = ExportVoice()
    db = tmp_path / "clips.sqlite3"
    with TestClient(create_app(db, voice)) as client:
        url, clip, segments, takes = prepare(client)
        assert status_codes(plan(client, url, clip)[1], "blockers") == ["qc_unavailable"] * 3
        for job in takes:
            approve(client, url, job)
        opts, proposal = plan(client, url, clip)
        assert proposal["blockers"] == proposal["warnings"] == []
        assert [i["take_id"] for i in proposal["inputs"]] == [j["id"] for j in takes]
        expected = bytes(23 * 48 * 2) + bytes(157 * 48 * 2).join(
            samples(*PHRASES[text]) for text in ("One.", "Two.", "Three.")
        )
        assert proposal["frames"] * 2 == len(expected)
        record = publish(client, url, opts, proposal)
        path = "/api/exports/" + record["id"] + "/wav"
        response = client.get(path)
        assert response.status_code == 200
        content = response.content
        assert hashlib.sha256(content).hexdigest() == record["files"]["wav"]["sha256"]
        with wave.open(io.BytesIO(content), "rb") as wav:
            assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 48000, 2)
            assert wav.getnframes() == len(expected) // 2
            assert wav.readframes(wav.getnframes()) == expected
        assert client.get(path, headers={"Range": "bytes=0-43"}).status_code == 206
        assert client.get(path + "?download=true").headers["content-disposition"].startswith("attachment")

        options, adjusted = plan(client, url, clip, leading_ms=0, gap_ms=0)
        second = publish(client, url, options, adjusted)
        assert second["plan"]["frames"] < record["plan"]["frames"]
        assert voice.calls == 4  # One preview and three takes.
        duplicate = batch(client, url, clip, "new-variant", "repeat", [segments[1]],
                          source_take=takes[1]["id"])["jobs"][0]
        duplicate = await_job(client, duplicate["id"])
        approve(client, url, duplicate)
        selected = client.put(url + "/take-selection", json={"take_id": duplicate["id"], "version": 1})
        assert selected.status_code == 200
        # The former preview cannot authorize a changed take selection.
        assert client.post(url + "/exports", json={**options, "plan_id": adjusted["plan_id"]}).status_code == 409
        assert all(not item["current"] for item in client.get(url + "/exports").json())
        edited = client.patch(url, json={
            "base_revision": clip["revision"], "edits": [{"start": 0, "end": 4, "text": "Changed."}],
        }).json()
        assert status_codes(plan(client, url, edited)[1], "blockers") == ["no_selection"]
        assert client.post(url + "/export-plan", json=options).status_code == 409
    with TestClient(create_app(db, ExportVoice())) as reopened:
        history = reopened.get(url + "/exports").json()
        assert [item["id"] for item in history] == [second["id"], record["id"]]
        assert history[1]["plan"] == proposal
        assert reopened.get(path).content == content


def test_qc_version_and_bad_takes_block_both_exports(tmp_path):
    recognizer = ExportWhisper()
    db = tmp_path / "clips.sqlite3"
    with TestClient(create_app(db, ExportVoice(), recognizer)) as client:
        url, clip, segments, takes = prepare(client, "One.\nTwo.")
        for job in takes:
            qc_state(client, url + "/takes/" + job["id"] + "/qc", "complete")
        opts, proposal = plan(client, url, clip)
        assert proposal["blockers"] == [] and recognizer.calls == 2
        recognizer.version = "changed"
        assert status_codes(plan(client, url, clip)[1], "blockers") == ["qc_stale"] * 2
        assert status_codes(plan(client, url, clip, mode="draft")[1], "warnings") == ["qc_stale"] * 2
        assert client.post(url + "/exports", json={**opts, "plan_id": proposal["plan_id"]}).status_code == 409
        first, second = takes
        approve(client, url, first, decision="rejected")
        approve(client, url, second)
        before = plan(client, url, clip, mode="draft")[1]
        assert status_codes(before, "blockers") == ["rejected"]
        assert status_codes(plan(client, url, clip)[1], "blockers") == ["rejected"]

        asset = db.with_suffix(".audio") / (second["result"]["asset"]["sha256"] + ".wav")
        original = asset.read_bytes()
        asset.write_bytes(b"corrupt")
        assert status_codes(plan(client, url, clip, mode="draft")[1], "blockers") == ["rejected", "invalid_wav"]
        asset.unlink()
        assert status_codes(plan(client, url, clip, mode="draft")[1], "blockers") == ["rejected", "missing_wav"]
        asset.write_bytes(original)
        # An inapplicable selection must never route a different segment into the master.
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE take_selection SET current=? WHERE segment_id=?", (first["id"], segments[1]))
        assert status_codes(plan(client, url, clip, mode="draft")[1], "blockers") == ["rejected", "stale"]


def test_mp3_roundtrip_and_wav_fallback(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg integration requires FFmpeg in PATH")
    with TestClient(create_app(tmp_path / "clips.sqlite3", ExportVoice())) as client:
        url, clip, _, _ = prepare(client, "One.")
        options, proposal = plan(client, url, clip, mode="draft", format="mp3", gap_ms=0, leading_ms=0)
        assert status_codes(proposal, "warnings") == ["qc_unavailable"]
        record = publish(client, url, options, proposal)
        assert not record["mp3_error"] and record["files"]["mp3"]["bitrate_kbps"] == 128
        path = "/api/exports/" + record["id"]
        mp3 = client.get(path + "/mp3?download=true")
        assert mp3.headers["content-type"] == "audio/mpeg"
        downloaded = tmp_path / "download.mp3"
        downloaded.write_bytes(mp3.content)
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(downloaded), "-f", "s16le", "-"],
            capture_output=True, check=True, timeout=30,
        ).stdout
        assert len(decoded) == proposal["frames"] * 2
        assert client.get(path + "/wav").content.startswith(b"RIFF")

        def broken(*args):
            raise RuntimeError("Кодер недоступен.")

        monkeypatch.setattr("clips.exports.encode_mp3", broken)
        fallback = publish(client, url, options, proposal)
        assert fallback["mp3_error"] and set(fallback["files"]) == {"wav"}
        assert client.get("/api/exports/" + fallback["id"] + "/wav").content == client.get(path + "/wav").content
        assert client.get("/api/exports/" + fallback["id"] + "/mp3").status_code == 404
