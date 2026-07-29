from __future__ import annotations

import mcp_runtime


def test_powershell_base_args_are_noninteractive(monkeypatch) -> None:
    monkeypatch.setenv("POWERSHELL_EXE", "pwsh")

    assert mcp_runtime.powershell_base_args() == ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass"]


def test_configure_stdio_sets_utf8_defaults(monkeypatch) -> None:
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    mcp_runtime.configure_stdio()

    assert mcp_runtime.os.environ["PYTHONIOENCODING"] == "utf-8"
    assert mcp_runtime.os.environ["PYTHONUTF8"] == "1"


def test_compact_text_output_leaves_short_text_unchanged() -> None:
    text = "short output\n"

    compacted, metadata = mcp_runtime.compact_text_output(
        text,
        filter_name="log",
        threshold=100,
        max_chars=200,
    )

    assert compacted == text
    assert metadata is None


def test_compact_text_output_uses_rtk_filter(monkeypatch) -> None:
    text = "INFO repeated line\n" * 100
    monkeypatch.setattr(mcp_runtime, "_rtk_pipe", lambda value, filter_name: "Log Summary\n")

    compacted, metadata = mcp_runtime.compact_text_output(
        text,
        filter_name="log",
        threshold=10,
        max_chars=500,
    )

    assert compacted == "Log Summary\n"
    assert metadata is not None
    assert metadata["methods"] == ["rtk:log"]
    assert metadata["returned_chars"] < metadata["original_chars"]


def test_compact_text_output_falls_back_to_bounded_head_tail(monkeypatch) -> None:
    text = "A" * 1_000 + "END"
    monkeypatch.setattr(mcp_runtime, "_rtk_pipe", lambda value, filter_name: None)

    compacted, metadata = mcp_runtime.compact_text_output(
        text,
        filter_name="log",
        threshold=10,
        max_chars=200,
    )

    assert len(compacted) <= 200
    assert compacted.startswith("A")
    assert compacted.endswith("END")
    assert "MCP output truncated" in compacted
    assert metadata is not None
    assert metadata["methods"] == ["head-tail-limit"]


def test_compact_command_result_can_be_disabled() -> None:
    result = {"stdout": "X" * 1_000, "stderr": "", "exit_code": 0}

    compacted = mcp_runtime.compact_command_result(
        result,
        enabled=False,
        threshold=10,
        max_chars=100,
    )

    assert compacted == result
    assert "output_compaction" not in compacted
