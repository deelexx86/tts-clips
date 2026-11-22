from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from clips.main import create_app
from clips.store import NOW, Store
from clips.text import LEGACY_LIMIT, LEGACY_POLICY, POLICY, normalize, segment


def speech(text, spans):
    return [text[row.start:row.end] for row in spans if row.segment_id is not None]


@pytest.mark.parametrize(("source", "expected"), [
    ("", []),
    ("  \r\n \t\r", []),
    (" \r\nЁж.\rОна́ 🙂!\n\nДа.\nДа. ", ["Ёж.", "Она́ 🙂!", "Да.", "Да."]),
    ("а" * LEGACY_LIMIT + "  б", ["а" * LEGACY_LIMIT, "б"]),
    ("а" * (LEGACY_LIMIT + 2), ["а" * LEGACY_LIMIT, "аа"]),
    ("«" + "я" * 90 + "!» " + "слово " * 27, ["«" + "я" * 90 + "!»", ("слово " * 27).strip()]),
    ("а" * 210 + "  " + "б" * 50, ["а" * 210, "б" * 50]),
])
def test_deterministic_exact_coverage(source, expected):
    text = normalize(source)
    spans = segment(text, LEGACY_POLICY)
    assert spans == segment(text, LEGACY_POLICY)
    assert speech(text, spans) == expected
    cursor = 0
    for span in spans:
        assert span.start == cursor < span.end
        chunk = text[span.start:span.end]
        if span.segment_id is None:
            assert chunk.isspace()
        else:
            assert chunk == chunk.strip() and len(chunk) <= LEGACY_LIMIT
        cursor = span.end
    assert cursor == len(text)
    assert "".join(text[span.start:span.end] for span in spans) == text


def edit(client, clip, edits, **kwargs):
    return client.patch(f"/api/clips/{clip['id']}", json={
        "base_revision": clip["revision"], "edits": edits, **kwargs,
    })


def replace(start, end, text):
    return dict(start=start, end=end, text=text)


def identities(clip):
    return [row["segment_id"] for row in clip["spans"] if row["segment_id"]]


def legacy_clip(db):
    store = Store(db)
    clip_id = uuid4().hex
    with closing(store.connect()) as conn, conn:
        conn.execute(f"INSERT INTO clip VALUES (?, NULL, {NOW})", (clip_id,))
        store.publish(conn, clip_id, None, "", "", [], LEGACY_POLICY, None)
    return store.get(clip_id)


def test_identity_follows_exact_position_through_edits_and_reopen(tmp_path):
    db = tmp_path / "clips.sqlite3"
    with TestClient(create_app(db)) as client:
        draft = legacy_clip(db)
        clip = edit(client, draft, [replace(0, 0, "Да.\r\nДа.\rЭхо 🙂")], title="Проба").json()
        first, second, third = identities(clip)
        shifted = edit(client, clip, [replace(0, 0, "Да.\n")]).json()
        added, *rest = identities(shifted)
        assert added not in rest and rest == [first, second, third]
        changed = edit(client, shifted, [replace(4, 7, "Нет."), replace(0, 4, "")]).json()
        modified, same, last = identities(changed)
        assert modified not in {added, first, second, third}
        assert (same, last) == (second, third)
        assert changed["text"] == "Нет.\nДа.\nЭхо 🙂"
    with TestClient(create_app(db)) as reopened:
        assert reopened.get("/api/clips").json()[0]["title"] == "Проба"
        assert reopened.get(f"/api/clips/{draft['id']}").json() == changed
        cleared = edit(reopened, changed, [replace(0, len(changed["text"]), "")]).json()
        assert cleared["text"] == "" and cleared["spans"] == []


def test_edits_are_atomic_and_stale_revision_returns_snapshot(tmp_path):
    with TestClient(create_app(tmp_path / "clips.sqlite3")) as client:
        draft = client.post("/api/clips").json()
        assert draft["policy"] == POLICY
        assert edit(client, draft, [replace(0, 0, "data"), replace(9, 10, "")]).status_code == 422
        assert client.get(f"/api/clips/{draft['id']}").json() == draft
        current = edit(client, draft, [replace(0, 0, "Сохранено.")]).json()
        assert edit(client, current, [replace(0, 0, "")]).json() == current
        stale = edit(client, draft, [replace(0, 0, "Черновик")])
        assert stale.status_code == 409
        assert stale.json() == {"current": current}


def test_concurrent_saves_from_same_base_have_one_winner(tmp_path):
    db = tmp_path / "clips.sqlite3"
    with TestClient(create_app(db)) as client:
        draft = client.post("/api/clips").json()
        barrier = Barrier(2)

        def save(text):
            barrier.wait(timeout=5)
            return edit(client, draft, [replace(0, 0, text)])

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = sorted(pool.map(save, ("Первая", "Вторая")), key=lambda r: r.status_code)
        winner, loser = responses
        assert [winner.status_code, loser.status_code] == [200, 409]
        assert loser.json()["current"] == winner.json()
        assert client.get(f"/api/clips/{draft['id']}").json() == winner.json()
