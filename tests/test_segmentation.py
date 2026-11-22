from contextlib import closing
from pathlib import Path
from random import Random

import pytest
from fastapi.testclient import TestClient

from clips.main import create_app
from clips.recovery import split_at_sentence
from clips.store import Store
from clips.text import LEGACY_POLICY, LIMIT, POLICY, digest, segment, sentence_ends
from test_clips import edit, identities, replace, speech
from test_export import approve, plan, publish, status_codes
from test_voice import ControlledVox, ControlledWhisper, await_job, batch, qc_state, setup_clip


FIXTURE = Path(__file__).resolve().parents[1] / "docs/fixtures/c1.9c/riverside.txt"
PARAGRAPHS = FIXTURE.read_text(encoding="utf-8").strip().split("\n\n")


def assert_coverage(text):
    spans = segment(text)
    assert spans == segment(text)
    cursor = 0
    for span in spans:
        chunk = text[span.start:span.end]
        assert span.start == cursor < span.end
        if span.segment_id is None:
            assert chunk.isspace()
        else:
            assert chunk == chunk.strip() and len(chunk) <= LIMIT
        cursor = span.end
    assert cursor == len(text)
    assert "".join(text[span.start:span.end] for span in spans) == text
    return speech(text, spans)


@pytest.mark.parametrize("length", [100, 240, 400, 500, 600, 700, 749, 801, 1321])
def test_single_sentence_lengths(length):
    words = "По дороге к станции жители заметили новый фонарь и остановились у моста "
    text = (words * length)[:length - 1] + "."
    chunks = assert_coverage(text)
    if length <= LIMIT:
        assert chunks == [text]
    else:
        assert len(chunks) > 1
        assert 400 <= len(chunks[0]) <= LIMIT
        assert text[len(chunks[0])].isspace()


def test_paragraphs_join_and_prefer_a_natural_edge():
    source = " \t" + "\r\n \r\n".join(PARAGRAPHS[:4]) + "  \r\n"
    chunks = assert_coverage(source)
    assert chunks == ["\r\n \r\n".join(PARAGRAPHS[:3]), PARAGRAPHS[3]]
    assert assert_coverage("Свет погас.\r\nПродолжение реплики.")[0].count("\r\n") == 1
    tiny = "\n\n".join(["Абзац."] * 17)
    assert all(chunk.count("\n\n") < 4 for chunk in assert_coverage(tiny))
    across_paragraphs = "Возьми с собой\n\nфонарь,\n\nкарту,\n\nзапас воды\n\nи тёплую куртку."
    assert assert_coverage(across_paragraphs) == [across_paragraphs]


def test_long_paragraph_prefers_sentence_boundaries_and_keeps_short_complete_sentences():
    source = " ".join(PARAGRAPHS[:4])
    chunks = assert_coverage(source)
    assert 400 <= len(chunks[0]) <= 600 and len(chunks) == 2
    assert chunks[0].endswith(".")
    short = "Поздно вечером к набережной подошли первые жители соседнего квартала."
    almost_maximum = PARAGRAPHS[4][:592].rsplit(" ", 1)[0] + "."
    tail = almost_maximum.replace("старого моста, чтобы", "старого моста,\n\nчтобы")
    # The second sentence fits alone. Taking any words from it would split a sentence.
    assert assert_coverage(short + " " + tail) == [short, tail]


def test_complex_russian_punctuation_and_recovery():
    phrases = [
        "На ул. Лётной встретились И. В. Крылов и его коллега.",
        "«Куда идём?..» — спросила она.",
        "«Всё готово!»",
        "Механик проверил крепления (работа закончена.)",
        "Он задумался… и продолжил разговор (выключить ли свет?) уже в коридоре.",
        "В комплект входят перчатки, каски и т. д.",
        "В смете — пятнадцать руб. двадцать коп. Мастер подписал акт.",
    ]
    text = " ".join(phrases)
    assert sentence_ends(text) == [len(" ".join(phrases[:i])) for i in range(1, len(phrases) + 1)]
    first = "Доклад завершён (по итогам встречи)."
    dialogue = first + "\r\n" + "И. В. Крылов открыл заседание."
    assert split_at_sentence(dialogue, POLICY) == len(first)
    assert split_at_sentence("Доклад подготовил И. В. Крылов вместе с коллегами", POLICY) is None


def test_word_and_unicode_fallback_have_exact_coverage():
    assert assert_coverage(" \r\n \t") == []
    word = "я" * (LIMIT - 1) + "е\u0301🐈\u200d⬛" + "у" * 770
    chunks = assert_coverage(word)
    assert len(chunks[0]) == LIMIT - 1
    assert chunks[1].startswith("е\u0301🐈\u200d⬛")
    assert len(assert_coverage("я" + "\u0301" * (2 * LIMIT + 3))) == 3
    assert_coverage("!" * 20_000 + "ещё одно слово")
    rng = Random(42)
    tokens = ("ежо́вый", "🚉", "«В путь!»", "иголки", "движение", "плакат…", "😺", "линия.")
    gaps = (" ", "  ", "\t", "\r\n", "\r\n\r\n", "\r", "\n \n", "\u00a0")
    for _ in range(25):
        assert_coverage("".join(rng.choice(tokens) + rng.choice(gaps) for _ in range(200)))


