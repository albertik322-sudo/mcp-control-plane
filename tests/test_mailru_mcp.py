from __future__ import annotations

import json
import ssl
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import mailru_imap as mailru


def write_config(path: Path, **overrides: object) -> None:
    account = {
        "account_id": "personal",
        "provider": "mailru",
        "address": "owner@mail.ru",
        "login": "owner@mail.ru",
        "credential_target": "CodexMailru/personal",
        "enabled": True,
    }
    account.update(overrides)
    path.write_text(json.dumps({"version": 1, "accounts": [account]}), encoding="utf-8")


def test_config_is_metadata_only_and_masks_address(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    write_config(path)

    result = mailru.MailruBridge(config_path=path, environment={}).list_accounts()

    assert result["read_only"] is True
    assert result["accounts"][0]["address"] == "ow***@mail.ru"
    assert "password" not in json.dumps(result).lower()


@pytest.mark.parametrize("field", ["password", "app_password", "secret", "token"])
def test_config_rejects_inline_secrets(tmp_path: Path, field: str) -> None:
    path = tmp_path / "accounts.json"
    write_config(path, **{field: "do-not-store-me"})

    with pytest.raises(mailru.MailruConfigError, match="forbidden"):
        mailru.MailruBridge(config_path=path, environment={}).accounts()


def test_config_rejects_unscoped_credential_target(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    write_config(path, credential_target="unrelated-secret")

    with pytest.raises(mailru.MailruConfigError, match="credential_target"):
        mailru.MailruBridge(config_path=path, environment={}).accounts()


def test_config_rejects_unknown_provider(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    write_config(path, provider="custom", credential_target="CodexCustom/personal")

    with pytest.raises(mailru.MailruConfigError, match="provider"):
        mailru.MailruBridge(config_path=path, environment={}).accounts()


def test_credential_writer_rejects_unscoped_target_before_windows_write() -> None:
    with pytest.raises(mailru.MailruConfigError, match="Unsafe credential target"):
        mailru.write_windows_credential("Unrelated/personal", "owner", "secret")


def test_message_reference_and_modified_utf7_round_trip() -> None:
    reference = mailru._encode_message_ref("personal", "Отправленные & архив", 77, 1234)

    assert mailru._decode_message_ref(reference) == ("personal", "Отправленные & архив", 77, 1234)
    folder = "Почта & travel"
    assert mailru._modified_utf7_decode(mailru._modified_utf7_encode(folder)) == folder


def test_search_criteria_are_typed_and_reject_control_characters() -> None:
    assert mailru.build_search_criteria(subject="Оплата", unread=True, since="2026-07-01") == [
        "SUBJECT",
        '"Оплата"',
        "UNSEEN",
        "SINCE",
        "01-Jul-2026",
    ]
    with pytest.raises(ValueError, match="control"):
        mailru.build_search_criteria(text="safe\r\nSTORE 1 +FLAGS \\Deleted")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        mailru.build_search_criteria(since="yesterday")


def test_safe_header_falls_back_to_raw_text_when_structured_parsing_breaks() -> None:
    class BrokenHeader:
        def get(self, _name: str, _default: str = "") -> str:
            raise IndexError("malformed address header")

        def raw_items(self):
            return [("From", "broken@example.com")]

    assert mailru._safe_header(BrokenHeader(), "From") == "broken@example.com"


def test_mime_parsing_prefers_plain_text_and_sanitizes_attachment_name() -> None:
    raw = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: owner@mail.ru\r\n"
        b"Subject: =?utf-8?b?0KLQtdGB0YI=?=\r\n"
        b"Content-Type: multipart/mixed; boundary=abc\r\n\r\n"
        b"--abc\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello plain\r\n"
        b"--abc\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<script>bad()</script><p>Hello html</p>\r\n"
        b"--abc\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=\"../unsafe?.exe\"\r\n\r\nDATA\r\n"
        b"--abc--\r\n"
    )

    result = mailru._full_message(
        raw,
        meta=b"1 (UID 9 FLAGS () RFC822.SIZE 400)",
        account_id="personal",
        folder="INBOX",
        uidvalidity=77,
        uid=9,
        max_body_chars=4096,
    )

    assert result["subject"] == "Тест"
    assert "Hello plain" in result["body"]
    assert "bad()" not in result["body"]
    assert result["attachments"][0]["filename"] == "unsafe_.exe"


class FakeImap:
    def __init__(self, host: str, port: int, *, ssl_context: ssl.SSLContext, timeout: int) -> None:
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.timeout = timeout
        self.calls: list[tuple[object, ...]] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("LOGIN", username, password))
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("SELECT", folder, readonly))
        return "OK", [b"1"]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        return name, [b"77"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"9"]
        if command == "FETCH" and args[-1] == mailru.HEADER_FETCH:
            header = b"From: Sender <sender@example.com>\r\nTo: owner@mail.ru\r\nSubject: Hi\r\n\r\n"
            return "OK", [(b"1 (UID 9 FLAGS () RFC822.SIZE 90)", header)]
        if command == "FETCH" and args[-1] == "(RFC822.SIZE)":
            return "OK", [b"1 (UID 9 RFC822.SIZE 120)"]
        if command == "FETCH" and args[-1] == "(BODY.PEEK[] FLAGS RFC822.SIZE)":
            raw = b"From: Sender <sender@example.com>\r\nTo: owner@mail.ru\r\nSubject: Hi\r\n\r\nHello"
            return "OK", [(b"1 (UID 9 FLAGS () RFC822.SIZE 120)", raw)]
        raise AssertionError((command, args))

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("LOGOUT",))
        return "BYE", [b"logout"]


