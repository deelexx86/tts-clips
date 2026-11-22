from contextlib import closing
import sqlite3

import pytest
from fastapi.testclient import TestClient

from clips import pronunciation
from clips.completion import Transcript, check
from clips.main import create_app
from clips.speech_ready import warnings_for
from test_export import approve, plan, publish, status_codes
from test_voice import ControlledVox, ControlledWhisper, await_job, batch, qc_state, setup_clip


def test_written_forms_report_whole_fragments_and_unicode_positions():
    text = "🚉 Площа́дь 2\u202f450,5 м². Отчёт 22 ноября 2026 г., 15.01.2027 в 08:00–17:45. И. В. Крылов, НИИ, 1-го числа."
    findings = warnings_for(text)
    assert [(x["text"], x["code"]) for x in findings] == [
        ("2\u202f450,5 м²", "measurement"), ("22 ноября 2026 г.", "date"), ("15.01.2027", "date"),
        ("08:00–17:45", "time"), ("И. В.", "abbreviation"), ("НИИ", "abbreviation"), ("1-го", "number"),
    ]
    for finding in findings:
        assert text[finding["start"]:finding["end"]] == finding["text"]
    assert warnings_for("Площадь — две тысячи четыреста пятьдесят с половиной квадратных метров.") == []


def test_confirmation_is_required_only_for_targeted_segments_and_current_revision(tmp_path):
    db = tmp_path / "clips.sqlite3"
    with TestClient(create_app(db, ControlledVox())) as client:
        text = "🚉 На площади 12,75 кг груза.\nВъезд со двора."
        url, clip, segments = setup_clip(client, text, legacy_db=db)
        assert client.get(url).json() == clip
        assert clip["text"] == text
        assert clip["speech_ready"][0]["text"] == "12,75 кг"
        unaffected = batch(client, url, clip, "clean", "selected", [segments[1]])["jobs"][0]
        await_job(client, unaffected["id"])
        command = {"revision": clip["revision"], "key": "retry", "mode": "missing"}
        unconfirmed = client.post(url + "/generations", json=command)
        assert unconfirmed.status_code == 422
        detail = unconfirmed.json()["detail"]
        assert detail["warnings"] == clip["speech_ready"]
        assert len(client.get(url + "/takes").json()["jobs"]) == 1
        renamed = client.patch(url, json={"base_revision": clip["revision"], "title": "Новая редакция"}).json()
        command.update(revision=renamed["revision"], speech_confirmation=detail["confirmation"])
        stale = client.post(url + "/generations", json=command)
        assert stale.status_code == 422
        command["speech_confirmation"] = stale.json()["detail"]["confirmation"]
        reply = client.post(url + "/generations", json=command)
        assert reply.status_code == 202
        job, = reply.json()["jobs"]
        assert "12,75 кг" in job["request"]["text"]
        assert client.post(url + "/generations", json=command).json()["id"] == reply.json()["id"]
        assert client.get(url).json()["text"] == text


EXPECTED = "Сегодня Анна назвала новую площадь и попросила редакцию уточнить данные вечером."
HEARD = EXPECTED.replace("Анна", "Алла")


@pytest.mark.parametrize(("source", "heard", "code"), [
    (EXPECTED, HEARD, "word_substitution"),
    ("Сегодня приняли двадцать семь заявок до закрытия.", "Сегодня приняли двадцать шесть заявок до закрытия.", "number_substitution"),
    ("Площадь работ 2 450,5 м² около станции. Новые данные по итогам обследования опубликуют завтра утром.",
     "Площадь работ 2.450 пища пиалмик ССР около станции. Новые данные по итогам обследования опубликуют завтра утром.",
     "numeric_fragment"),
])
def test_substitutions_and_offsets(source, heard, code):
    transcript = Transcript(heard, heard)
    assert check(source, transcript, bytes(960)).status == "verified"
    result = pronunciation.evaluate(source, heard, "ru")
    assert result["status"] == "review"
    finding, = result["findings"]
    assert finding["code"] == code
    for label, text in [("expected", source), ("heard", heard)]:
        fragment = finding[label]
        assert fragment["text"] == text[fragment["start"]:fragment["end"]]


@pytest.mark.parametrize(("source", "heard"), [
    ("Пётр Ковалёв пришёл вчера, всё уточнили.", "ПЕТР КОВАЛЕВ ПРИШЕЛ ВЧЕРА — ВСЕ УТОЧНИЛИ!"),
    ("Вес составлял ровно восемь целых четыре десятых тонны.", "Вес составлял ровно 8,4 тонны."),
    ("Плё́нку убрали с окна.", "Пленку убрали с окна."),
    ("Вечером инженеры осмотрели ограждение станции.", "Вечером инженеры осмотрели ограждения станции."),
])
def test_lexical_rules_skip_formatting_and_near_inflection(source, heard):
    assert pronunciation.evaluate(source, heard, "ru")["status"] == "clear"


