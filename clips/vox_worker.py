"""Standalone JSON Lines worker. Vox call adapted from TTS Books b553710."""

import base64
import contextlib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

UPSTREAM = "300e4260b509066151d623dc9c0aa922cf82232b"
REVISION = "7936080b41163874f38610f46f438e2c0757f9fd"
FILES = (
    "config.json", "model.safetensors", "audiovae.pth", "tokenizer.json",
    "tokenizer_config.json", "special_tokens_map.json",
)
MAX_AUDIO = 16 * 1024**2


class Vox:
    def __init__(self, directory):
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
        import numpy
        import soundfile
        import torch
        from voxcpm import VoxCPM

        receipt = json.loads((directory / "tts-books-model.json").read_text("utf-8"))
        dist = importlib.metadata.distribution("voxcpm")
        commit = json.loads(dist.read_text("direct_url.json") or "{}").get("vcs_info", {}).get("commit_id")
        if commit != UPSTREAM or receipt != {"repository": "openbmb/VoxCPM2", "revision": REVISION}:
            raise ValueError("VoxCPM2 runtime or model revision differs from the configured recipe")
        hashes = {}
        for name in FILES:
            with (directory / name).open("rb") as file:
                hashes[name] = hashlib.file_digest(file, "sha256").hexdigest()
        if json.loads((directory / "config.json").read_text("utf-8"))["architecture"] != "voxcpm2":
            raise ValueError("Expected VoxCPM2 model")
        self.identity = {"commit": commit, "revision": REVISION, "files": hashes,
                         "voxcpm": dist.version, "torch": torch.__version__,
                         "transformers": importlib.metadata.version("transformers"),
                         "soundfile": soundfile.__version__, "numpy": numpy.__version__}
        self.model = VoxCPM.from_pretrained(
            str(directory), load_denoiser=False, optimize=False, device="cuda:0", local_files_only=True
        )
        self.numpy = numpy
        self.soundfile = soundfile
        self.torch = torch

    def generate(self, request):
        text = request["text"]
        if request["description"]:
            text = f"({request['description']}){text}"
        reference = request.get("reference")
        reference_hash = None
        with tempfile.TemporaryDirectory(prefix="tts-clips-") as temporary:
            path = Path(temporary)
            ref = None
            if reference is not None:
                data = base64.b64decode(reference, validate=True)
                if not 0 < len(data) <= MAX_AUDIO:
                    raise ValueError("Invalid reference size")
                reference_hash = hashlib.sha256(data).hexdigest()
                ref = path / "reference.wav"
                ref.write_bytes(data)
            started = time.perf_counter()
            wav = self.model.generate(
                text=text, reference_wav_path=str(ref) if ref else None, seed=request["seed"],
                cfg_value=2.0, inference_timesteps=10, normalize=False, denoise=False, retry_badcase=False,
            )
            self.torch.cuda.synchronize()
            seconds = time.perf_counter() - started
            wav = self.numpy.asarray(wav)
            if wav.ndim != 1 or not wav.size or not self.numpy.isfinite(wav).all():
                raise RuntimeError("VoxCPM2 returned invalid audio")
            output = path / "output.wav"
            self.soundfile.write(output, wav, self.model.tts_model.sample_rate, subtype="PCM_16")
            if output.stat().st_size > MAX_AUDIO:
                raise ValueError("Generated WAV exceeds the size limit")
            data = output.read_bytes()
        return {"audio": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest(),
                "reference_sha256": reference_hash, "effective_text": text, "seconds": seconds,
                # The pinned VoxCPM.generate collapses whitespace before passing text to the model.
                "model_text": re.sub(r"\s+", " ", text),
                "model": self.identity}


def serve(directory):
    protocol = sys.stdout
    protocol.reconfigure(encoding="utf-8")

    def emit(**event):
        print(json.dumps(event, ensure_ascii=True, allow_nan=False), file=protocol, flush=True)

    backend = None
    emit(event="hello", protocol=1)
    for line in sys.stdin:
        job = json.loads(line)
        key = job["id"]
        reused = backend is not None
        try:
            with contextlib.redirect_stdout(sys.stderr):
                if not reused:
                    emit(event="status", id=key, phase="loading")
                    backend = Vox(directory)
                emit(event="status", id=key, phase="synthesizing")
                result = backend.generate(job)
            emit(event="done", id=key, reused=reused, **result)
        except Exception as error:
            emit(event="error", id=key, message=str(error))
            return


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