def test_live_path_is_tls_readonly_and_uses_body_peek(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    write_config(path)
    clients: list[FakeImap] = []

    def factory(*args: object, **kwargs: object) -> FakeImap:
        client = FakeImap(*args, **kwargs)
        clients.append(client)
        return client

    bridge = mailru.MailruBridge(
        config_path=path,
        credential_reader=lambda target: ("owner@mail.ru", "sentinel-app-password"),
        imap_factory=factory,
        environment={},
    )
    search = bridge.search_emails("personal")
    message = bridge.read_email(search["emails"][0]["message_ref"])

    assert message["body"] == "Hello"
    assert all(client.host == "imap.mail.ru" and client.port == 993 for client in clients)
    assert all(client.ssl_context.check_hostname for client in clients)
    assert all(client.ssl_context.verify_mode == ssl.CERT_REQUIRED for client in clients)
    calls = [call for client in clients for call in client.calls]
    assert all(call[2] is True for call in calls if call[0] == "SELECT")
    assert any(any("BODY.PEEK[]" in str(item) for item in call) for call in calls if call[0] == "FETCH")
    assert not ({str(call[0]).upper() for call in calls} & mailru.WRITE_IMAP_COMMANDS)
    assert "sentinel-app-password" not in json.dumps(search)
    assert "sentinel-app-password" not in json.dumps(message)


def test_stale_uidvalidity_is_rejected_before_message_fetch(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    write_config(path)
    client = FakeImap("imap.mail.ru", 993, ssl_context=ssl.create_default_context(), timeout=15)
    bridge = mailru.MailruBridge(
        config_path=path,
        credential_reader=lambda target: ("owner@mail.ru", "secret"),
        imap_factory=lambda *args, **kwargs: client,
        environment={},
    )

    with pytest.raises(mailru.StaleMessageReference):
        bridge.read_email(mailru._encode_message_ref("personal", "INBOX", 999, 9))

    assert not any(call[0] == "FETCH" for call in client.calls)


def test_yandex_uses_allowlisted_host_and_separate_login(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    write_config(
        path,
        provider="yandex",
        address="owner@yandex.ru",
        login="owner",
        credential_target="CodexYandex/personal",
    )
    clients: list[FakeImap] = []

    def factory(*args: object, **kwargs: object) -> FakeImap:
        client = FakeImap(*args, **kwargs)
        clients.append(client)
        return client

    bridge = mailru.MailruBridge(
        config_path=path,
        credential_reader=lambda target: ("owner", "app-password"),
        imap_factory=factory,
        environment={},
    )

    result = bridge.health("personal", connect=True)

    assert result["ok"] is True
    assert result["accounts"][0]["host"] == "imap.yandex.ru"
    assert clients[0].host == "imap.yandex.ru"
    assert ("LOGIN", "owner", "app-password") in clients[0].calls


def test_stdio_entrypoint_exposes_only_read_tools() -> None:
    expected = {
        "health",
        "list_accounts",
        "list_folders",
        "search_emails",
        "read_email",
        "batch_read_emails",
        "read_thread",
        "read_attachment",
    }

    async def inspect_server() -> set[str]:
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable, args=[str(root / "mailru_mcp.py")])
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return {tool.name for tool in result.tools}

    names = anyio.run(inspect_server)

    assert names == expected
    assert not names & {"send", "archive", "delete", "move", "store", "expunge", "raw_imap"}
