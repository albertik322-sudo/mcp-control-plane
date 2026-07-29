from __future__ import annotations

from pathlib import Path

import ssh_auth


def test_remote_key_filename_resolves_relative_to_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REMOTE_KEY_FILENAME", ".secrets/homelab_ed25519")
    monkeypatch.delenv("REMOTE_KEY", raising=False)

    assert ssh_auth.remote_key_filename(tmp_path) == str(
        (tmp_path / ".secrets" / "homelab_ed25519").resolve()
    )


def test_remote_key_alias_and_empty_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("REMOTE_KEY_FILENAME", raising=False)
    monkeypatch.setenv("REMOTE_KEY", "keyfile")
    assert ssh_auth.remote_key_filename(tmp_path) == str((tmp_path / "keyfile").resolve())

    monkeypatch.setenv("REMOTE_KEY", "")
    assert ssh_auth.remote_key_filename(tmp_path) is None


def test_ssh_connect_kwargs_are_explicit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REMOTE_KEY_FILENAME", "keyfile")
    kwargs = ssh_auth.ssh_connect_kwargs("fallback", tmp_path)

    assert kwargs == {
        "password": "fallback",
        "key_filename": str((tmp_path / "keyfile").resolve()),
        "look_for_keys": False,
        "allow_agent": False,
    }
