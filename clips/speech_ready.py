"""Local detection of written forms that need editorial preparation."""

import json
import re

from .text import digest

_NUMBER = r"\d+(?:[ \u00a0\u202f]\d{3}\b)*(?:[.,]\d+)?"
_UNITS = r"км/ч|м/с|км²|м²|м³|см²|мм|см|км|мг|кг|млн|млрд|руб|кВт|МВт|Гц|°[CFС]|[%₽€$]|м|г|л"
_MONTH = r"января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_RULES = (
    ("date", "Раскройте дату словами в нужном падеже.",
     re.compile(rf"(?<!\d)(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}([./-])\d{{1,2}}\1\d{{2,4}}|"
                rf"\d{{1,2}}[ \u00a0]+(?:{_MONTH})\b(?:[ \u00a0]+\d{{4}}\b(?:[ \u00a0]+(?:г\.|года\b))?)?)"
                r"(?!\d)", re.IGNORECASE)),
    ("time", "Запишите время словами.",
     re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*[–—−-][ \t]*\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)")),
    ("measurement", "Запишите величину и единицу измерения словами.",
     re.compile(rf"(?<!\d)[+−-]?{_NUMBER}[ \u00a0\u202f]*(?:{_UNITS})(?!\w)", re.IGNORECASE)),
    ("abbreviation", "Уточните, как произнести сокращение или инициалы.",
     re.compile(
         r"(?<!\w)(?:[А-ЯЁ]\.[ \t]*[А-ЯЁ]\.|"
         r"т\.[ \t]*[дпекн]\.|(?:пн|вт|ср|чт|пт|сб|вс|ул|пер|просп|д|кв|гг?|им|см|руб|коп|тыс|млн|млрд|др|рис|стр)\.|"
         r"МУП|НИИ|КПД|ООО|АО|ИП|СМИ|РФ|США|ЕС|ВОЗ|НДС)(?!\w)", re.IGNORECASE)),
    ("number", "Запишите число словами в нужном падеже.",
     re.compile(rf"[+−-]?{_NUMBER}(?:-(?:й|я|е|го|му|м|х|ми)(?!\w))?", re.IGNORECASE)),
)


def warnings_for(text):
    candidates = [
        (match.start(), match.end(), priority, code, reason)
        for priority, (code, reason, pattern) in enumerate(_RULES)
        for match in pattern.finditer(text)
    ]
    result = []
    for start, end, _, code, reason in sorted(candidates, key=lambda item: (item[0], item[2], -item[1])):
        if not result or result[-1]["end"] <= start:
            result.append({"start": start, "end": end, "text": text[start:end], "code": code, "reason": reason})
    return result


class PreparationRequired(Exception):
    def __init__(self, warnings, confirmation):
        self.detail = {"code": "speech_ready", "warnings": warnings, "confirmation": confirmation}


def require_preparation(clip, spans, confirmation):
    if not spans:
        return
    # Segments and findings both use code-point offsets in the current saved text.
    spans = iter(sorted(spans, key=lambda span: span["start"]))
    span = next(spans)
    warnings = []
    for finding in warnings_for(clip["text"]):
        while span and span["end"] <= finding["start"]:
            span = next(spans, None)
        if not span:
            break
        if span["start"] < finding["end"]:
            warnings.append(finding)
    if warnings:
        token = digest(json.dumps([clip["revision"], warnings], sort_keys=True))
        if confirmation != token:
            raise PreparationRequired(warnings, token)
