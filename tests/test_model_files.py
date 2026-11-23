from hashlib import sha256
import json

import pytest

from clips.model_files import prepare_models


@pytest.fixture
def snapshot(tmp_path):
    files = {"config.json": b"config", "model.bin": b"weights"}
    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps({"test": {
        "repository": "example/model", "revision": "pinned",
        "files": {name: sha256(data).hexdigest() for name, data in files.items()},
    }}))
    root = tmp_path / "models"
    root.mkdir()
    return manifest, root, files


def test_interrupted_preparation_reuses_completed_file_and_download_cache(snapshot):
    manifest, root, files = snapshot
    model = root / "test-pinned"
    calls = []

    def interrupted(source, name, partial):
        calls.append(name)
        if name == "model.bin":
            cache = partial / ".cache"
            cache.mkdir()
            (cache / "pending").write_bytes(b"partial download")
            raise ConnectionError()
        (partial / name).write_bytes(files[name])

    with pytest.raises(ConnectionError):
        prepare_models(manifest, root, interrupted)
    assert not model.exists()
    assert calls == ["config.json", "model.bin"]

    def resume(source, name, partial):
        assert name == "model.bin"
        assert (partial / ".cache/pending").read_bytes() == b"partial download"
        (partial / name).write_bytes(files[name])

    assert prepare_models(manifest, root, resume) == {"test": model}
    assert not model.with_suffix(".partial").exists()
    assert not (model / ".cache").exists()

    def unexpected(*args):
        pytest.fail("An existing snapshot must not trigger a download")

    prepare_models(manifest, root, unexpected)
    (model / "model.bin").write_bytes(b"corruption")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch.*model.bin"):
        prepare_models(manifest, root, unexpected)


def test_offline_preseed_requires_all_hashes_before_recording_receipt(snapshot):
    manifest, root, files = snapshot
    model = root / "test-pinned"
    model.mkdir()
    receipt = model / "tts-books-model.json"

    def unexpected(*args):
        pytest.fail("A pre-seeded snapshot must not trigger a download")

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        prepare_models(manifest, root, unexpected, allow_preseed=True)
    assert not receipt.exists()
    for name, contents in files.items():
        (model / name).write_bytes(contents)
    prepare_models(manifest, root, unexpected, allow_preseed=True)
    assert json.loads(receipt.read_text()) == {"repository": "example/model", "revision": "pinned"}
