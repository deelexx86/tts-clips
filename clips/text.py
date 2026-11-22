"""Deterministic text spans and position-based identity mapping."""

import hashlib
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from unicodedata import category
from uuid import uuid4

LEGACY_POLICY = "clips-text-v1"
POLICY = "clips-text-v2"
LEGACY_LIMIT = 240
TARGET_MIN, TARGET, LIMIT = 400, 500, 600
PARAGRAPH_LIMIT = 4
SENTENCE = re.compile(r"""[.!?…]+[»”"')\]]*(?=\s)""")
TOKEN = re.compile(r"\S+")
_SENTENCE_END = re.compile(r"""(?<![.!?…])([.!?…]++)[»”’"')\]}]*+(?=\s|$)""")
_ABBREVIATION = re.compile(
    r"(?<!\w)(?:гг?|ул|пер|д|кв|стр|рис|им|тов|проф|акад|см|напр|руб|коп|млн|млрд|"
    r"тыс|мин|сек|обл|пос|св|ред|п|т|е|др|пр)\.$", re.IGNORECASE
)
_INITIAL = re.compile(r"(?<!\w)[А-ЯЁA-Z]\.$")
_FINAL_ABBREVIATION = re.compile(r"(?<!\w)(?:и\s+др|и\s+пр|т\.\s*[дп])\.$", re.IGNORECASE)
_ATTRIBUTION = re.compile(r"[—–-][^\S\r\n]*[а-яё]")
_LINE_BREAK = r"(?:\r\n|\r(?!\n)|(?<!\r)\n)"
_PARAGRAPH = re.compile(rf"(?<=\S)[^\S\r\n]*{_LINE_BREAK}[^\S\r\n]*{_LINE_BREAK}\s*")
_WORD_END = re.compile(r"\S(?=\s)")


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


def _legacy_spans(text: str) -> list[Span]:
    spans = []
    offset = 0
    cursor = 0
    for line in text.split("\n"):
        first = len(line) - len(line.lstrip())
        last = len(line.rstrip())
        while first < last:
            stop = last
            if last - first > LEGACY_LIMIT:
                window = line[first : first + LEGACY_LIMIT + 1]
                sentences = [m.end() for m in SENTENCE.finditer(window) if m.end() <= LEGACY_LIMIT]
                words = [m.end() for m in TOKEN.finditer(window) if m.end() <= LEGACY_LIMIT]
                stop = first + (sentences or words or [LEGACY_LIMIT])[-1]
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


def sentence_ends(text: str) -> list[int]:
    """Likely sentence ends, including punctuation that closes quotes or brackets."""
    ends = []
    for match in _SENTENCE_END.finditer(text):
        following = match.end()
        while following < len(text) and text[following].isspace():
            following += 1
        if following < len(text):
            if _ATTRIBUTION.match(text, following):
                continue
            punct = match[1]
            if punct == ".":
                left = text[max(0, match.end(1) - 20):match.end(1)]
                if _INITIAL.search(left):
                    continue
                if _ABBREVIATION.search(left) and not (
                    _FINAL_ABBREVIATION.search(left) and text[following].isupper()
                ):
                    continue
            if text[following].islower() and (punct in ("...", "…") or match.end(1) < match.end()):
                continue
        ends.append(match.end())
    return ends


def _near_target(start: int, candidates: list[int], paragraphs: set[int]) -> int:
    # Give a nearby paragraph edge a small preference over an equally natural sentence edge.
    return min(candidates, key=lambda end: (abs(end - start - TARGET) - 35 * (end in paragraphs), -end))


def _hard_break(text: str, start: int, limit: int) -> int:
    # Avoid cutting common combining and joined emoji sequences when there is room.
    stop = limit
    while stop > start:
        following = text[stop]
        if (category(following).startswith("M") or following == "\u200d" or
            text[stop - 1] == "\u200d" or "\U0001f3fb" <= following <= "\U0001f3ff"):
            stop -= 1
        else:
            break
    return stop if stop > start else limit


def _natural_spans(text: str) -> list[Span]:
    last = len(text.rstrip())
    sentences = sentence_ends(text)
    paragraphs = [match.start() for match in _PARAGRAPH.finditer(text)]
    paragraph_edges = set(paragraphs)
    words = [match.end() for match in _WORD_END.finditer(text)]
    spans = []
    cursor = 0
    while cursor < len(text):
        start = cursor
        while start < len(text) and text[start].isspace():
            start += 1
        if cursor < start:
            spans.append(Span(cursor, start))
        if start == len(text):
            break

        first_paragraph = bisect_right(paragraphs, start)
        first_sentence = bisect_right(sentences, start)
        cap_index = first_paragraph + PARAGRAPH_LIMIT - 1
        limit = min(start + LIMIT, last)
        if cap_index < len(paragraphs) and paragraphs[cap_index] < limit:
            cap = paragraphs[cap_index]
            # A complete sentence can span several short paragraphs.
            if first_sentence < len(sentences) and cap < sentences[first_sentence] <= limit:
                limit = sentences[first_sentence]
            else:
                limit = cap
        if limit == last:
            stop = last
        else:
            complete = sentences[first_sentence:bisect_right(sentences, limit)]
            edges = paragraphs[first_paragraph:bisect_right(paragraphs, limit)]
            # A paragraph without final punctuation can still be a useful boundary.
            in_band = [end for end in complete if end >= start + TARGET_MIN]
            paragraph_band = [end for end in edges if end >= start + TARGET_MIN]
            if in_band:
                stop = _near_target(start, in_band, paragraph_edges)
            elif complete:
                stop = complete[-1]
            elif paragraph_band:
                stop = _near_target(start, paragraph_band, paragraph_edges)
            elif edges:
                stop = edges[-1]
            else:
                left = bisect_left(words, start + TARGET_MIN)
                right = bisect_right(words, limit)
                if left < right:
                    stop = _near_target(start, words[left:right], set())
                elif right and words[right - 1] > start:
                    stop = words[right - 1]
                else:
                    stop = _hard_break(text, start, limit)
        spans.append(Span(start, stop, ""))
        cursor = stop
    return spans


def segment(text: str, policy: str = POLICY) -> list[Span]:
    """Cover the source in Unicode code points. Speech IDs are assigned on publication."""
    if policy == LEGACY_POLICY:
        return _legacy_spans(text)
    if policy == POLICY:
        return _natural_spans(text)
    raise ValueError(f"Неизвестная политика разбиения: {policy}")


def revise(text: str, spans: list[Span], edits: list[Edit], policy: str = POLICY) -> tuple[str, list[Span]]:
    surviving = [span for span in spans if span.segment_id]
    for edit in edits:
        if not 0 <= edit.start <= edit.end <= len(text):
            raise ValueError("Диапазон правки выходит за пределы текста.")
        inserted = normalize(edit.text) if policy == LEGACY_POLICY else edit.text
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
        for span in segment(text, policy)
    ]
