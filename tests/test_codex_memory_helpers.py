from __future__ import annotations

import json
import re

import codex_memory


def test_sanitize_redacts_secret_keys_and_bearer_values() -> None:
    payload = {
        "HOMEASSISTANT_TOKEN": "super-secret-token",
        "nested": {
            "message": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            "safe": "visible",
        },
    }

    assert codex_memory.sanitize(payload) == {
        "HOMEASSISTANT_TOKEN": "[redacted]",
        "nested": {
            "message": "Authorization: [redacted]",
            "safe": "visible",
        },
    }


def test_to_json_sanitizes_before_serializing() -> None:
    encoded = codex_memory.to_json({"password": "abc123", "name": "router"})

    assert json.loads(encoded) == {"name": "router", "password": "[redacted]"}


def test_tags_text_normalizes_supported_inputs() -> None:
    assert codex_memory.tags_text(None) == ""
    assert codex_memory.tags_text("server,docker") == "server,docker"
    assert codex_memory.tags_text([" server ", "", "docker"]) == "server,docker"


def test_search_query_builder_handles_punctuation() -> None:
    query = codex_memory._fts_or_query("pytest / smoke: mcp")

    assert query == "pytest* OR smoke* OR mcp*"
    assert re.fullmatch(r"[A-Za-z0-9_\"* ]+( OR [A-Za-z0-9_\"* ]+)*", query)
