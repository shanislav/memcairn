"""Time expressions inside a recall query ("what did we change last week", "v októbri").

Parsing them on the server means no extra tool parameter, so no extra fixed token cost.
Returns the query without the expression plus a [start, end) date range on recorded/updated.
Slovak, Czech and English; matching is done on diacritics-folded lowercase text.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

# nominative, genitive and locative ("v októbri", "v říjnu") forms, diacritics folded
MONTHS = {
    1: "januar januara januari january leden ledna lednu jan",
    2: "februar februara februari february unor unora unoru feb",
    3: "marec marca marci march brezen brezna breznu mar",
    4: "april aprila aprili duben dubna dubnu apr",
    5: "maj maja maji may kveten kvetna kvetnu",
    6: "jun juna juni june cerven cervna cervnu",
    7: "jul jula juli july cervenec cervence cervenci",
    8: "august augusta auguste srpen srpna srpnu aug",
    9: "september septembra septembri zari sep sept",
    10: "oktober oktobra oktobri october rijen rijna rijnu oct",
    11: "november novembra novembri listopad listopadu nov",
    12: "december decembra decembri prosinec prosince prosinci dec",
}
MONTH_WORDS = {w: m for m, words in MONTHS.items() for w in words.split()}

RELATIVE = [
    (r"\b(today|dnes|dneska)\b", lambda d: (d, d + timedelta(1))),
    (r"\b(yesterday|vcera)\b", lambda d: (d - timedelta(1), d)),
    (r"\b(this week|tento tyzden|tento tyden)\b",
     lambda d: (d - timedelta(d.weekday()), d + timedelta(1))),
    (r"\b(last week|minuly tyzden|minuly tyden|minulom tyzdni|minulem tydnu)\b",
     lambda d: (d - timedelta(d.weekday() + 7), d - timedelta(d.weekday()))),
    (r"\b(this month|tento mesiac|tento mesic|tento mesiaci)\b",
     lambda d: (d.replace(day=1), d + timedelta(1))),
    (r"\b(last month|minuly mesiac|minuly mesic|minulom mesiaci|minulem mesici)\b",
     lambda d: _prev_month(d)),
    (r"\b(last|posledn[ych]+|za posledn[ych]+) (\d{1,3}) (days|dni|dna)\b", None),
]


_ON = r"\b(?:(?:on|dna) )?"
_D = r"([0-3]?\d)(?:st|nd|rd|th|\.)?"
_Y = r"(?:,? ((?:19|20)\d\d))?\b"
DAY_PATTERNS = [  # (regex, group numbers of day, month, year)
    (_ON + _D + r" ([a-z]{3,})" + _Y, (1, 2, 3)),               # 3 June, 2023
    (_ON + r"([a-z]{3,}) " + _D + _Y, (2, 1, 3)),               # June 3, 2023
    (_ON + r"([0-3]?\d)\. ?([01]?\d)\. ?((?:19|20)\d\d)\b", (1, 2, 3)),  # 3.6.2023
]


def fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _prev_month(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    prev = (first - timedelta(1)).replace(day=1)
    return prev, first


def _month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def parse(query: str, today: date | None = None) -> tuple[str, date | None, date | None]:
    """Strip one time expression from `query`; return (rest, start, end) or (query, None, None)."""
    today = today or date.today()
    q = fold(query)
    for pattern, rng in RELATIVE:
        m = re.search(pattern, q)
        if not m:
            continue
        if rng is None:
            days = int(m.group(2))
            start, end = today - timedelta(days), today + timedelta(1)
        else:
            start, end = rng(today)
        return _cut(query, q, m), start, end
    # ISO date or month: 2026-10-01, 2026-10
    m = re.search(r"\b(20\d\d)-([01]\d)(?:-([0-3]\d))?\b", q)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            if m.group(3):
                start = date(y, mo, int(m.group(3)))
                return _cut(query, q, m), start, start + timedelta(1)
            return (_cut(query, q, m), *_month_range(y, mo))
    # one day: "3 June, 2023", "June 3rd 2023", "3. juna 2023", "3.6.2023" (year optional)
    for pattern, dmy in DAY_PATTERNS:
        for m in re.finditer(pattern, q):
            d, mo, y = (m.group(i) for i in dmy)
            month = int(mo) if mo.isdigit() else MONTH_WORDS.get(mo)
            if not month:
                continue
            year = int(y) if y else (today.year if month <= today.month else today.year - 1)
            try:
                start = date(year, month, int(d))
            except ValueError:
                continue
            return _cut(query, q, m), start, start + timedelta(1)
    # month name, optionally with "in/v/vo" before and a year after
    for m in re.finditer(r"\b(?:(?:in|v|vo) )?([a-z]+)(?: (20\d\d))?\b", q):
        month = MONTH_WORDS.get(m.group(1))
        if not month or len(m.group(1)) < 3:
            continue
        if m.group(1) in ("may", "maj", "mar", "jun", "jul", "jan") and not (
                m.group(2) or m.group(0).startswith(("in ", "v ", "vo "))):
            continue  # too ambiguous as plain words ("may" the verb, "jul"...)
        year = int(m.group(2)) if m.group(2) else (
            today.year if month <= today.month else today.year - 1)
        return (_cut(query, q, m), *_month_range(year, month))
    return query, None, None


def _cut(original: str, folded: str, m: re.Match) -> str:
    # folding keeps string length for Latin text, so offsets map back to the original
    if len(original) == len(folded):
        rest = original[:m.start()] + original[m.end():]
    else:
        rest = folded[:m.start()] + folded[m.end():]
    return re.sub(r"\s+", " ", rest).strip(" ,.?!")
