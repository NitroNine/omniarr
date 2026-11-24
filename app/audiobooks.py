"""Audiobooks: pick an edition from Audible, find it on the indexers, download it with NZBGet and file it into
the audiobooks folder as <author>/<series>/Book NN - <title> (or <author>/<title>), with the cover.

Release matching follows Spokarr (https://github.com/BlugDeg/Spokarr, MIT licence, src/indexers.mjs and
src/util.mjs): every release is scored on title, narrator, author, language, abridged vs. unabridged,
dramatisations and size per minute of the book."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import unicodedata

import httpx

from . import db
from .services import audible, newznab, nzbget

AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
JUNK = re.compile(r"\.(par2|nzb|sfv|srr|nfo|url|torrent|exe|lnk|txt|sample)$|(^|/)\._", re.I)
ROOT_GUESSES = ("/data/media/audiobooks", "/data/media/Audiobooks", "/data/audiobooks", "/data/Audiobooks", "/audiobooks")
REASONS = {
    "title_mismatch": "Title doesn't match the book", "wrong_language": "Another language",
    "drama": "Dramatised / full-cast version", "abridged": "Abridged", "too_small": "Too small for the book's length",
    "too_large": "Unusually large for the book's length", "blocklisted": "Blocklisted", "low_score": "Score too low",
}


# ------------------------------------------------------------------ names --
def translit(s: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return s


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", translit(str(s or "")))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# edition notes that never appear in release names ("Narrated by …", "(Unabridged)", "Gesprochen von …")
_EDITION = re.compile(r"\s*[-–:|]\s*(narrated|read by|unabridged|abridged|dramati[sz]ed|gesprochen|gelesen|ungekürzt|"
                      r"gekürzt|die ungekürzte|das hörspiel|hörspiel|hörbuch|lu par|letto da|narrado).*$", re.I)


def clean_title(t: str | None) -> str:
    t = _EDITION.sub("", str(t or ""))
    t = re.sub(r"\s*[(\[][^)\]]*[)\]]\s*", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def safe_name(s: str | None) -> str:
    """A folder name: ': ' becomes ' - ' (like Theremin's .m4b names), no characters Windows/SMB can't take."""
    s = re.sub(r"\s*:\s*", " - ", str(s or ""))
    s = re.sub(r'[\\/*?"<>|]+', " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")[:120] or "Unknown"


def _seq(seq: str) -> str:
    """Series number as in the folder names already there: 1 → 01, 2.5 → 02.5."""
    m = re.fullmatch(r"(\d+)(\.\d+)?", (seq or "").strip())
    return f"{int(m.group(1)):02d}{m.group(2) or ''}" if m else (seq or "").strip()


def relative_folder(book: dict) -> str:
    author = safe_name((book["authors"] or ["Unknown"])[0])
    title = safe_name(clean_title(book["title"]))
    s = next((x for x in book["series"] if x.get("title")), None)
    if s and s.get("seq"):
        return os.path.join(author, safe_name(s["title"]), f"Book {_seq(s['seq'])} - {title}")
    return os.path.join(author, title)


# --------------------------------------------------------------- library --
def root() -> str:
    r = (db.setting("root_audiobooks") or "").strip()
    if r:
        return r.rstrip("/")
    return next((g for g in ROOT_GUESSES if os.path.isdir(g)), ROOT_GUESSES[0])


_ASIN = re.compile(r"\b(B0[0-9A-Z]{8})\b")
_index: dict = {"at": 0.0, "root": None, "keys": {}, "asins": {}}


def _folder_key(name: str) -> str:
    """'Expeditionary Force - Book 03 - Paradise (Unabridged)' / 'Book 01 - Skyward' / 'Project Hail Mary B08GBHW6J1'
    → the title alone, normalised."""
    name = _ASIN.sub("", name)
    name = re.sub(r"\s*[(\[](un)?abridged[)\]]", "", name, flags=re.I)
    name = re.sub(r"^.*?\bbook\s*\d+(\.\d+)?\s*-\s*", "", name, flags=re.I)
    return norm(clean_title(name))


def library_index(force: bool = False) -> dict:
    """Book folders already in the audiobooks folder (3 levels deep), by title and by an ASIN in the name.
    Read again after 5 minutes."""
    r = root()
    if not force and _index["root"] == r and time.time() - _index["at"] < 300:
        return _index
    keys: dict[str, str] = {}
    asins: dict[str, str] = {}
    if os.path.isdir(r):
        for dp, dns, _fns in os.walk(r):
            depth = dp[len(r):].count(os.sep)
            if depth >= 3:
                dns[:] = []
                continue
            dns[:] = [d for d in dns if not d.startswith(".")]
            for d in dns:
                rel = os.path.relpath(os.path.join(dp, d), r)
                k = _folder_key(d)
                if k:
                    keys.setdefault(k, rel)
                m = _ASIN.search(d)
                if m:
                    asins[m.group(1)] = rel
    _index.update(at=time.time(), root=r, keys=keys, asins=asins)
    return _index


def found_in_folder(book: dict) -> str | None:
    """The folder (relative to the audiobooks folder) that already holds this book, if any."""
    ix = library_index()
    if book.get("asin") in ix["asins"]:
        return ix["asins"][book["asin"]]
    return ix["keys"].get(norm(clean_title(book["title"])))


def _row(r: dict) -> dict:
    for k in ("authors", "narrators", "series"):
        try:
            r[k] = json.loads(r[k] or "[]")
        except (TypeError, json.JSONDecodeError):
            r[k] = []
    r["abridged"] = bool(r.get("abridged"))
    return r


def get(book_id: int) -> dict | None:
    r = db.one("SELECT * FROM audiobooks WHERE id=?", (book_id,))
    return _row(r) if r else None


def active_download(book_id: int) -> dict | None:
    return db.one("SELECT * FROM downloads WHERE kind='audiobook' AND audiobook_id=? "
                  "AND status IN ('queued','downloading','importing') ORDER BY id DESC", (book_id,))


def list_all() -> list[dict]:
    dls = {d["audiobook_id"]: d for d in db.q(
        "SELECT audiobook_id, status, progress FROM downloads WHERE kind='audiobook' "
        "AND status IN ('queued','downloading','importing') ORDER BY id")}
    out = []
    for r in db.q("SELECT * FROM audiobooks ORDER BY added DESC, id DESC"):
        r = _row(r)
        d = dls.get(r["id"])
        r["download"] = {"status": d["status"], "progress": d["progress"]} if d else None
        out.append(r)
    return out


def library_state(asins: list[str]) -> dict[str, dict]:
    """For search results: which books are tracked (and their state) or already sit in the folder."""
    rows = {r["asin"]: r for r in db.q(f"SELECT id, asin, folder FROM audiobooks WHERE asin IN ({','.join('?' * len(asins))})",
                                       tuple(asins))} if asins else {}
    return {a: {"id": r["id"], "have": bool(r["folder"])} for a, r in rows.items()}


async def add(asin: str, search: bool = True) -> dict:
    asin = (asin or "").strip().upper()
    if not asin:
        raise ValueError("Missing ASIN")
    existing = db.one("SELECT id FROM audiobooks WHERE asin=?", (asin,))
    if existing:
        raise ValueError("That audiobook is already in Omniarr")
    b = await audible.get_book(asin)
    folder = found_in_folder(b)
    bid = db.insert("audiobooks", {
        "asin": asin, "title": b["title"], "subtitle": b["subtitle"], "authors": json.dumps(b["authors"]),
        "narrators": json.dumps(b["narrators"]), "minutes": b["minutes"], "abridged": int(b["abridged"]),
        "release_date": b["release_date"], "series": json.dumps(b["series"]), "cover": b["cover"],
        "language": b["language"], "publisher": b["publisher"], "summary": b["summary"], "region": audible.region(),
        "folder": folder,
    })
    if folder:
        db.log(f"Added audiobook {b['title']} — already in the audiobooks folder ({folder})")
    else:
        db.log(f"Added audiobook {b['title']}")
        if search:
            asyncio.create_task(_search_logged(bid))
    return get(bid)


async def _search_logged(book_id: int) -> None:
    try:
        await search_book(book_id)
    except newznab.IndexerBusy:
        pass  # the backlog search tries again once the indexer is back
    except Exception as e:  # noqa: BLE001
        db.log(f"Audiobook search failed: {e}", "warn")


def remove(book_id: int, delete_files: bool = False) -> None:
    b = get(book_id)
    if not b:
        return
    if delete_files and b["folder"]:
        path = os.path.realpath(os.path.join(root(), b["folder"]))
        if path.startswith(os.path.realpath(root()) + os.sep) and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            db.log(f"Deleted {path}")
    db.ex("DELETE FROM audiobooks WHERE id=?", (book_id,))
    library_index(force=True)


# ---------------------------------------------------------------- scoring --
LANG_TOKENS = {
    "german": {"german", "deutsch", "ger"}, "english": {"english", "eng"},
    "french": {"french", "francais", "truefrench", "vf"}, "italian": {"italian", "italiano", "ita"},
    "spanish": {"spanish", "espanol", "spa", "castellano"}, "japanese": {"japanese", "jap", "jpn"},
}
_ENGLISH_WORDS = re.compile(r"\b(the|and|of)\b")
_DRAMA = re.compile(r"\bhoerspiel\b|full cast|dramati[sz](?:ed|ation)|audio drama|radio (?:drama|play|adapt\w*)|\bbbc radio\b")


def score(book: dict, rel: newznab.Release) -> dict:
    n = norm(rel.title)
    words = set(n.split())
    title_words = [w for w in norm(clean_title(book["title"])).split() if len(w) > 1]
    hit = sum(1 for w in title_words if w in words) / max(1, len(title_words))
    plus, minus = [], []
    s = round(hit * 60)
    if hit < 0.75:
        minus.append("title_mismatch")
    narr = [w for x in book["narrators"] for w in norm(x).split() if len(w) > 2]
    if narr and any(w in words for w in narr):
        s += 25
        plus.append("narrator")
    if any(w in words for x in book["authors"] for w in norm(x).split() if len(w) > 2):
        s += 5
        plus.append("author")
    lang = book.get("language") or audible.language()
    named = [lg for lg, toks in LANG_TOKENS.items() if toks & words]
    looks_english = "english" in named or (not named and bool(_ENGLISH_WORDS.search(n)))
    if lang in named:
        s += 10
        plus.append("language")
    elif (lang != "english" and looks_english) or any(lg != lang for lg in named):
        s -= 50
        minus.append("wrong_language")
    if _DRAMA.search(n) and not _DRAMA.search(norm(f"{book['title']} {book.get('subtitle') or ''}")):
        s -= 30
        minus.append("drama")
    unabridged = bool(re.search(r"\bungekuerzt\b|\bunabridged\b", n))
    abridged = not unabridged and bool(re.search(r"\bgekuerzt\b|\babridged\b", n))
    if unabridged and not book["abridged"]:
        s += 5
        plus.append("unabridged")
    if abridged and not book["abridged"]:
        s -= 25
        minus.append("abridged")
    if book.get("minutes") and rel.size:
        mb_per_min = rel.size / 1048576 / book["minutes"]
        if mb_per_min < 0.15:
            s -= 30
            minus.append("too_small")
        elif mb_per_min > 4:
            s -= 10
            minus.append("too_large")
    ok = hit >= 0.75 and s >= 50 and "wrong_language" not in minus
    if not ok and not minus:
        minus.append("low_score")
    return {"score": s, "ok": ok, "plus": plus, "minus": minus}


def _blocked() -> tuple[set, set]:
    rows = db.q("SELECT guid, release_title FROM blocklist")
    return {r["guid"] for r in rows if r["guid"]}, {r["release_title"] for r in rows}


async def find_releases(book: dict) -> list[dict]:
    base = clean_title(book["title"])
    queries = list(dict.fromkeys(q for q in (base, translit(base)) if q))
    bg, bt = _blocked()

    def ranked(rels):
        out = []
        for r in rels:
            sc = score(book, r)
            if r.guid in bg or r.title in bt:
                sc["ok"] = False
                sc["minus"].append("blocklisted")
            out.append({"title": r.title, "guid": r.guid, "link": r.link, "size": r.size, "indexer": r.indexer,
                        "indexer_id": r.indexer_id, "age_days": round(r.age_days) if r.age_days is not None else None,
                        **sc, "reasons": [REASONS.get(m, m) for m in sc["minus"]]})
        out.sort(key=lambda x: (not x["ok"], -x["score"], -(x["size"] or 0)))
        return out
    rels = await newznab.search_audiobook(queries, enough=lambda rs: any(x["ok"] for x in ranked(rs)))
    return ranked(rels)[:60]


# ---------------------------------------------------------- search + grab --
async def grab(book: dict, rel: dict) -> int:
    from . import search as search_mod
    await search_mod.check_space(rel.get("size") or 0)
    nzb = await newznab.fetch_nzb(rel["link"])
    cat = db.setting("nzbget_category_audiobooks") or "audiobooks"
    nzb_id = await nzbget.add(nzb, rel["title"], cat, dupe_key=f"omniarr-audiobook-{book['id']}", dupe_score=int(rel.get("score") or 0))
    dl_id = db.insert("downloads", {
        "kind": "audiobook", "audiobook_id": book["id"], "episode_ids": "[]", "release_title": rel["title"],
        "indexer": rel.get("indexer"), "guid": rel.get("guid"), "size": rel.get("size"), "quality": "Audiobook",
        "score": int(rel.get("score") or 0), "custom_formats": "[]", "nzbget_id": nzb_id, "status": "queued",
    })
    db.log(f"Grabbed {rel['title']} [audiobook, score {rel.get('score')}] from {rel.get('indexer')}")
    return dl_id


async def search_book(book_id: int, auto_grab: bool = True) -> list[dict]:
    book = get(book_id)
    if not book:
        raise ValueError("Audiobook not found")
    results = await find_releases(book)
    now = db.one("SELECT datetime('now') t")["t"]
    if not auto_grab:
        return results
    if book["folder"] or active_download(book_id):
        db.update("audiobooks", book_id, {"last_search": now})
        return results
    busy = None
    for r in [r for r in results if r["ok"]]:
        if newznab.paused_until(r["link"]):
            busy = busy or newznab.IndexerBusy(r["indexer"], newznab.paused_until(r["link"]))
            continue
        try:
            await grab(book, r)
        except newznab.IndexerBusy as e:
            busy = e
            continue
        db.update("audiobooks", book_id, {"last_search": now})
        return results
    if busy:
        raise busy
    db.update("audiobooks", book_id, {"last_search": now})
    db.log(f"Search for audiobook {book['title']}: {len(results)} results, none matching well enough")
    return results


async def missing_search(limit: int = 5) -> None:
    """Backlog: wanted books never searched, or not searched for a day."""
    from . import search as search_mod
    for b in db.q("SELECT id, last_search FROM audiobooks WHERE monitored=1 AND folder IS NULL AND "
                  "(last_search IS NULL OR last_search < datetime('now','-1 day')) ORDER BY last_search IS NOT NULL, last_search"):
        if limit <= 0 and b["last_search"]:
            break
        if active_download(b["id"]):
            continue
        try:
            await search_book(b["id"])
        except search_mod.HOLD:
            return
        except Exception as e:  # noqa: BLE001
            db.log(f"Audiobook search failed: {e}", "warn")
        if b["last_search"]:
            limit -= 1


# ----------------------------------------------------------------- import --
def audio_files(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path] if os.path.splitext(path)[1].lower() in AUDIO_EXT else []
    return sorted(os.path.join(dp, f) for dp, _dn, fns in os.walk(path) for f in fns
                  if os.path.splitext(f)[1].lower() in AUDIO_EXT and not f.startswith("._"))


def _move_book(src: str, dest: str) -> int:
    """Move the audio files and pictures (keeping CD1/CD2 subfolders) into dest. Returns the bytes moved."""
    files = audio_files(src)
    base = os.path.commonpath([os.path.dirname(f) for f in files]) if os.path.isdir(src) else os.path.dirname(src)
    os.makedirs(dest, exist_ok=True)
    total = 0
    keep = files + [os.path.join(dp, f) for dp, _dn, fns in os.walk(base) for f in fns
                    if os.path.splitext(f)[1].lower() in IMAGE_EXT and not JUNK.search(f)] if os.path.isdir(base) else files
    for f in keep:
        target = os.path.join(dest, os.path.relpath(f, base))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            os.remove(target)
        mode = db.setting("import_mode")
        if mode == "copy":
            shutil.copy2(f, target)
        elif mode == "hardlink":
            try:
                os.link(f, target)
            except OSError:
                shutil.copy2(f, target)
        else:
            shutil.move(f, target)
        total += os.path.getsize(target)
    return total


async def _save_cover(book: dict, dest: str) -> None:
    if not book.get("cover") or any(os.path.splitext(f)[1].lower() in IMAGE_EXT for f in os.listdir(dest)):
        return
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(book["cover"])
        if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
            with open(os.path.join(dest, "cover.jpg" if r.content[:3] == b"\xff\xd8\xff" else "cover.png"), "wb") as f:
                f.write(r.content)
    except httpx.HTTPError:
        pass  # the cover is optional; Theremin finds one online too


async def import_book(dl: dict, src: str) -> None:
    from .importer import unpack_nested
    book = get(dl["audiobook_id"])
    if not book:
        raise RuntimeError("The audiobook was removed from Omniarr")
    if not audio_files(src) and await unpack_nested(src):
        pass
    if not audio_files(src):
        raise RuntimeError(f"No audio files found in {src}")
    rel = book["folder"] or relative_folder(book)
    dest = os.path.join(root(), rel)
    if not book["folder"] and os.path.isdir(dest) and audio_files(dest):  # someone else's book with the same name
        n = 2
        while os.path.isdir(f"{dest} ({n})"):
            n += 1
        dest, rel = f"{dest} ({n})", f"{rel} ({n})"
    size = await asyncio.to_thread(_move_book, src, dest)
    await _save_cover(book, dest)
    db.update("audiobooks", book["id"], {"folder": rel, "file_size": size, "imported": db.one("SELECT datetime('now') t")["t"]})
    library_index(force=True)
    db.log(f"Imported audiobook {book['title']} → {dest}")


def cleanup_source(src: str) -> None:
    if db.setting("import_mode") == "move" and os.path.isdir(src) and not audio_files(src):
        shutil.rmtree(src, ignore_errors=True)
