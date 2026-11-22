"""Validate, normalize and publish WAV files in the local asset directory."""

from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile
import wave

MAX_WAV = 16 * 1024**2
RATE = 48_000


def wav_info(data):
    try:
        if not 44 <= len(data) <= MAX_WAV or int.from_bytes(data[4:8], "little") != len(data) - 8:
            raise ValueError()
        with wave.open(BytesIO(data), "rb") as wav:
            frames = wav.getnframes()
            frame_size = wav.getnchannels() * wav.getsampwidth()
            pcm = wav.readframes(frames + 1)
            if len(pcm) != frames * frame_size or not any(pcm):
                raise ValueError()
            return {
                "channels": wav.getnchannels(), "width": wav.getsampwidth(),
                "rate": wav.getframerate(), "frames": frames, "seconds": frames / wav.getframerate(),
            }
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        raise ValueError("Нужен целый PCM WAV с ненулевым звуком размером до 16 МиБ.") from None


def normalize_reference(data):
    info = wav_info(data)
    if info["seconds"] > 30 or info["channels"] not in (1, 2):
        raise ValueError("Референс должен быть моно или стерео длительностью до 30 секунд.")
    if (info["channels"], info["width"], info["rate"]) == (1, 2, RATE):
        return data
    # wave writes the final RIFF length. FFmpeg's WAV pipe would use placeholder lengths.
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "wav", "-i", "pipe:0",
             "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
            input=data, capture_output=True, check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Не удалось преобразовать референс. Проверьте наличие FFmpeg или загрузите mono PCM16 48 кГц.") from None
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(result.stdout)
    return output.getvalue()


class Assets:
    def __init__(self, directory: Path):
        self.directory = directory

    def path(self, key):
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise KeyError(key)
        path = self.directory / (key + ".wav")
        if not path.is_file():
            raise KeyError(key)
        return path

    def publish(self, data):
        info = wav_info(data)
        if (info["channels"], info["width"], info["rate"]) != (1, 2, RATE):
            raise ValueError("Ожидался WAV mono PCM16 48 кГц.")
        key = sha256(data).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / (key + ".wav")
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError("Нарушена целостность сохранённого WAV.")
        else:
            with NamedTemporaryFile(dir=self.directory, delete=False) as file:
                staging = Path(file.name)
                try:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                    file.close()
                    staging.replace(destination)
                finally:
                    file.close()
                    staging.unlink(missing_ok=True)
        return {"sha256": key, "bytes": len(data), **info}

    def read(self, key):
        data = self.path(key).read_bytes()
        if sha256(data).hexdigest() != key:
            raise ValueError("Нарушена целостность сохранённого WAV.")
        return data
