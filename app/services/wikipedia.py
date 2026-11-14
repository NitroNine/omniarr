"""Hallmark Channel's premiere schedule from Wikipedia's "List of Hallmark Channel Original Movies (2020s)".

TMDB knows most new Hallmark movies but often not that they're Hallmark's (blank TV-premiere note, outside production
companies, no keywords), so the Countdown to Christmas line-up is read from Wikipedia, which is kept up to date, through
the MediaWiki API (allowed for automated clients that identify themselves). Each title is then matched to TMDB."""
from __future__ import annotations

import re
import time

import httpx

API = "https://en.wikipedia.org/w/api.php"
PAGE = "List of Hallmark Channel Original Movies (2020s)"
_DATE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December) "
                   r"(\d{1,2}), (\d{4})")
_TITLE = re.compile(r"''\s*(?:\[\[(?:[^|\]]*\|)?)?([^'\]]+?)(?:\]\])?\s*''\s*(\(CtC\))?")


def _norm_quotes(t: str) -> str:
    return t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').strip()


def parse(wikitext: str, year: int) -> list[dict]:
    """Movies premiering in `year`: title, date (YYYY-MM-DD), countdown (marked "(CtC)": Countdown to Christmas)."""
    start = re.search(rf"^=={year}==\s*$", wikitext, re.M)
    if not start:
        return []
    section = wikitext[start.end():]
    end = re.search(r"^==[^=]", section, re.M)
    section = section[:end.start()] if end else section
    out = []
    for row in section.split("\n|-"):
        t, d = _TITLE.search(row), _DATE.search(row)
        if not (t and d):
            continue
        date = time.strftime("%Y-%m-%d", time.strptime(d.group(0), "%B %d, %Y"))
        out.append({"title": _norm_quotes(t.group(1)), "date": date, "countdown": bool(t.group(2))})
    return out


async def hallmark_schedule(year: int | None = None) -> list[dict]:
    year = year or int(time.strftime("%Y"))
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Omniarr (Hallmark schedule reader)"}) as c:
        r = await c.get(API, params={"action": "parse", "page": PAGE, "prop": "wikitext", "format": "json",
                                     "formatversion": "2"})
    r.raise_for_status()
    return parse(r.json()["parse"]["wikitext"], year)
