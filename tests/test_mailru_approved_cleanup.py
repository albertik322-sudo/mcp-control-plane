from __future__ import annotations

import mailru_approved_cleanup as cleanup
import mailru_imap as mailru


class FakeWritableImap:
    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[tuple[object, ...]] = []

    def login(self, _username: str, _password: str):
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = False):
        self.calls.append(("SELECT", folder, readonly))
        return "OK", [b"1"]

    def response(self, name: str):
        return name, [b"77"]

    def uid(self, command: str, *args: object):
        self.calls.append((command, *args))
        return "OK", [b"moved"]

    def logout(self):
        return "BYE", [b"logout"]


def _bridge(client: FakeWritableImap) -> mailru.MailruBridge:
    return mailru.MailruBridge(
        config_path="mailru_accounts.example.json",
        credential_reader=lambda _target: ("name@mail.ru", "password"),
        imap_factory=lambda *_args, **_kwargs: client,
    )


def test_archive_rechecks_reference_and_moves_by_uid() -> None:
    client = FakeWritableImap()
    result = cleanup.archive_referenced_messages(
        _bridge(client),
        account_id="personal",
        message_refs=[mailru._encode_message_ref("personal", "INBOX", 77, 9)],
    )

    assert result["moved"] == 1
    assert ("SELECT", '"INBOX"', False) in client.calls
    assert ("MOVE", "9", '"Archive"') in client.calls


def test_cleanup_refuses_a_reference_from_another_folder() -> None:
    client = FakeWritableImap()
    reference = mailru._encode_message_ref("personal", "Archive", 77, 9)

    try:
        cleanup.archive_referenced_messages(_bridge(client), account_id="personal", message_refs=[reference])
    except ValueError as exc:
        assert "source folder" in str(exc)
    else:
        raise AssertionError("Expected source-folder validation failure")

    assert not client.calls
