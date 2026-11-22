#!/usr/bin/python3.12
"""Provision the installed EL9 package and start its systemd unit."""

import argparse
from fcntl import flock, LOCK_EX
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

from clips import __version__
from clips.vox_worker import UPSTREAM

APP = Path(__file__).resolve().parent
ROOT = Path("/var/lib/tts-clips-runtime")
RUNTIME = ROOT / __version__
PYTHON = RUNTIME / "venv/bin/python"
PYTHON_READY = RUNTIME / "python-ready"
READY = RUNTIME / "ready"
UNIT = "tts-clips.service"
SERVICE_PATH = "/usr/local/bin:/usr/bin"


def run(*command, **options):
    subprocess.run(command, check=True, **options)


def verify_host():
    print("Checking the NVIDIA driver and FFmpeg with the service account", flush=True)
    if shutil.which("nvidia-smi", path=SERVICE_PATH) is None:
        raise RuntimeError("Install the NVIDIA driver for EL9 and check nvidia-smi.")
    if shutil.which("ffmpeg", path=SERVICE_PATH) is None:
        raise RuntimeError("Install FFmpeg with libmp3lame as described in the server guide.")
    run("runuser", "-u", "tts-clips", "--", "env", f"PATH={SERVICE_PATH}",
        "nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv")
    run("runuser", "-u", "tts-clips", "--", "env", f"PATH={SERVICE_PATH}",
        "ffmpeg", "-nostdin", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1",
        "-i", "pipe:0", "-c:a", "libmp3lame", "-f", "mp3", "pipe:1",
        input=bytes(9600), stdout=subprocess.DEVNULL, timeout=15)


def install_python():
    # Build in the final path because venv entry points contain absolute paths.
    run(sys.executable, "-m", "venv", "--clear", RUNTIME / "venv")
    run(PYTHON, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
        "-r", APP / "requirements-rhel.lock", "-r", APP / "requirements-cuda.lock")
    run(PYTHON, "-m", "pip", "check")
    # The worker uses VoxCPM's inference API without training, web demos or denoising.
    # Preserve pip's direct_url.json, which the worker uses to verify the source commit.
    run(PYTHON, "-m", "pip", "install", "--no-deps", "--no-build-isolation",
        f"voxcpm @ git+https://github.com/OpenBMB/VoxCPM.git@{UPSTREAM}")


def verify_python():
    run("runuser", "-u", "tts-clips", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
        PYTHON, "-I", "-c", """
import faster_whisper, librosa, soundfile, torch, torchaudio
from voxcpm import VoxCPM
if torch.version.cuda != "12.8" or not torch.cuda.is_available():
    raise SystemExit("PyTorch cannot use CUDA 12.8. Check the driver and GPU permissions.")
value = (torch.ones(8, device="cuda:0") + 1).sum().item()
if value != 16:
    raise SystemExit("CUDA self-test returned the wrong result.")
print(f"PyTorch {torch.__version__}, CUDA {torch.version.cuda}, {torch.cuda.get_device_name(0)}")
""", timeout=90)


def matches(path, checksum):
    if not path.is_file():
        return False
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest() == checksum


def install_models():
    manifest = json.loads((APP / "models.json").read_text(encoding="utf-8"))
    for name, entry in manifest.items():
        source = {key: entry[key] for key in ("repository", "revision")}
        model = ROOT / "models" / f"{name}-{source['revision']}"
        receipt = model / "tts-books-model.json"
        if model.exists():
            if (all(matches(model / file, digest) for file, digest in entry["files"].items()) and
                    receipt.is_file() and json.loads(receipt.read_text("utf-8")) == source):
                continue
            raise RuntimeError(f"Model files failed verification in {model}. Move the directory aside and rerun.")

        partial = model.with_suffix(".partial")
        partial.mkdir(parents=True, exist_ok=True)
        for file, digest in entry["files"].items():
            path = partial / file
            if matches(path, digest):
                continue
            path.unlink(missing_ok=True)
            print(f"Downloading {source['repository']}/{file}", flush=True)
            run(PYTHON.with_name("hf"), "download",
                source["repository"], file, "--revision", source["revision"], "--local-dir", partial)
            if not matches(path, digest):
                path.unlink(missing_ok=True)
                raise RuntimeError(f"SHA-256 mismatch for {path}")
        (partial / receipt.name).write_text(json.dumps(source) + "\n", encoding="utf-8")
        shutil.rmtree(partial / ".cache", ignore_errors=True)
        partial.rename(model)


def start_service():
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", UNIT)
    client = build_opener(ProxyHandler({}))
    deadline = monotonic() + 30
    while monotonic() < deadline:
        try:
            with client.open("http://127.0.0.1:8765/openapi.json", timeout=2) as response:
                info = json.load(response)["info"]
                if info["title"] == "TTS Clips" and info["version"] == __version__:
                    run("systemctl", "is-active", "--quiet", UNIT)
                    print(f"TTS Clips {__version__} is running on http://127.0.0.1:8765", flush=True)
                    return
        except (URLError, TimeoutError):
            pass
        sleep(0.5)
    raise RuntimeError(f"TTS Clips did not start. See journalctl -u {UNIT} -n 80.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check host prerequisites without installing")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run with sudo")
    verify_host()
    if args.check:
        return
    os.umask(0o022)
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    ROOT.mkdir(mode=0o755, exist_ok=True)
    with (ROOT / ".setup.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        if not PYTHON_READY.is_file():
            install_python()
            PYTHON_READY.touch()
        verify_python()
        install_models()
        READY.touch()
        start_service()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"TTS Clips setup: {error}")
