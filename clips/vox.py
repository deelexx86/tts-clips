"""One warm subprocess, using SSH stdio when a host is configured."""

import base64
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Thread
from time import monotonic

WORKER = Path(__file__).with_name("vox_worker.py")
MESSAGE_LIMIT = 24 * 1024**2


class Cancelled(Exception):
    pass


class VoxClient:
    def __init__(self, command=None, bootstrap=None, timeout=600):
        self.command = command
        self.bootstrap = bootstrap
        self.timeout = timeout
        self.process = None

    @classmethod
    def configured(cls):
        python = os.environ.get("TTS_CLIPS_VOX_PYTHON")
        model = os.environ.get("TTS_CLIPS_VOX_MODEL")
        host = os.environ.get("TTS_CLIPS_VOX_SSH")
        if not python or not model:
            return cls()
        if not host:
            return cls([python, "-I", "-u", str(WORKER), model])
        # The remote login shell is Windows cmd.exe. Only accept absolute ordinary paths.
        if not re.fullmatch(r"[\w.-]+", host) or host.startswith("-"):
            raise ValueError("Use an SSH host alias in TTS_CLIPS_VOX_SSH")
        for path in (python, model):
            if not re.fullmatch(r"[A-Za-z]:[\\/][\w .\\/()-]+", path) or path.endswith(("\\", "/")):
                raise ValueError("Use absolute Windows paths in the VoxCPM2 settings")
        code = "exec(__import__('base64').b64decode(input()))"
        remote = f'cmd /d /s /c ""{python}" -I -u -c "{code}" "{model}""'
        return cls(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote],
                   base64.b64encode(WORKER.read_bytes()) + b"\n")

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def generate(self, key, payload, reference, progress, cancelled):
        if not self.command:
            raise RuntimeError("Настройте пути к Python и модели VoxCPM2 по инструкции README.")
        deadline = monotonic() + self.timeout

        def receive():
            while not cancelled():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RuntimeError("Истекло время ожидания VoxCPM2.")
                try:
                    line = self.events.get(timeout=min(remaining, 0.1))
                except Empty:
                    continue
                if not line or not line.endswith(b"\n"):
                    raise RuntimeError("Соединение с VoxCPM2 завершилось без полного ответа.")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Некорректный ответ VoxCPM2.")
                return event
            raise Cancelled()

        try:
            if cancelled():
                raise Cancelled()
            if self.process is not None and self.process.poll() is not None:
                self.close()
            if self.process is None:
                progress("connecting")
                self.process = subprocess.Popen(
                    self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.events = Queue()
                Thread(target=self.read_events, args=(self.process.stdout, self.events), daemon=True).start()
                if self.bootstrap:
                    self.send(self.bootstrap)
                if receive() != {"event": "hello", "protocol": 1}:
                    raise RuntimeError("Неподдерживаемый протокол VoxCPM2.")
            message = {**payload, "id": key, "reference": base64.b64encode(reference).decode() if reference else None}
            self.send(json.dumps(message, ensure_ascii=True).encode() + b"\n")
            phases = []
            while True:
                event = receive()
                if event.get("id") != key:
                    raise ValueError("VoxCPM2 вернул ответ для другого задания.")
                kind = event.get("event")
                if kind == "error":
                    raise RuntimeError(event["message"])
                if kind == "status" and (
                    (not phases and event.get("phase") in {"loading", "synthesizing"}) or
                    (phases == ["loading"] and event.get("phase") == "synthesizing")
                ):
                    phases.append(event["phase"])
                    progress(event["phase"])
                    continue
                if kind != "done" or not phases or phases[-1] != "synthesizing":
                    raise ValueError("Нарушен порядок событий VoxCPM2.")
                data = base64.b64decode(event.pop("audio"), validate=True)
                expected = f"({payload['description']}){payload['text']}" if payload["description"] else payload["text"]
                if (event["effective_text"] != expected or
                    event["reference_sha256"] != (hashlib.sha256(reference).hexdigest() if reference else None) or
                    event["sha256"] != hashlib.sha256(data).hexdigest() or
                    event["reused"] is not (phases == ["synthesizing"])):
                    raise ValueError("Несовпадение входа или результата VoxCPM2.")
                return data, event
        except Exception:
            self.close()
            raise

    def send(self, data):
        # Keep writes interruptible too, including when SSH stops reading a reference.
        writer = Thread(target=self.write, args=(self.process.stdin, data), daemon=True)
        writer.start()

    @staticmethod
    def write(pipe, data):
        try:
            pipe.write(data)
            pipe.flush()
        except (OSError, ValueError):
            pass  # Process exit is reported to the receiver through EOF.

    @staticmethod
    def read_events(pipe, events):
        try:
            while line := pipe.readline(MESSAGE_LIMIT):
                events.put(line)
                if not line.endswith(b"\n"):
                    break
        finally:
            events.put(b"")
