"""Settings behave correctly for a fresh clone (``cp .env.example .env``)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROBE = (
    "import django; django.setup(); "
    "from django.conf import settings; print(bool(settings.SECRET_KEY))"
)


def run_settings(**env: str) -> subprocess.CompletedProcess[str]:
    """Load the real settings in a clean subprocess with the given environment."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("SECRET_KEY", "DEBUG"))}
    environment.update(DJANGO_SETTINGS_MODULE="config.settings", **env)
    return subprocess.run(
        [sys.executable, "-c", PROBE], cwd=REPO, env=environment, capture_output=True, text=True
    )


def test_empty_secret_key_uses_dev_key_in_debug() -> None:
    result = run_settings(SECRET_KEY="", DEBUG="True")  # exactly what .env.example contains
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize("secret", ["", "   "])
def test_missing_secret_key_is_an_error_without_debug(secret: str) -> None:
    result = run_settings(SECRET_KEY=secret.strip(), DEBUG="False")
    assert result.returncode != 0
    assert "SECRET_KEY must be set" in result.stderr
