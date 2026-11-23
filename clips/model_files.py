"""Verify and publish immutable model snapshots from the deployment manifest."""

from hashlib import file_digest
import json
import shutil


def matches(path, checksum):
    if not path.is_file():
        return False
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest() == checksum


def prepare_models(manifest_path, root, download, *, allow_preseed=False):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    directories = {}
    for name, entry in manifest.items():
        source = {key: entry[key] for key in ("repository", "revision")}
        model = root / f"{name}-{source['revision']}"
        receipt = model / "tts-books-model.json"
        directories[name] = model
        if model.exists():
            for file, digest in entry["files"].items():
                if not matches(model / file, digest):
                    raise RuntimeError(f"Missing file or SHA-256 mismatch: {model / file}. Restore a verified snapshot.")
            if allow_preseed and not receipt.exists():
                temporary = receipt.with_suffix(".tmp")
                temporary.write_text(json.dumps(source) + "\n", encoding="utf-8")
                temporary.replace(receipt)
            if not receipt.is_file() or json.loads(receipt.read_text("utf-8")) != source:
                raise RuntimeError(f"Invalid model receipt: {receipt}. Restore a verified snapshot.")
            print(f"Verified existing model: {model}", flush=True)
            continue

        partial = model.with_suffix(".partial")
        partial.mkdir(parents=True, exist_ok=True)
        for file, digest in entry["files"].items():
            path = partial / file
            if matches(path, digest):
                continue
            path.unlink(missing_ok=True)
            print(f"Downloading {source['repository']}/{file}", flush=True)
            download(source, file, partial)
            if not matches(path, digest):
                path.unlink(missing_ok=True)
                raise RuntimeError(f"SHA-256 mismatch for {path}")
        (partial / receipt.name).write_text(json.dumps(source) + "\n", encoding="utf-8")
        shutil.rmtree(partial / ".cache", ignore_errors=True)
        partial.rename(model)
        print(f"Published verified model: {model}", flush=True)
    return directories
