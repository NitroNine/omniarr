from app import db, decision
from app.parser import not_english
from app.services.newznab import Release


def test_names():
    for t in ["Movie.2026.MULTi.1080p.WEB.H264", "Movie.2026.German.DL.1080p.WEB.x264",
              "Movie.2026.1080p.WEB-DL.Multi-Audio.x264", "Movie.2026.1080p.MultiAudio.x264",
              "Movie.2026.DUAL.AUDIO.1080p.x264", "Movie.2026.1080p.HINDI.WEB-DL", "Movie.2026.iTALiAN.1080p.WEB",
              "Show.S01E08.Empire.Day.MULTI.1080p.DSNP.WEB-DL.AAC2.0.x264-AndreMor"]:
        assert not_english(t), t
    for t in ["Movie.2026.1080p.WEB-DL.DDP5.1.H.264-FLUX", "My.Big.Fat.Greek.Wedding.2002.1080p.BluRay.x264",
              "The.French.Dispatch.2021.2160p.WEB-DL", "Show.S01E01.1080p.WEB.h264-NTb", "Movie 2026 1080p WEBDL x264"]:
        assert not_english(t) is None, t


def test_setting_rejects_and_can_be_turned_off(env):
    rel = Release(title="Movie.2026.MULTi.1080p.WEB-DL.H264-GRP", guid="g", link="", size=4 << 30,
                  indexer="FakeIdx", indexer_id=1)
    prof = decision.profile_for("movie", "hd")
    assert any("Not English" in r for r in decision.evaluate(rel, "movie", prof).reasons)
    db.save_settings({"english_only": False})
    assert not any("Not English" in r for r in decision.evaluate(rel, "movie", prof).reasons)


def test_dual_audio_names():
    from app.parser import dual_audio
    for t in ["Movie.2026.DUAL.AUDIO.1080p.x264", "Movie.2026.MULTi.1080p.WEB.H264", "Movie.2026.German.DL.1080p.WEB.x264",
              "Movie.2026.1080p.WEB-DL.Multi-Audio.x264"]:
        assert dual_audio(t), t
    for t in ["Movie.2026.iTALiAN.1080p.WEB", "Movie.2026.1080p.HINDI.WEB-DL", "Movie.2026.German.DUBBED.1080p.x264",
              "Movie.2026.1080p.WEB-DL.DDP5.1.H.264-FLUX"]:
        assert not dual_audio(t), t


def _movie():
    return {"id": 1, "tmdb_id": 1, "tier": "hd", "title": "Movie", "original_title": "Movie", "year": 2026,
            "runtime": 100, "imdb_id": None, "file_quality": None, "file_score": None, "profile": None}


def _rel(t):
    return Release(title=t, guid=t, link="", size=4 << 30, indexer="FakeIdx", indexer_id=1)


def test_dual_only_when_nothing_english(env):
    dual, ita = _rel("Movie.2026.DUAL.1080p.WEB-DL.H264-GRP"), _rel("Movie.2026.iTALiAN.1080p.WEB-DL.H264-GRP")
    eng = _rel("Movie.2026.1080p.WEB-DL.H264-OTHER")
    ds = decision.decide_movie(_movie(), [dual, ita])
    assert [d.release.title for d in ds if d.accepted] == [dual.title]  # only dual available: ok
    ds = decision.decide_movie(_movie(), [dual, eng])
    assert [d.release.title for d in ds if d.accepted] == [eng.title]  # an English-only one wins
    assert not any(d.accepted for d in decision.decide_movie(_movie(), [dual], dual_fallback=False))  # RSS waits


def test_tv_stays_english_only(env):
    sid = db.insert("series", {"tmdb_id": 9, "tvdb_id": 9, "title": "Show", "year": 2026, "tier": "hd", "monitored": 1})
    db.insert("episodes", {"series_id": sid, "season": 1, "episode": 1, "air_date": "2026-01-01", "monitored": 1})
    s = db.one("SELECT * FROM series WHERE id=?", (sid,))
    eps = db.q("SELECT * FROM episodes WHERE series_id=?", (sid,))
    ds = decision.decide_episodes(s, eps, [_rel("Show.S01E01.DUAL.1080p.WEB-DL.H264-GRP")])
    assert not ds[0].accepted and "Not English" in ds[0].reasons[0]
