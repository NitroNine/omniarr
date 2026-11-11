from app import decision, db
from app.services.newznab import Release


def _rel(title):
    return Release(title=title, guid=title, link="", size=40 << 30, indexer="FakeIdx", indexer_id=1)


def _check(title, have, score, kind="movie", tier="uhd"):
    prof = decision.profile_for(kind, tier)
    d = decision.evaluate(_rel(title), kind, prof)
    decision._upgrade_check(d, prof, have, score)
    return d


def test_remux_is_not_replaced_by_a_4k_encode(env):
    d = _check("Batman.Begins.2005.2160p.UHD.BluRay.DTS-HD.MA.5.1.DV.x265-W4NK3R", "Remux-2160p", 3005)
    assert not d.accepted and any("already as good" in r for r in d.reasons)
    d = _check("Batman.and.Robin.1997.2160p.UHD.BluRay.TrueHD.7.1.HDR.x265-CtrlHD", "Remux-2160p", -4500)
    assert not d.accepted


def test_remux_1080p_in_4k_library_is_replaced_by_4k_bluray(env):
    d = _check("Movie.2005.2160p.UHD.BluRay.DTS-HD.MA.5.1.HDR.x265-GRP", "Remux-1080p", 0)
    assert "Not better" not in " ".join(d.reasons) and "already as good" not in " ".join(d.reasons)


def test_remux_2160p_in_1080p_library_is_kept(env):
    d = _check("Movie.2005.1080p.BluRay.DTS-HD.MA.5.1.x264-GRP", "Remux-2160p", 0, tier="hd")
    assert not d.accepted


def test_low_quality_disallowed_file_still_upgraded(env):
    d = _check("Movie.2005.2160p.UHD.BluRay.DTS-HD.MA.5.1.HDR.x265-GRP", "DVD", 0)
    assert not any("Not better" in r or "already as good" in r for r in d.reasons)


def test_quality_order():
    assert decision.quality_order("Remux-2160p") < decision.quality_order("Bluray-2160p") < decision.quality_order("WEBDL-2160p")
    assert decision.quality_order("Bluray-1080p Remux") == decision.quality_order("Remux-1080p")


def test_remux_counts_as_done_for_searches(env):
    from app import library
    prof = decision.profile_for("movie", "uhd")
    assert library.cutoff_met(prof, "Remux-2160p", -4500)
    assert not library.cutoff_met(prof, "Remux-1080p", 0)
    assert not library.cutoff_met(prof, "WEBDL-1080p", 0)
    assert library.cutoff_met(decision.profile_for("movie", "hd"), "Remux-1080p", 0)


def test_import_never_replaces_a_better_file(env, monkeypatch):
    import asyncio, os
    from app import importer, library
    from app.services import metadata

    async def details(tmdb_id):
        return {"tmdb_id": 268, "imdb_id": None, "title": "Batman", "original_title": "Batman", "year": 1989,
                "overview": "", "poster": None, "runtime": 126, "release_date": "1989-06-23"}
    monkeypatch.setattr(metadata, "movie_details", details)
    m = asyncio.run(library.add_movie(268, "uhd"))
    d = library.movie_dir(m)
    os.makedirs(d)
    remux = os.path.join(d, "Batman (1989) [Remux-2160p].mkv")
    open(remux, "w").close()
    db.update("movies", m["id"], {"file_path": remux, "file_quality": "Remux-2160p"})
    src = env["tmp"] / "dl" / "Batman.1989.2160p.UHD.BluRay.x265-IAMABLE"
    src.mkdir(parents=True)
    (src / "batman.mkv").write_bytes(b"x" * 1000)
    dl = {"id": 1, "movie_id": m["id"], "release_title": "Batman.1989.2160p.UHD.BluRay.x265-IAMABLE",
          "quality": "Bluray-2160p", "score": 5500, "custom_formats": "[]"}
    try:
        asyncio.run(importer.import_movie(dl, str(src)))
        raise AssertionError("should have refused")
    except RuntimeError as e:
        assert "lower quality" in str(e)
    assert os.path.isfile(remux)
    assert db.one("SELECT file_quality FROM movies WHERE id=?", (m["id"],))["file_quality"] == "Remux-2160p"
