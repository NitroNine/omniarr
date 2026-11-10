import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "trash")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh config dir with the TRaSH fixture subset and a /data tree."""
    from app import db, trash
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copytree(FIXTURES, cfg / "trash")
    db.reset_for_tests(str(cfg / "omniarr.db"))
    trash.STORE.loaded = False
    trash.STORE.reload()
    data = tmp_path / "data"
    roots = {k: str(data / "media" / v) for k, v in
             {"root_movies_hd": "movies", "root_movies_uhd": "movies-4k",
              "root_tv_hd": "tv", "root_tv_uhd": "tv-4k"}.items()}
    db.save_settings({**roots, "import_mode": "move", "tmdb_api_key": "x", "tvdb_api_key": "x"})
    db.insert("indexers", {"name": "FakeIdx", "url": "http://idx", "apikey": "k"})
    yield {"tmp": tmp_path, "data": data}
