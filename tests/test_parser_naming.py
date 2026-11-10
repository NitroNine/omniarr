from app import naming
from app.parser import normalize_title, parse


def test_movie_parse():
    p = parse("Dune.Part.Two.2024.2160p.UHD.BluRay.x265.DV.HDR10.TrueHD.Atmos.7.1-FraMeSToR")
    assert p.resolution == 2160 and p.source == "bluray" and p.year == 2024
    assert p.group == "FraMeSToR" and p.quality_name("movie") == "Bluray-2160p"
    assert p.hdr == "DV HDR10" and p.audio == "TrueHD Atmos" and p.channels == "7.1"
    assert normalize_title(p.clean_name) == normalize_title("Dune: Part Two")


def test_remux_and_web():
    assert parse("Heat.1995.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT").quality_name("movie") == "Remux-1080p"
    assert parse("Heat.1995.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT").quality_name("tv") == "Bluray-1080p Remux"
    assert parse("Heat 1995 1080p AMZN WEB-DL DDP5.1 H.264-FLUX").quality_name() == "WEBDL-1080p"
    assert parse("Heat.1995.720p.WEBRip.x264-GRP").quality_name() == "WEBRip-720p"
    assert parse("Heat.1995.HDCAM.x264-LQ").quality_name() == "CAM"


def test_tv_parse():
    p = parse("The.Bear.S03E01.1080p.HULU.WEB-DL.DDP5.1.H.264-NTb")
    assert (p.season, p.episodes, p.full_season) == (3, [1], False)
    assert normalize_title(p.clean_name) == normalize_title("The Bear")
    p = parse("Show.Name.S01E01-E03.720p.HDTV.x264-KILLERS")
    assert p.episodes == [1, 2, 3] and p.quality_name("tv") == "HDTV-720p"
    p = parse("The.Bear.S02.1080p.WEB.h264-ETHEL")
    assert p.full_season and p.season == 2
    p = parse("The.Daily.Show.2024.05.01.1080p.WEB.h264-GRP")
    assert p.air_date == "2024-05-01" and p.season is None


def test_naming_tokens():
    p = parse("Dune.Part.Two.2024.Extended.2160p.UHD.BluRay.x265.DV.HDR10.TrueHD.Atmos.7.1-FraMeSToR")
    v = {**naming.movie_values({"title": "Dune: Part Two", "year": 2024, "tmdb_id": 693134}),
         **naming.media_values(p, "Bluray-2160p", ["IMAX"])}
    fmt = ("{Movie CleanTitle} {(Release Year)} {tmdb-{TmdbId}} - {edition-{Edition Tags}} {[MediaInfo 3D]}"
           "{[Custom Formats]}{[Quality Full]}{[Mediainfo AudioCodec}{ Mediainfo AudioChannels]}"
           "{[MediaInfo VideoDynamicRangeType]}{[Mediainfo VideoCodec]}{-Release Group}")
    assert naming.render(fmt, v) == ("Dune Part Two (2024) {tmdb-693134} - {edition-Extended} "
                                     "[IMAX][Bluray-2160p][TrueHD Atmos 7.1][DV HDR10][x265]-FraMeSToR")
    # missing edition: wrappers disappear cleanly
    v["edition tags"] = None
    assert naming.render("{Movie CleanTitle} {(Release Year)} - {{Edition Tags}} {[Quality Full]}", v) == \
        "Dune Part Two (2024) [Bluray-2160p]"


def test_episode_naming_multi():
    p = parse("The.Bear.S03E01E02.1080p.WEB-DL.DDP5.1.H.264-NTb")
    v = {**naming.series_values({"title": "The Bear", "year": 2022, "tvdb_id": 1}),
         **naming.episode_values([{"season": 3, "episode": 1, "title": "Tomorrow"},
                                  {"season": 3, "episode": 2, "title": "Next"}]),
         **naming.media_values(p, "WEBDL-1080p", [])}
    out = naming.render_episode("{Series CleanTitleWithoutYear} {(Series Year)} - S{season:00}E{episode:00} - "
                                "{Episode CleanTitle:90} {[Quality Full]}{-Release Group}", v)
    assert out == "The Bear (2022) - S03E01-E02 - Tomorrow + Next [WEBDL-1080p]-NTb"
    assert naming.render("Season {season:00}", v) == "Season 03"
