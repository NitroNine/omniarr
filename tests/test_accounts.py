from fastapi.testclient import TestClient

from app import auth, db
from app.main import app
from app.services import metadata


def _fake_meta(monkeypatch):
    async def movie(tid):
        return {"tmdb_id": tid, "imdb_id": None, "title": f"Movie {tid}", "original_title": "", "year": 2020,
                "overview": "", "poster": None, "runtime": 100, "release_date": "2020-01-01",
                "collection_id": None, "collection_name": None}

    async def series(tvdb):
        return {"tvdb_id": tvdb, "title": f"Show {tvdb}", "year": 2019, "overview": "", "poster": None,
                "imdb_id": None, "tmdb_id": None, "runtime": 30, "status": "Ended", "series_type": "standard"}

    async def eps(_):
        return []
    monkeypatch.setattr(metadata, "movie_details", movie)
    monkeypatch.setattr(metadata, "series_details", series)
    monkeypatch.setattr(metadata, "series_episodes", eps)


def _login(name, pw):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return c


def test_first_run_setup(env):
    auth._failures.clear()
    c = TestClient(app)
    assert c.get("/api/auth/status").json() == {"setup_needed": True, "user": None}
    assert c.get("/api/movies").status_code == 401
    assert c.post("/api/auth/setup", json={"username": "boss", "password": "short"}).status_code == 400
    r = c.post("/api/auth/setup", json={"username": "boss", "password": "longenough1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert c.get("/api/movies").status_code == 200  # setup logs you in
    assert TestClient(app).post("/api/auth/setup", json={"username": "x", "password": "longenough1"}).status_code == 403
    # passwords are hashed, never stored in clear
    assert "longenough1" not in db.one("SELECT password_hash FROM users")["password_hash"]


def test_login_logout_and_throttle(env):
    auth._failures.clear()
    auth.create_user("boss", "longenough1", role="admin")
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "boss", "password": "nope"}).status_code == 403
    c = _login("BOSS", "longenough1")  # usernames are case-insensitive
    assert c.get("/api/auth/me").json()["username"] == "boss"
    c.post("/api/auth/logout")
    assert c.get("/api/movies").status_code == 401
    bad = TestClient(app)
    for _ in range(10):
        bad.post("/api/auth/login", json={"username": "boss", "password": "nope"})
    r = bad.post("/api/auth/login", json={"username": "boss", "password": "longenough1"})
    assert r.status_code == 403 and "Too many" in r.json()["detail"]
    auth._failures.clear()


def test_user_permissions_and_approval(client, monkeypatch):
    _fake_meta(monkeypatch)
    auth._failures.clear()
    r = client.post("/api/users", json={"username": "kid", "password": "kidpassword"})
    assert r.status_code == 200 and not r.json()["auto_approve"] and not r.json()["allow_uhd"]
    kid = _login("kid", "kidpassword")

    # browse yes, admin things no
    assert kid.get("/api/movies").status_code == 200
    for method, path in [("GET", "/api/settings"), ("PUT", "/api/settings"), ("GET", "/api/queue"),
                         ("POST", "/api/movies"), ("DELETE", "/api/movies/1"), ("GET", "/api/users"),
                         ("POST", "/api/tasks/rss"), ("POST", "/api/movies/1/search"), ("GET", "/api/import/scan")]:
        assert kid.request(method, path, json={}).status_code == 403, path

    # 4K needs permission
    r = kid.post("/api/requests", json={"kind": "movie", "tmdb_id": 5, "tier": "uhd"})
    assert r.status_code == 403
    r = kid.post("/api/requests", json={"kind": "movie", "tmdb_id": 5, "tier": "hd", "search": False}).json()
    assert r["status"] == "pending" and r["title"] == "Movie 5"
    assert db.one("SELECT COUNT(*) c FROM movies")["c"] == 0  # nothing added yet
    again = kid.post("/api/requests", json={"kind": "movie", "tmdb_id": 5, "search": False}).json()
    assert again["duplicate"] and again["id"] == r["id"]
    assert client.get("/api/auth/me").json()["pending_requests"] == 1

    # admin approves and upgrades it to the 4K library
    a = client.post(f"/api/requests/{r['id']}/approve", json={"tier": "uhd"}).json()
    assert a["status"] == "approved" and a["decided_by_name"] == "admin"
    m = db.one("SELECT * FROM movies WHERE tmdb_id=5")
    assert m["tier"] == "uhd"
    # becomes "available" once downloaded, and the user sees it
    db.update("movies", m["id"], {"file_path": "/x.mkv", "file_quality": "WEBDL-2160p", "file_score": 0})
    mine = kid.get("/api/requests").json()
    assert [x["status"] for x in mine] == ["available"]
    # users don't see file system paths
    assert kid.get(f"/api/movies/{m['id']}").json()["file_path"] is True
    assert client.get(f"/api/movies/{m['id']}").json()["file_path"] == "/x.mkv"

    # decline with a reason
    r2 = kid.post("/api/requests", json={"kind": "tv", "tvdb_id": 81189, "search": False}).json()
    d = client.post(f"/api/requests/{r2['id']}/decline", json={"note": "Not on this server"}).json()
    assert d["status"] == "declined" and d["note"] == "Not on this server"
    assert db.one("SELECT COUNT(*) c FROM series")["c"] == 0
    # already in the library -> can't request
    assert kid.post("/api/requests", json={"kind": "movie", "tmdb_id": 5}).status_code == 400

    # other users' requests are private
    client.post("/api/users", json={"username": "sis", "password": "sispassword"})
    sis = _login("sis", "sispassword")
    assert sis.get("/api/requests").json() == []
    r3 = kid.post("/api/requests", json={"kind": "movie", "tmdb_id": 6, "search": False}).json()
    assert sis.delete(f"/api/requests/{r3['id']}").status_code == 403
    assert kid.delete(f"/api/requests/{r3['id']}").status_code == 200


