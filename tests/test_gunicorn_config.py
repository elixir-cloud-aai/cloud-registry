"""Tests for the Gunicorn settings of the image."""

import os
import shlex
import sys

from gunicorn.app.wsgiapp import WSGIApplication
import pytest

from cloud_registry import gunicorn_config
from cloud_registry.gunicorn_config import _worker_tmp_dir

CONFIG = "python:cloud_registry.gunicorn_config"
APP = "cloud_registry.app:create_app()"


def load_settings(monkeypatch, cmd_args: str | None = None) -> WSGIApplication:
    """Load the Gunicorn config as the image's command does, optionally with
    `GUNICORN_CMD_ARGS`.
    """
    monkeypatch.setattr(sys, "argv", ["gunicorn", "--config", CONFIG, APP])
    if cmd_args is None:
        monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    else:
        monkeypatch.setenv("GUNICORN_CMD_ARGS", cmd_args)
    cwd = os.getcwd()
    try:
        return WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]")
    finally:
        os.chdir(cwd)  # Gunicorn changes to `chdir` (the current directory)


def test_settings(monkeypatch):
    """Gunicorn loads the settings, without preloading the app."""
    cfg = load_settings(monkeypatch).cfg
    assert cfg.bind == ["0.0.0.0:8080"]
    assert cfg.worker_class_str == "gthread"
    assert (cfg.workers, cfg.threads) == (1, 4)
    assert (cfg.timeout, cfg.graceful_timeout, cfg.keepalive) == (30, 20, 5)
    assert cfg.limit_request_line == 4094
    assert cfg.limit_request_fields == 100
    assert cfg.limit_request_field_size == 8190
    assert (cfg.accesslog, cfg.errorlog) == (None, "-")
    assert cfg.control_socket_disable
    assert not cfg.preload_app
    assert cfg.worker_tmp_dir == gunicorn_config.worker_tmp_dir


def test_cmd_args(monkeypatch):
    """`GUNICORN_CMD_ARGS` overrides the settings."""
    args = shlex.join(["--workers", "2", "--threads", "8", "--timeout", "60"])
    cfg = load_settings(monkeypatch, args).cfg
    assert (cfg.workers, cfg.threads, cfg.timeout) == (2, 8, 60)
    assert cfg.graceful_timeout == 20


def test_app_uri(monkeypatch):
    """The app is created by the factory."""
    assert load_settings(monkeypatch).app_uri == APP


def test_worker_tmp_dir_shm(tmp_path):
    """A writable memory-backed directory is used for heartbeat files."""
    assert _worker_tmp_dir(tmp_path) == str(tmp_path)


@pytest.mark.parametrize("mode", [0o500, None])
def test_worker_tmp_dir_default(tmp_path, mode):
    """Without a writable memory-backed directory, Gunicorn's default
    (`None`, the temporary directory) is used.
    """
    shm = tmp_path / "shm"
    if mode is not None:
        shm.mkdir(mode=mode)
    try:
        if mode is not None and os.access(shm, os.W_OK):
            pytest.skip("directory permissions are not enforced (e.g., root)")
        assert _worker_tmp_dir(shm) is None
    finally:
        if mode is not None:
            shm.chmod(0o700)
