from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import auth, db, library
from app.main import app
from app.services import metadata

RELEASES = {"results": [
    {"iso_3166_1": "US", "release_dates": [
        {"type": 1, "release_date": "2024-02-10T00:00:00.000Z", "certification": ""},
        {"type": 3, "release_date": "2024-03-01T00:00:00.000Z", "certification": "PG-13"},
        {"type": 4, "release_date": "2024-04-16T00:00:00.000Z"},
        {"type": 5, "release_date": "2024-05-14T00:00:00.000Z"}]},
    {"iso_3166_1": "CA", "release_dates": [
        {"type": 3, "release_date": "2024-02-28T00:00:00.000Z", "certification": "PG"}]},
]}


def test_release_dates_prefers_region_then_us(env):
    d = metadata.release_dates(RELEASES, "CA")
    assert (d["theatrical"], d["theatrical_country"]) == ("2024-02-28", "CA")
    assert (d["digital"], d["digital_country"]) == ("2024-04-16", "US")  # no Canadian digital date -> US
    assert d["physical"] == "2024-05-14" and d["premiere"] == "2024-02-10"
    assert d["certification"] == "PG"
    assert metadata.release_dates(RELEASES, "US")["certification"] == "PG-13"
    assert metadata.release_dates(None, "US")["physical"] is None


def test_available_date():
    assert library.available_date({"digital_release": "2024-04-16", "physical_release": "2024-05-14"}) == "2024-04-16"
    assert library.available_date({"physical_release": "2024-05-14", "release_date": "2024-03-01"}) == "2024-05-14"
    assert library.available_date({"release_date": "2024-03-01"}) == "2024-05-30"  # cinema + 90 days
    assert library.available_date({}) is None
    soon = (date.today() + timedelta(days=30)).isoformat()
    assert not library.movie_available({"digital_release": soon})
    assert library.movie_available({"digital_release": "2020-01-01"})
    assert library.movie_available({})  # no dates at all: don't hold it back


def test_tv_movie_available_when_it_airs(env):
    """Hallmark-style TV movies only have a TV premiere (type 6): searchable that day, not 90 days later."""
    d = metadata.release_dates({"results": [{"iso_3166_1": "US", "release_dates": [
        {"type": 6, "release_date": "2026-11-08T00:00:00.000Z"}]}]}, "CA")
    assert metadata.home_date(d) == "2026-11-08"
    assert metadata.home_date({"digital": "2024-04-16", "tv": "2024-06-01"}) == "2024-04-16"
    assert library.available_date({"release_date": "2026-11-08", "digital_release": metadata.home_date(d)}) == "2026-11-08"


def _fake_tmdb(monkeypatch):
    async def tmdb(path, **params):
        if path == "/movie/550":
            return {"id": 550, "imdb_id": "tt0137523", "title": "Fight Club", "original_title": "Fight Club",
                    "release_date": "1999-10-15", "overview": "…", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
                    "genres": [{"name": "Drama"}], "runtime": 139, "vote_average": 8.4, "vote_count": 30000,
                    "status": "Released", "original_language": "en", "budget": 63000000, "revenue": 100853753,
                    "production_companies": [{"name": "Fox 2000 Pictures"}],
                    "credits": {"cast": [{"id": 819, "name": "Edward Norton", "character": "Narrator", "profile_path": "/e.jpg"}],
                                "crew": [{"id": 7467, "name": "David Fincher", "job": "Director", "department": "Directing"}]},
                    "release_dates": RELEASES,
                    "videos": {"results": [{"site": "YouTube", "key": "abc", "type": "Teaser"},
                                           {"site": "YouTube", "key": "xyz", "type": "Trailer", "official": True}]},
                    "watch/providers": {"results": {"US": {"flatrate": [
                        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
                        {"provider_id": 1796, "provider_name": "Netflix Standard with Ads", "logo_path": "/na.png"}]}}}}
        if path == "/tv/1399":
            return {"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17", "last_air_date": "2019-05-19",
                    "status": "Ended", "genres": [], "episode_run_time": [60], "vote_count": 0,
                    "external_ids": {"tvdb_id": 121361, "imdb_id": "tt0944947"},
                    "aggregate_credits": {"cast": [{"id": 22970, "name": "Peter Dinklage", "total_episode_count": 67,
                                                    "roles": [{"character": "Tyrion Lannister"}]}]},
                    "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
                    "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 10}],
                    "number_of_seasons": 8, "number_of_episodes": 73, "created_by": [{"id": 1, "name": "D. B. Weiss"}]}
        raise AssertionError(path)
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    metadata._info_cache.clear()


def test_movie_info_endpoint(client, monkeypatch):
    _fake_tmdb(monkeypatch)
    i = client.get("/api/info/movie/550").json()
    assert i["title"] == "Fight Club" and i["directors"][0]["name"] == "David Fincher"
    assert i["cast"][0] == {"id": 819, "name": "Edward Norton", "role": "Narrator", "photo": metadata.TMDB_FACE + "/e.jpg"}
    assert i["dates"]["physical"] == "2024-05-14" and i["available_date"] == "2024-04-16"
    assert i["trailer"].endswith("xyz")  # official trailer beats a teaser
    assert [s["name"] for s in i["streaming"]] == ["Netflix"]  # ad plan hidden
    assert i["library"] == [] and i["requested"] == []
    mid = db.insert("movies", {"tmdb_id": 550, "title": "Fight Club", "tier": "uhd", "folder": "/x"})
    assert client.get("/api/info/movie/550").json()["library"] == [{"id": mid, "tier": "uhd"}]


def test_tv_info_endpoint_and_user_access(client, monkeypatch):
    _fake_tmdb(monkeypatch)
    sid = db.insert("series", {"tvdb_id": 121361, "title": "Game of Thrones", "tier": "hd", "folder": "/x"})
    i = client.get("/api/info/tv/1399").json()
    assert i["certification"] == "TV-MA" and i["cast"][0]["episodes"] == 67
    assert i["library"] == [{"id": sid, "tier": "hd"}]  # matched by TheTVDB id from TMDB
    # requesters can open title pages, and see their own pending requests
    auth._failures.clear()
    auth.create_user("kid", "kidpassword")
    kid = TestClient(app)
    kid.post("/api/auth/login", json={"username": "kid", "password": "kidpassword"})
    uid = db.one("SELECT id FROM users WHERE username='kid'")["id"]
    db.insert("requests", {"user_id": uid, "kind": "movie", "target_id": 550, "tier": "hd", "title": "Fight Club"})
    assert kid.get("/api/info/movie/550").json()["requested"] == ["hd"]
    assert kid.get("/api/info/tv/1399").status_code == 200
    assert kid.get("/api/info/person/1").status_code in (400, 403)


def test_ui_is_revalidated(env):
    c = TestClient(app)
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert c.get("/").headers["cache-control"] == "no-cache"
