"""A release folder packed inside the outer archive: unpack the inner RAR set, then find the video."""
import asyncio
import os
import stat

from app import importer
from app.services import downloader


def test_unpacks_inner_rar_set(env, tmp_path, monkeypatch):
    tool = tmp_path / "nzbget"
    tool.mkdir()
    fake = tool / "unrar"  # stand-in: "x … <archive> <dest>/" writes a movie into dest and logs the archive
    fake.write_text('#!/bin/sh\nfor a; do :; done\necho "$5" >> "' + str(tmp_path / "calls") + '"\n'
                    'head -c 2048 /dev/zero > "${a}movie.mkv"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(downloader, "APP_DIR", str(tool))
    inner = tmp_path / "dl" / "Snowbound.2026.1080p.WEB-EDITH" / "Snowbound.2026.1080p.WEB-EDITH"
    (inner / "Sample").mkdir(parents=True)
    for f in ("x.rar", "x.r00", "x.r01", "x.nfo", "Sample/x-sample.rar", "y.part1.rar", "y.part02.rar"):
        (inner / f).write_bytes(b"r")
    root = str(tmp_path / "dl" / "Snowbound.2026.1080p.WEB-EDITH")
    assert importer.video_files(root) == []
    assert asyncio.run(importer.unpack_nested(root))
    calls = sorted(os.path.basename(c) for c in (tmp_path / "calls").read_text().split())
    assert calls == ["x.rar", "y.part1.rar"]  # first volumes only, never the sample
    assert [os.path.basename(v) for v in importer.video_files(root)] == ["movie.mkv"]


def test_no_unrar_no_crash(env, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(importer.shutil, "which", lambda _: None)
    (tmp_path / "a.rar").write_bytes(b"r")
    assert not asyncio.run(importer.unpack_nested(str(tmp_path)))
