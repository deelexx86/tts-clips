"""Offline Whisper decoding in a separate CPU runtime."""

import importlib.metadata
import json
import os
import sys
from pathlib import Path

RATE = 16_000
TAIL_SECONDS = 12
OPTIONS = {
    "task": "transcribe", "beam_size": 5, "temperature": 0.0,
    "vad_filter": False, "condition_on_previous_text": False,
}


def main():
    if sys.argv[1] == "--versions":
        print(json.dumps({
            package: importlib.metadata.version(package)
            for package in ("faster-whisper", "ctranslate2", "tokenizers", "av", "numpy")
        }))
        return

    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    request = json.load(sys.stdin)
    samples = decode_audio(request["wav"], sampling_rate=RATE)
    if not samples.size:
        raise ValueError("Empty WAV")
    model = WhisperModel(str(Path(sys.argv[1])), device="cpu", compute_type="int8",
                         cpu_threads=4, local_files_only=True)

    def transcribe(audio):
        segments, _ = model.transcribe(audio, language=request["language"], **OPTIONS)
        return " ".join(segment.text.strip() for segment in segments).strip()

    full = transcribe(samples)
    ending = transcribe(samples[-RATE * TAIL_SECONDS:]) if len(samples) > RATE * TAIL_SECONDS else full
    print(json.dumps({"full": full, "ending": ending}, ensure_ascii=True))


if __name__ == "__main__":
    main()
