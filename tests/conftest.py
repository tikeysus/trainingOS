from __future__ import annotations

import os

import pytest

from trainingos.config import ANTHROPIC_API_KEY_ENV


@pytest.fixture(autouse=True, scope="session")
def _default_anthropic_api_key() -> None:
    """Ensure ANTHROPIC_API_KEY is set so AppConfig.from_env() doesn't
    fail config validation in tests that never reach the network layer
    (in-process AppConfig.from_env() calls and subprocess-spawned CLIs,
    which inherit os.environ)."""
    os.environ.setdefault(ANTHROPIC_API_KEY_ENV, "test-key-placeholder")
