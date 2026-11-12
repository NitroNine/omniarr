import asyncio

from app import db
from app.services import downloader, nzbget


def test_speed_log(env, monkeypatch):
    rates = iter([50 << 20, 70 << 20, 1000, 60 << 20])

    async def status():
        return {"DownloadRate": next(rates), "DownloadPaused": False}
    monkeypatch.setattr(nzbget, "status", status)
    for _ in range(4):
        asyncio.run(downloader.record_speed())
    (row,) = downloader.speed_history()
    assert row["samples"] == 3  # the 1000 B/s tail end isn't counted
    assert row["peak"] == 70 << 20 and row["avg"] == 60 << 20


def test_status_includes_speeds(client, monkeypatch):
    db.ex("INSERT INTO speed_log VALUES('2026-11-12 03:00', 1, 1, 1)")
    assert client.get("/api/downloader/status").json()["speeds"][0]["hour"] == "2026-11-12 03:00"