class SubstitutingWhisper(ControlledWhisper):
    """Simulate a recognizer substituting a name in an otherwise matching utterance."""

    def transcribe(self, path, language, stopping):
        super().transcribe(path, language, stopping)
        return Transcript(HEARD, HEARD)


def test_legacy_qc_backfill_and_manual_export_keep_audio_and_history(tmp_path, monkeypatch):
    db = tmp_path / "clips.sqlite3"
    recognizer = SubstitutingWhisper()
    with TestClient(create_app(db, ControlledVox(), recognizer)) as client:
        url, clip, _ = setup_clip(client, EXPECTED)
        take = await_job(client, batch(client, url, clip, "first")["jobs"][0]["id"])
        review_url = url + "/takes/" + take["id"]
        qc_url = review_url + "/qc"
        qc = qc_state(client, qc_url, "complete")
        assert qc["current"]["report"]["status"] == "verified"
        assert qc["current"]["pronunciation_integrity"]["status"] == "review"
        assert status_codes(plan(client, url, clip)[1], "blockers") == ["pronunciation_review"]
        opts, proposal = plan(client, url, clip, mode="draft")
        assert status_codes(proposal, "warnings") == ["pronunciation_review"]
        historical = publish(client, url, opts, proposal)
        historical_wav = client.get("/api/exports/" + historical["id"] + "/wav").content
        assert client.patch(review_url + "/review", json={"decision": "accepted"}).status_code == 422
        approve(client, url, take, note="Сверено с оригиналом, распознавание ошиблось.")
        opts, proposal = plan(client, url, clip)
        final = publish(client, url, opts, proposal)
        entry, = final["plan"]["inputs"]
        assert entry["approval"] == "manual"
        assert entry["review"]["listened"] and entry["review"]["decision"] == "accepted"
        assert entry["qc_basis"]["policy"] == "books-completion-v1"
        assert entry["pronunciation_integrity"]["basis"]["policy"] == pronunciation.POLICY
        assert recognizer.calls == 1

    with closing(sqlite3.connect(db)) as conn, conn:
        legacy_report = conn.execute("SELECT report FROM qc_result").fetchone()[0]
        conn.execute("DROP TABLE pronunciation_result")  # C1.7 database shape.
    with TestClient(create_app(db, ControlledVox(), recognizer)) as client:
        assert client.get(qc_url).json() == qc
        assert client.get(url + "/exports").json() == [final, historical]
    monkeypatch.setattr(pronunciation, "POLICY", "clips-pronunciation-test-next")
    with TestClient(create_app(db, ControlledVox(), recognizer)) as client:
        assert client.post(qc_url).status_code == 202
        recheck = qc_state(client, qc_url, "complete")
        assert recheck["current"]["cached"] and recognizer.calls == 1
        assert recheck["reports"] == qc["reports"]
        new, old = recheck["pronunciation_reports"]
        assert old == qc["pronunciation_reports"][0]
        assert new["basis"]["policy"] == pronunciation.POLICY
        assert client.get("/api/audio-jobs/" + take["id"]).json() == take
        assert client.get(url + "/exports").json() == [final, historical]
        assert client.get("/api/exports/" + historical["id"] + "/wav").content == historical_wav
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT report FROM qc_result").fetchall() == [(legacy_report,)]


def test_recovery_requires_confirmation_for_the_eligible_source(tmp_path):
    with TestClient(create_app(tmp_path / "clips.sqlite3", ControlledVox(), ControlledWhisper())) as client:
        url, clip, _ = setup_clip(client, "Поезд приехал утром. На станцию вышли 5 пассажиров и начальник вокзала.")
        initial = client.post(url + "/generations", json={
            "revision": clip["revision"], "key": "first", "mode": "missing",
        }).json()["detail"]
        take = await_job(client, batch(client, url, clip, "first",
                                       speech_confirmation=initial["confirmation"])["jobs"][0]["id"])
        qc_state(client, url + "/takes/" + take["id"] + "/qc", "complete")
        command = {"revision": clip["revision"], "source_take": take["id"], "key": "recover"}
        response = client.post(url + "/recoveries", json=command)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["warnings"][0]["text"] == "5"
        assert client.get(url + "/takes").json()["recoveries"] == []
        command["speech_confirmation"] = detail["confirmation"]
        reply = client.post(url + "/recoveries", json=command)
        assert reply.status_code == 202
        assert reply.json()["items"][0]["source_id"] == take["id"]
        assert client.post(url + "/recoveries", json=command).json()["id"] == reply.json()["id"]
