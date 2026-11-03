from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "src" / "xplanlens" / "samples"
FIXTURES = ROOT / "tests" / "fixtures"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture
def sample():
    return lambda name: _read(SAMPLES / name)


@pytest.fixture
def fixture():
    return lambda name: _read(FIXTURES / name)
