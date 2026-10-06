"""Configure the complete sanea application for integration checks."""

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from pytest_djangoapp import configure_djangoapp_plugin

from sanea import settings as sanea_settings


# Use the file-backed SQLite mode deployed by sanea. A shared in-memory database
# has different locking semantics and cannot represent concurrent clients.
database_path = Path(os.environ["SANEA_DATABASE_PATH"])
sanea_settings.DATABASES["default"]["TEST"] = {
    "NAME": database_path.with_name(f"test-{database_path.name}"),
}

pytest_plugins = configure_djangoapp_plugin(
    settings="sanea.settings",
    app_name="core",
)


@pytest.fixture(scope="session")
def integration_state_dir() -> Path:
    """Return the run-specific state directory prepared by the entry script."""
    path = Path(os.environ["SANECMP_INTEGRATION_STATE_DIR"])
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="module")
def run_script() -> ModuleType:
    path = Path(__file__).parents[1] / "run.py"
    spec = importlib.util.spec_from_file_location("integration_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner_workspace(run_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    template = Path(__file__).parents[1] / ".env.example"
    (root / ".env.example").write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(run_script, "PROJECT_ROOT", root)

    for name in tuple(os.environ):

        if name.startswith(("SANEA_", "SANECMP_")) or name == "PYTHON_ENV":
            monkeypatch.delenv(name)

    return root
