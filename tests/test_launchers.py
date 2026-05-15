"""Dist-launcher Docker-error translation — CLAUDE.md §16 v0.11 bucket 1 #2.

The launchers are render templates substituted by ``scripts/build-dist.sh``;
the @PLACEHOLDER@ tokens aren't expanded in this test. We grep the raw
template for the translated error-pattern blocks so a regression that
drops one of them fails CI.

We test the rendered text (not the build script) because:
* the placeholders don't affect the error-handling blocks
* shelling out to ``scripts/build-dist.sh`` per test would require
  Docker, buildx, and several minutes per run
"""

from __future__ import annotations

from pathlib import Path

import pytest


COMMAND_IN = Path("dist-templates/Capsule.command.in")
BAT_IN = Path("dist-templates/Capsule.bat.in")


@pytest.fixture(scope="module")
def command_text() -> str:
    return COMMAND_IN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bat_text() -> str:
    return BAT_IN.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# macOS .command
# ----------------------------------------------------------------------


def test_command_translates_port_already_allocated(command_text: str):
    """Port conflict → user-facing remap hint, not raw Docker stderr."""
    assert "address already in use" in command_text
    assert "port is already allocated" in command_text
    # The translated text mentions the safe remap path.
    assert "9090:8080" in command_text
    assert "docker rm -f" in command_text


def test_command_translates_permission_denied(command_text: str):
    """Folder permission error → Privacy & Security pointer."""
    assert "permission denied" in command_text
    assert "Privacy & Security" in command_text
    assert "Files and Folders" in command_text


def test_command_translates_daemon_connection_loss(command_text: str):
    """Daemon-disconnect mid-launch → Docker Desktop pointer."""
    # The startup check already handles "daemon not running"; this is
    # the mid-launch flavour where a one-shot reconnect makes sense.
    assert "Cannot connect to the Docker daemon" in command_text
    assert "Docker Desktop" in command_text


def test_command_captures_stderr_to_tmpfile(command_text: str):
    """The translation only works if stderr is captured for grep."""
    assert "mktemp" in command_text
    assert "2>\"$err_tmp\"" in command_text


# ----------------------------------------------------------------------
# Windows .bat
# ----------------------------------------------------------------------


def test_bat_translates_port_already_allocated(bat_text: str):
    assert "address already in use" in bat_text
    assert "port is already allocated" in bat_text
    assert "9090:8080" in bat_text
    assert "docker rm -f" in bat_text


def test_bat_translates_permission_denied(bat_text: str):
    assert "permission denied" in bat_text
    # Windows-flavoured remediation.
    assert "Properties" in bat_text
    assert "Security" in bat_text


def test_bat_translates_daemon_connection_loss(bat_text: str):
    assert "Cannot connect to the Docker daemon" in bat_text
    assert "Docker Desktop" in bat_text


def test_bat_captures_stderr_to_tmpfile(bat_text: str):
    """findstr needs a file to grep against — confirm we redirect stderr."""
    assert "ERR_TMP" in bat_text
    assert "2>\"!ERR_TMP!\"" in bat_text
    # Cleanup paths exist on both success and failure branches.
    assert "del \"!ERR_TMP!\"" in bat_text
