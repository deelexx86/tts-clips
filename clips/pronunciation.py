"""Explainable lexical review of a saved Russian Whisper transcript."""

from dataclasses import dataclass
import re

from .completion import _near, _words, align_tokens

POLICY = "clips-pronunciation-v1"
_WORD = re.compile(r"(?:[^\W_]\u0301*)+(?:[-‑](?:[^\W_]\u0301*)+)*")
_CYRILLIC = re.compile(r"[а-я]")
_FUNCTION = frozenset("""
а без бы в во вот для до же за и из к как ко ли на над не ни но о об от по под
при про с со так то у уже что чтоб чтобы это этот эта эти был была были есть ещё
""".replace("ё", "е").split())
_NUMERAL = {
    form: value
    for value, forms in (
        (0, "ноль нуль нуля"), (1, "один одна одно одну одного одной первый первого"),
        (2, "два две двух второй второго второму"), (3, "три трех третий третьего"),
        (4, "четыре четырех четвертый четвертого"), (5, "пять пяти пятый пятого пятому"),
        (6, "шесть шести шестой шестого"), (7, "семь семи седьмой седьмого"),
        (8, "восемь восьми восьмой восьмого"), (9, "девять девяти девятый девятого"),
        (10, "десять десяти десятого"), (11, "одиннадцать"), (12, "двенадцать"),
        (13, "тринадцать"), (14, "четырнадцать"), (15, "пятнадцать пятнадцатого пятнадцатому"),
        (16, "шестнадцать"), (17, "семнадцать семнадцати"), (18, "восемнадцать"),
        (19, "девятнадцать"), (20, "двадцать двадцати двадцатого"),
        (30, "тридцать тридцати"), (40, "сорок сорока"), (50, "пятьдесят пятидесяти"),
        (60, "шестьдесят"), (70, "семьдесят"), (80, "восемьдесят"), (90, "девяносто"),
        (100, "сто ста"), (200, "двести"), (300, "триста"), (400, "четыреста"),
        (500, "пятьсот"), (600, "шестьсот"), (700, "семьсот"), (800, "восемьсот"),
        (900, "девятьсот"), (1000, "тысяча тысячу тысячи тысяч"),
        (10**6, "миллион миллиона миллионов"), (10**9, "миллиард миллиарда миллиардов"),
    )
    for form in forms.split()
}
_DIGIT = re.compile(r"(\d+)(?:[-‑](?:й|я|е|го|му|м|х|ми))?", re.IGNORECASE)


@dataclass(frozen=True)
class Token:
    raw: str
    word: str
    start: int
    end: int

    @property
    def digits(self) -> str | None:
        match = _DIGIT.fullmatch(self.raw)
        return match[1] if match else None

    @property
    def number(self) -> str | None:
        if self.digits is not None:
            return self.digits.lstrip("0") or "0"
        value = _NUMERAL.get(self.word)
        return str(value) if value is not None else None

    @property
    def content(self) -> bool:
        return len(self.word) >= 3 and self.word not in _FUNCTION and bool(_CYRILLIC.search(self.word))


def tokens(text):
    return [Token(match[0], _words(match[0])[0], match.start(), match.end()) for match in _WORD.finditer(text)]


def excerpt(text, words):
    start, end = words[0].start, words[-1].end
    return {"start": start, "end": end, "text": text[start:end]}


def finding_reason(expected, heard, left_anchor):
    numbers = [token for token in expected if token.number is not None]
    other_numbers = [token for token in heard if token.number is not None]
    if len(numbers) == len(other_numbers) == 1:
        a, b = numbers[0], other_numbers[0]
        # Compare like representations only. Whisper may rewrite a spoken expression as digits.
        if (a.digits is None) == (b.digits is None) and a.number != b.number:
            return "number_substitution"
    if numbers or other_numbers:
        # Detect an unreadable piece within an otherwise aligned written quantity.
        if not other_numbers and any(token.digits is not None for token in numbers) and (
            left_anchor and left_anchor.digits is not None and any(token.content for token in heard)
        ):
            return "numeric_fragment"
        return None
    if len(expected) == len(heard) == 1:
        a, b = expected[0], heard[0]
        name = a.raw[0].isupper() and b.raw[0].isupper() and left_anchor is not None
        if a.content and b.content and (name or not (_near(a.word, b.word) or _near(b.word, a.word))):
            return "word_substitution"
    return None


def evaluate(expected, heard, language):
    findings = []
    if language != "ru":
        return {"status": "unassessed", "explanation": "Правила рассчитаны на русский текст.", "findings": findings}
    source, spoken = tokens(expected), tokens(heard)
    if not source or not spoken:
        return {"status": "unassessed", "explanation": "Недостаточно слов для сопоставления.", "findings": findings}
    operations = align_tokens([token.word for token in source], [token.word for token in spoken])
    for index, (kind, start, stop, left, right) in enumerate(operations):
        if kind != "replace" or max(stop - start, right - left) > 4:
            continue
        before = operations[index - 1] if index else None
        after = operations[index + 1] if index + 1 < len(operations) else None
        anchors = sum(op[2] - op[1] for op in (before, after) if op and op[0] == "equal")
        if anchors < 2:
            continue
        a, b = source[start:stop], spoken[left:right]
        reason = finding_reason(a, b, source[start - 1] if before and before[0] == "equal" else None)
        if reason:
            findings.append({"code": reason, "expected": excerpt(expected, a), "heard": excerpt(heard, b)})
    if findings:
        return {"status": "review", "explanation": "Распознавание отличается в значимом месте. Проверьте WAV на слух.",
                "findings": findings}
    return {"status": "clear", "explanation": "Правила не нашли значимых подмен в сопоставимых фрагментах.",
            "findings": findings}
