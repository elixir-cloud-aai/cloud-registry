"""Tests for the command of the image (Gunicorn with FOCA's settings)."""

import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import pytest

from tests.gunicorn_settings import DOCKERFILE, image_command, load_settings

APP = "cloud_registry.app:create_app()"


def test_image_command() -> None:
    """The image serves the app with Gunicorn and FOCA's settings.

    Without preloading the app, so that each worker creates it after
    forking (MongoDB clients must not be shared across forks).
    """
    assert image_command()[:3] == [
        "gunicorn",
        "--config",
        "python:foca.gunicorn_config",
    ]
    settings = load_settings()
    assert settings.app_uri == APP
    assert not settings.cfg.preload_app


def test_health_check_port() -> None:
    """The image's health check probes the port Gunicorn listens on."""
    (bind,) = load_settings().cfg.bind
    (url,) = re.findall(r"^ENV HEALTHCHECK_URL=(\S+)$", DOCKERFILE.read_text(), re.M)
    assert urlsplit(url).port == int(bind.rpartition(":")[2])


def test_load_settings_restores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loading the settings restores the arguments and the environment.

    That is, the arguments, `GUNICORN_CMD_ARGS` and the current directory.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 3")
    assert load_settings("--chdir /").cfg.workers == 1
    assert load_settings().cfg.workers == 1
    assert (os.getcwd(), sys.argv) == (str(tmp_path), ["pytest"])
    assert os.environ["GUNICORN_CMD_ARGS"] == "--workers 3"
    monkeypatch.delenv("GUNICORN_CMD_ARGS")
    load_settings("--workers 2")
    assert "GUNICORN_CMD_ARGS" not in os.environ
