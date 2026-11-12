import asyncio

from app import db, decision, library
from app.services import metadata


def _add(monkeypatch, tier="hd"):
    async def details(tmdb_id):
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": "Alpha", "original_title": "Alpha", "year": 2020,
                "overview": "", "poster": None, "runtime": 90, "release_date": "2020-01-01"}
    monkeypatch.setattr(metadata, "movie_details", details)
    return asyncio.run(library.add_movie(1, tier))


def test_title_profile_overrides_library(client, monkeypatch):
    m = _add(monkeypatch)
    db.save_settings({"quality_overrides": {"movies_hd": {"allowed": ["Bluray-1080p"], "cutoff_score": 5}}})
    lib = decision.profile_for("movie", "hd", item=m)
    choices = client.get("/api/profiles/movie/hd").json()
    assert choices["library"] == "hd-bluray-web" and {"hd-bluray-web", "uhd-bluray-web"} <= {p["key"] for p in choices["profiles"]}

    r = client.put(f"/api/movies/{m['id']}", json={"profile": "uhd-bluray-web"})
    assert r.status_code == 200 and r.json()["profile"] == "uhd-bluray-web"
    row = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    own = decision.profile_for("movie", "hd", item=row)
    base = decision.profile_for("movie", "uhd", apply_overrides=False)
    assert own.name == base.name and own.name != lib.name
    assert own.cutoff_format_score == 5  # the library's score cutoff carries over, its quality list doesn't
    assert [i["name"] for i in own.items if i.get("allowed")] == [i["name"] for i in base.items if i.get("allowed")]

    assert client.put(f"/api/movies/{m['id']}", json={"profile": "nope"}).status_code == 400
    client.put(f"/api/movies/{m['id']}", json={"profile": ""})
    assert db.one("SELECT profile FROM movies WHERE id=?", (m["id"],))["profile"] is None


def test_series_profile(client):
    sid = db.insert("series", {"tmdb_id": 9, "title": "Show", "tier": "hd", "folder": "Show"})
    assert client.put(f"/api/series/{sid}", json={"profile": "web-2160p"}).status_code == 200
    s = db.one("SELECT * FROM series WHERE id=?", (sid,))
    assert decision.profile_for("tv", "hd", item=s).name == decision.profile_for("tv", "uhd", apply_overrides=False).name


def test_bulk_profile(client, monkeypatch):
    async def details(tmdb_id):
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": f"T{tmdb_id}", "original_title": "", "year": 2020,
                "overview": "", "poster": None, "runtime": 90, "release_date": "2020-01-01"}
    monkeypatch.setattr(metadata, "movie_details", details)
    a = asyncio.run(library.add_movie_tiers(1, "both"))
    b = asyncio.run(library.add_movie(2, "hd"))
    prof = lambda: {(m["tmdb_id"], m["tier"]): m["profile"] for m in db.q("SELECT * FROM movies")}
    r = client.post("/api/movies/profile", json={"ids": [a[0]["id"], b["id"]], "profile": "uhd-bluray-web", "tier": "uhd"})
    assert r.json() == {"changed": 1} and prof() == {(1, "hd"): None, (1, "uhd"): "uhd-bluray-web", (2, "hd"): None}
    client.post("/api/movies/profile", json={"ids": [a[1]["id"], b["id"]], "profile": "hd-bluray-web"})
    assert set(prof().values()) == {"hd-bluray-web"}
    client.post("/api/movies/profile", json={"ids": [b["id"]], "profile": ""})
    assert prof()[(2, "hd")] is None
    assert client.post("/api/movies/profile", json={"ids": [b["id"]], "profile": "nope"}).status_code == 400
    sid = db.insert("series", {"tmdb_id": 9, "title": "Show", "tier": "hd", "folder": "Show"})
    assert client.post("/api/series/profile", json={"ids": [sid], "profile": "web-2160p"}).json() == {"changed": 1}


def test_remux_only_profiles(client):
    import copy
    from app import trash
    st = trash.ensure_loaded()
    base = copy.deepcopy(st.profiles["radarr"]["uhd-bluray-web"])
    base["name"] = "Remux + WEB 2160p"
    st.profiles["radarr"]["remux-web-2160p"] = base
    st._add_remux_only()
    keys = {p["key"]: p["name"] for p in client.get("/api/profiles/movie/uhd").json()["profiles"]}
    assert keys["remux-only-2160p"] == "Remux only 2160p" and "remux-only-1080p" not in keys  # no 1080p base here
    p = st.build_profile("movie", "remux-only-2160p")
    assert [i["name"] for i in p.items if i.get("allowed")] == ["Remux-2160p"] and p.cutoff == "Remux-2160p"
    assert p.quality_rank("WEBDL-2160p") is None and p.quality_rank("Remux-2160p") is not None
    assert p.format_scores == st.build_profile("movie", "remux-web-2160p").format_scores


def test_add_with_profile(client, monkeypatch):
    async def details(tmdb_id):
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": "Alpha", "original_title": "Alpha", "year": 2020,
                "overview": "", "poster": None, "runtime": 90, "release_date": "2020-01-01"}
    monkeypatch.setattr(metadata, "movie_details", details)
    r = client.post("/api/movies", json={"tmdb_id": 1, "tier": "both", "search": False, "profile": "uhd-bluray-web"})
    assert r.status_code == 200
    assert {m["tier"]: m["profile"] for m in db.q("SELECT * FROM movies")} == {"hd": "uhd-bluray-web", "uhd": "uhd-bluray-web"}
    assert client.post("/api/movies", json={"tmdb_id": 2, "profile": "nope"}).status_code == 400
    assert not db.q("SELECT 1 FROM movies WHERE tmdb_id=2")  # nothing added on a bad profile


def test_searched_on_add_whatever_the_dates(client, monkeypatch):
    import datetime
    from app import search
    today = datetime.date.today()

    async def details(tmdb_id):
        rd = (today - datetime.timedelta(days=30 if tmdb_id == 1 else -30)).isoformat()
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": f"T{tmdb_id}", "original_title": "", "year": today.year,
                "overview": "", "poster": None, "runtime": 90, "release_date": rd}
    monkeypatch.setattr(metadata, "movie_details", details)
    searched = []

    async def fake_search(mid, auto_grab=True):
        searched.append(mid)
        return []
    monkeypatch.setattr(search, "search_movie", fake_search)
    a = client.post("/api/movies", json={"tmdb_id": 1, "tier": "hd"}).json()  # in cinemas a month, not home yet
    b = client.post("/api/movies", json={"tmdb_id": 2, "tier": "hd"}).json()  # not out anywhere
    m = db.one("SELECT * FROM movies WHERE id=?", (a["id"],))
    assert not library.movie_available(m)
    assert a["id"] in searched and b["id"] in searched
