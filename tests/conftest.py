from __future__ import annotations

from pathlib import Path

import pytest

from kernel.config import ArtifactsSection, HITLSection, KernelSection, Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        kernel=KernelSection(default_max_retries=2),
        artifacts=ArtifactsSection(local_dir=tmp_path / "artifacts"),
        hitl=HITLSection(plan_review=False),
    )