def test_auto_approve_user(client, monkeypatch):
    _fake_meta(monkeypatch)
    auth._failures.clear()
    uid = client.post("/api/users", json={"username": "trusted", "password": "trustedpass", "auto_approve": True,
                                          "allow_uhd": True}).json()["id"]
    t = _login("trusted", "trustedpass")
    r = t.post("/api/requests", json={"kind": "tv", "tvdb_id": 81189, "tier": "uhd", "monitor": "future",
                                       "search": False}).json()
    assert r["status"] == "approved" and r["note"] == "Auto-approved"
    s = db.one("SELECT * FROM series WHERE tvdb_id=81189")
    assert s and s["tier"] == "uhd" and r["item_id"] == s["id"]
    # admin switches auto-approve off -> next request waits
    client.put(f"/api/users/{uid}", json={"auto_approve": False})
    assert t.post("/api/requests", json={"kind": "movie", "tmdb_id": 9, "search": False}).json()["status"] == "pending"


def test_admin_safety_rules(client):
    auth._failures.clear()
    me = client.get("/api/auth/me").json()
    assert client.put(f"/api/users/{me['id']}", json={"role": "user"}).status_code == 400  # last admin
    assert client.delete(f"/api/users/{me['id']}").status_code == 400
    uid = client.post("/api/users", json={"username": "kid", "password": "kidpassword"}).json()["id"]
    kid = _login("kid", "kidpassword")
    client.put(f"/api/users/{uid}", json={"enabled": False})
    assert kid.get("/api/movies").status_code == 401  # disabled -> logged out everywhere
    assert TestClient(app).post("/api/auth/login", json={"username": "kid", "password": "kidpassword"}).status_code == 403
    # password change
    client.put(f"/api/users/{uid}", json={"enabled": True, "password": "newkidpass"})
    kid = _login("kid", "newkidpass")
    assert kid.post("/api/auth/password", json={"current": "wrong", "new": "whatever123"}).status_code == 400
    assert kid.post("/api/auth/password", json={"current": "newkidpass", "new": "whatever123"}).status_code == 200


def test_env_password_bootstraps_admin(env, monkeypatch):
    monkeypatch.setenv("OMNIARR_PASSWORD", "fromtemplate")
    monkeypatch.setenv("OMNIARR_USERNAME", "unraid")
    auth.bootstrap_from_env()
    u = db.one("SELECT * FROM users")
    assert u["username"] == "unraid" and u["role"] == "admin"
    auth._failures.clear()
    _login("unraid", "fromtemplate")