def test_new_clip_uses_source_offsets_and_keeps_unchanged_neighbour_after_edit(tmp_path):
    with TestClient(create_app(tmp_path / "clips.sqlite3", ControlledVox())) as client:
        source = "🚉 " + "\r\n\r\n".join(PARAGRAPHS[:4]).replace("После", "По́сле", 1)
        url, clip, (first, second) = setup_clip(client, source)
        assert clip["policy"] == POLICY and clip["segmentation_upgrade_count"] is None
        assert clip["text"] == source and clip["text_sha256"] == digest(source)
        span, _, last = clip["spans"]
        assert last["start"] == source.index(PARAGRAPHS[3])
        selected = batch(client, url, clip, "one", "selected", [second])["jobs"][0]
        await_job(client, selected["id"])
        assert selected["request"]["text"] == source[last["start"]:last["end"]]
        assert selected["text_hash"] == last["text_sha256"]
        start = source.index("открыли")
        updated = edit(client, clip, [replace(start, start + len("открыли"), "распахнули")]).json()
        assert identities(updated)[0] != first and identities(updated)[1] == second
        job, = batch(client, url, updated, "fill")["jobs"]
        assert job["request"]["text"].count("\r\n\r\n") == 2
        assert job["request"]["text"].startswith(source[:start] + "распахнули")
        assert "\r\n" in clip["text"][span["start"]:span["end"]]


def test_explicit_upgrade_preserves_only_equal_material_and_survives_restart(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    recognizer = ControlledWhisper()
    with TestClient(create_app(db, ControlledVox(), recognizer)) as client:
        source = "\n\n".join(PARAGRAPHS[:4])
        url, legacy, old_ids = setup_clip(client, source, legacy_db=db)
        assert len(old_ids) == 4 and legacy["segmentation_upgrade_count"] == 2
        edited = edit(client, legacy, [replace(0, len("После"), "После долгого")], title="Набережная").json()
        assert edited["policy"] == LEGACY_POLICY
        assert len(identities(edited)) == 4 and edited["segmentation_upgrade_count"] == 2
        jobs = [await_job(client, job["id"]) for job in batch(client, url, edited, "old")["jobs"]]
        for job in jobs:
            qc_state(client, url + "/takes/" + job["id"] + "/qc", "complete")
            approve(client, url, job, note="Решение до обновления")
        preserved = jobs[-1]
        retry = batch(client, url, edited, "repeat", "repeat", [old_ids[-1]], source_take=preserved["id"])
        alternate = await_job(client, retry["jobs"][0]["id"])
        qc_state(client, url + "/takes/" + alternate["id"] + "/qc", "complete")
        selection = client.put(url + "/take-selection", json={"take_id": alternate["id"], "version": 1}).json()
        opts, proposal = plan(client, url, edited, mode="draft")
        exported = publish(client, url, opts, proposal)
        download = "/api/exports/" + exported["id"] + "/wav"
        wav = client.get(download).content
        previous = client.get(url + "/takes").json()
        asset = "/api/audio/" + jobs[0]["result"]["asset"]["sha256"]
        previous_audio = client.get(asset).content

        upgrade_url = url + "/segmentation-upgrade"
        stale = client.post(upgrade_url, json={"base_revision": legacy["revision"]})
        assert stale.status_code == 409 and stale.json()["current"] == edited
        changed = client.post(upgrade_url, json={"base_revision": edited["revision"]}).json()
        assert changed["policy"] == POLICY and changed["segmentation_upgrade_count"] is None
        assert changed["text"] == edited["text"] and changed["text_sha256"] == edited["text_sha256"]
        assert changed["voice_id"] == edited["voice_id"] and changed["title"] == edited["title"]
        assert changed["revision"] != edited["revision"]
        assert identities(changed)[-1] == old_ids[-1]
        assert identities(changed)[0] not in identities(edited)
        assert client.post(upgrade_url, json={"base_revision": changed["revision"]}).json() == changed
        assert client.get(url + "/takes").json() == previous  # Includes jobs, QC, reviews and selections.
        assert status_codes(plan(client, url, changed, mode="draft")[1], "blockers") == ["no_selection"]
        assert client.get(url + "/exports").json()[0]["current"] is False
        assert client.get(download).content == wav

    with TestClient(create_app(db, ControlledVox(), ControlledWhisper())) as client:
        assert client.get(url).json() == changed
        assert client.get(url + "/takes").json() == previous
        assert client.get(download).content == wav and client.get(asset).content == previous_audio
        assert next(row for row in previous["selections"] if row["segment_id"] == old_ids[-1])["current"] == selection["current"]
        assert [job["segment_id"] for job in batch(client, url, changed, "new")["jobs"]] == identities(changed)[:1]
    with closing(Store(db).connect()) as conn:
        assert tuple(conn.execute("SELECT parent_id,policy FROM revision WHERE id=?", (changed["revision"],)).fetchone()) == (edited["revision"], POLICY)
        assert [dict(row) for row in conn.execute(
            "SELECT ordinal,start,end,segment_id,text_sha256 FROM span WHERE revision_id=? ORDER BY ordinal",
            (edited["revision"],),
        )] == edited["spans"]
