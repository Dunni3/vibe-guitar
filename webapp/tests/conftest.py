import functools
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fxweb.catalog import REPO_ROOT, CatalogError, cpp_check_text, find_fxrender, load_catalog
from fxweb.engine import FakeEngine
from fxweb.presets import PresetStore
from fxweb.server import create_app

PRESETS_DIR = REPO_ROOT / "presets"


@pytest.fixture(scope="session")
def fxrender() -> Path:
    try:
        return find_fxrender()
    except CatalogError as e:
        pytest.skip(str(e))


@pytest.fixture(scope="session")
def catalog(fxrender) -> dict:
    return load_catalog(fxrender)


@pytest.fixture
def presets_dir(tmp_path) -> Path:
    d = tmp_path / "presets"
    shutil.copytree(PRESETS_DIR, d)
    return d


@pytest.fixture
def store(presets_dir, catalog, fxrender) -> PresetStore:
    return PresetStore(presets_dir, catalog, cpp_check=functools.partial(cpp_check_text, fxrender))


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(seed=0, xrun_chance=0.0)


@pytest.fixture
def client(store, engine) -> TestClient:
    return TestClient(create_app(store, engine))
