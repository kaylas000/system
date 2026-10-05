from __future__ import annotations

from pathlib import Path

import pytest

SAMPLE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


@pytest.fixture
def sample_repo() -> Path:
    return SAMPLE_REPO


@pytest.fixture
def sample_files() -> dict[str, str]:
    return {str(f.relative_to(SAMPLE_REPO)): f.read_text() for f in SAMPLE_REPO.rglob("*") if f.is_file()}
