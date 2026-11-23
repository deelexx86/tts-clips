"""Prepare mounted model snapshots before handing PID and signals to Uvicorn."""

from fcntl import flock, LOCK_EX
import os
from pathlib import Path
import sys

from clips.model_files import prepare_models


def download(source, file, partial):
    from huggingface_hub import hf_hub_download

    if os.environ.get("HF_HUB_OFFLINE") == "1":
        raise RuntimeError(f"Offline mode: pre-seed {source['repository']}/{file} in {partial}")
    hf_hub_download(repo_id=source["repository"], revision=source["revision"],
                    filename=file, local_dir=partial)


def main():
    os.umask(0o027)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    with Path("/models/.prepare.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        models = prepare_models(Path("/app/packaging/models.json"), Path("/models"),
                                download, allow_preseed=True)

    os.environ.update(TTS_CLIPS_VOX_PYTHON=sys.executable, TTS_CLIPS_WHISPER_PYTHON=sys.executable,
                      TTS_CLIPS_VOX_MODEL=str(models["voxcpm2"]),
                      TTS_CLIPS_WHISPER_MODEL=str(models["whisper-small"]))
    os.environ.pop("TTS_CLIPS_VOX_SSH", None)
    os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "clips.main:create_app", "--factory",
                            "--host", "0.0.0.0", "--port", "8765", "--workers", "1",
                            "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "90"])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit(f"TTS Clips startup: {error}")
