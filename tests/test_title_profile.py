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
