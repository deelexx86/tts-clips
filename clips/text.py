"""Deterministic text spans and position-based identity mapping."""

import hashlib
import re
from dataclasses import dataclass, replace
from uuid import uuid4

POLICY = "clips-text-v1"
LIMIT = 240
SENTENCE = re.compile(r"""[.!?…]+[»”"')\]]*(?=\s)""")
TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    segment_id: str | None = None


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: str


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def segment(text: str) -> list[Span]:
    """Return exact coverage in Unicode code points. Speech has an empty ID until assigned."""
    spans = []
    offset = 0
    cursor = 0
    for line in text.split("\n"):
        first = len(line) - len(line.lstrip())
        last = len(line.rstrip())
        while first < last:
            stop = last
            if last - first > LIMIT:
                window = line[first : first + LIMIT + 1]
                sentences = [m.end() for m in SENTENCE.finditer(window) if m.end() <= LIMIT]
                words = [m.end() for m in TOKEN.finditer(window) if m.end() <= LIMIT]
                stop = first + (sentences or words or [LIMIT])[-1]
            if cursor < offset + first:
                spans.append(Span(cursor, offset + first))
            cursor = offset + stop
            spans.append(Span(offset + first, cursor, ""))
            first = stop
            while first < last and line[first].isspace():
                first += 1
        offset += len(line) + 1
    if cursor < len(text):
        spans.append(Span(cursor, len(text)))
    return spans


def revise(text: str, spans: list[Span], edits: list[Edit]) -> tuple[str, list[Span]]:
    surviving = [span for span in spans if span.segment_id]
    for edit in edits:
        if not 0 <= edit.start <= edit.end <= len(text):
            raise ValueError("Диапазон правки выходит за пределы текста.")
        inserted = normalize(edit.text)
        if text[edit.start : edit.end] == inserted:
            continue
        delta = len(inserted) - (edit.end - edit.start)
        shifted = []
        for span in surviving:
            if span.end <= edit.start:
                shifted.append(span)
            elif span.start >= edit.end:
                shifted.append(replace(span, start=span.start + delta, end=span.end + delta))
        surviving = shifted
        text = text[: edit.start] + inserted + text[edit.end :]
    identities = {(span.start, span.end): span.segment_id for span in surviving}
    return text, [
        replace(span, segment_id=identities.get((span.start, span.end)) or uuid4().hex)
        if span.segment_id == ""
        else span
        for span in segment(text)
    ]
