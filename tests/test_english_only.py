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
