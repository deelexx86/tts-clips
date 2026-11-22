"""Process boundary and provenance for local Whisper."""

from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import subprocess
from time import monotonic

from .completion import Transcript

WORKER = Path(__file__).with_name("whisper_worker.py")
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


class Interrupted(Exception):
    pass


class Whisper:
    def __init__(self, python, model):
        self.python = Path(python)
        self.model = Path(model)
        self._cached = None

    @classmethod
    def configured(cls):
        python = os.environ.get("TTS_CLIPS_WHISPER_PYTHON")
        model = os.environ.get("TTS_CLIPS_WHISPER_MODEL")
        if python or model:
            return cls(python or "", model or "")
        return None

    def run(self, argument, payload, stopping):
        deadline = monotonic() + 120
        with subprocess.Popen(
            [str(self.python), "-I", "-u", str(WORKER), argument], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as proc:
            try:
                data = json.dumps(payload, ensure_ascii=True).encode("ascii")
                while not stopping():
                    if monotonic() >= deadline:
                        raise RuntimeError("Истекло время ожидания Whisper.")
                    try:
                        output, errors = proc.communicate(data, timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        data = None
                else:
                    raise Interrupted()
                if proc.returncode:
                    details = errors.decode("utf-8", errors="replace").strip().splitlines()
                    raise RuntimeError(details[-1] if details else "Процесс Whisper завершился с ошибкой.")
                result = json.loads(output)
                if not isinstance(result, dict):
                    raise ValueError("Некорректный ответ Whisper.")
                return result
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def identity(self, stopping):
        if not self.python.is_file() or not self.model.is_dir():
            raise RuntimeError("Укажите пути к Python и модели Whisper в настройках запуска.")
        files = [self.model / name for name in MODEL_FILES]
        # Additional processor config can change feature extraction.
        optional = self.model / "preprocessor_config.json"
        if optional.is_file():
            files.append(optional)
        stamps = []
        for path in [self.python, WORKER, *files]:
            stat = path.stat()
            stamps.append((path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        if self._cached and stamps == self._cached[0]:
            return self._cached[1]
        hashes = {}
        for path in files:
            if stopping():
                raise Interrupted()
            with path.open("rb") as file:
                hashes[path.name] = file_digest(file, "sha256").hexdigest()
        identity = {
            "engine": "faster-whisper-cpu-int8",
            "worker_sha256": sha256(WORKER.read_bytes()).hexdigest(),
            "files": hashes,
            "versions": self.run("--versions", {}, stopping),
        }
        self._cached = stamps, identity
        return identity

    def transcribe(self, wav, language, stopping):
        row = self.run(str(self.model), {"wav": str(wav), "language": language}, stopping)
        if not all(isinstance(row.get(key), str) for key in ("full", "ending")):
            raise ValueError("Whisper вернул некорректный транскрипт.")
        return Transcript(row["full"], row["ending"])
