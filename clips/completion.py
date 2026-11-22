"""Speech completion rules adapted from TTS Books b553710 (speech_completion.py)."""

from __future__ import annotations

import math
import re
from array import array
from dataclasses import dataclass
from difflib import SequenceMatcher

from .audio import RATE

POLICY = "books-completion-v1"

_WORD = re.compile(r"[^\W_]+(?:[-‑][^\W_]+)*")
_WORD_FORMS = {"1000": "тысяч", "тысяча": "тысяч", "тысячи": "тысяч", "тысячу": "тысяч"}


@dataclass(frozen=True)
class Transcript:
    full: str
    ending: str


@dataclass(frozen=True)
class Completion:
    status: str
    full_coverage: float
    longest_discrepancy: int
    longest_omission: int
    ending_matches: bool
    full_ending_missing: int
    tail_ending_missing: int
    edge_dbfs: float
    expected_ending: tuple[str, ...]
    heard_ending: tuple[str, ...]
    reasons: tuple[str, ...]


def _words(text: str) -> list[str]:
    cleaned = text.casefold().replace("ё", "е").replace("\u0301", "")
    return [
        _WORD_FORMS.get(word, word.replace("-", "").replace("‑", ""))
        for word in _WORD.findall(cleaned)
    ]


def _near(word: str, heard: str) -> bool:
    prefix = len(word) - 2
    return prefix >= 4 and abs(len(word) - len(heard)) <= 2 and heard.startswith(word[:prefix])


def align_tokens(expected: list[str], heard: list[str]):
    return SequenceMatcher(None, expected, heard, autojunk=False).get_opcodes()


def _alignment(expected: list[str], heard: list[str]) -> tuple[float, int, int, int, bool]:
    matched = longest = omission = missing_end = 0
    ending_matches = False
    for kind, start, stop, left, right in align_tokens(expected, heard):
        count = stop - start
        if kind == "equal":
            matched += count
            if stop == len(expected):
                ending_matches = True
            continue
        if kind == "replace" and count == right - left:
            near = [
                _near(a, b) for a, b in zip(expected[start:stop], heard[left:right], strict=True)
            ]
            matched += sum(near)
            count -= sum(near)
        longest = max(longest, count, right - left if kind == "insert" else 0)
        if kind in {"replace", "delete"}:
            omission = max(omission, stop - start - (right - left))
        if stop == len(expected) and kind in {"replace", "delete"}:
            missing_end = count
    return matched / max(1, len(expected)), longest, omission, missing_end, ending_matches


def edge_dbfs(pcm: bytes) -> float:
    samples = array("h", pcm[-2 * (RATE // 100) :])
    rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples))) / 32768
    return 20 * math.log10(max(1e-10, rms))


def check(text: str, transcript: Transcript, pcm: bytes) -> Completion:
    expected, full, tail = map(_words, (text, transcript.full, transcript.ending))
    coverage, longest, omission, full_missing, full_ending = _alignment(expected, full)
    tail_scope = expected[-36:]
    _, _, _, tail_missing, tail_ending = _alignment(tail_scope, tail)
    # Compare the literal ending separately so stem similarity cannot mask a missing syllable.
    ending_ok = full_ending and tail_ending
    edge = edge_dbfs(pcm)
    reasons = []
    if not expected:
        reasons.append("no_expected_words")
    if not ending_ok and expected:
        reasons.append("ending_mismatch")
    if full_missing >= 2 and tail_missing >= 2:
        reasons.append("ending_words_missing")
    if expected and coverage < 0.82:
        reasons.append("low_coverage")
    if longest >= 3:
        reasons.append("long_discrepancy")
    if omission >= 2:
        reasons.append("omission")
    if edge > -32:
        reasons.append("loud_edge")
    status = "verified"
    if not expected:
        status = "suspect"
    elif "ending_words_missing" in reasons or ("loud_edge" in reasons and not ending_ok):
        status = "likely_defect"
    elif reasons:
        status = "suspect"
    return Completion(
        status,
        round(coverage, 4),
        longest,
        omission,
        ending_ok,
        full_missing,
        tail_missing,
        round(edge, 2),
        tuple(expected[-5:]),
        tuple(tail[-5:]),
        tuple(reasons),
    )
