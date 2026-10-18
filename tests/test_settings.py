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


PRINT_KEY = (
    "import django; django.setup(); "
    "from django.conf import settings; print(repr(settings.SECRET_KEY))"
)
DEV_KEY = "'django-insecure-local-dev-only-change-me'"


def run_print_key(**env: str) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("SECRET_KEY", "DEBUG"))}
    environment.update(DJANGO_SETTINGS_MODULE="config.settings", **env)
    return subprocess.run(
        [sys.executable, "-c", PRINT_KEY], cwd=REPO, env=environment, capture_output=True, text=True
    )


def test_empty_secret_key_uses_dev_key_in_debug() -> None:
    result = run_settings(SECRET_KEY="", DEBUG="True")  # exactly what .env.example contains
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize("secret", ["", "   ", "\t \n"])
def test_blank_secret_key_is_an_error_without_debug(secret: str) -> None:
    result = run_settings(SECRET_KEY=secret, DEBUG="False")  # passed through unchanged
    assert result.returncode != 0
    assert "SECRET_KEY must be set" in result.stderr


def test_whitespace_only_secret_key_falls_back_to_dev_key_in_debug() -> None:
    result = run_print_key(SECRET_KEY="   ", DEBUG="True")
    assert result.stdout.strip() == DEV_KEY


@pytest.mark.parametrize("secret", ["$k3y-starting-with-dollar#and!symbols", "abc$HOME"])
def test_secret_key_is_read_literally(secret: str) -> None:
    """django-environ would expand "$..." as a variable reference; we must not."""
    result = run_print_key(SECRET_KEY=secret, DEBUG="False", ALLOWED_HOSTS="example.com")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(secret)


def test_secret_key_from_dotenv_file_is_read_literally(tmp_path) -> None:
    """Same through django-environ's .env loader (the path a real deployment uses)."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("SECRET_KEY=$dollar-first-key\n", encoding="utf-8")
    code = (
        f"import os, environ; environ.Env.read_env({str(dotenv)!r}); "
        "print(repr(os.environ['SECRET_KEY'].strip()))"
    )
    environment = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True, text=True
    )
    assert result.stdout.strip() == repr("$dollar-first-key")
