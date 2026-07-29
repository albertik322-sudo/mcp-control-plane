"""Narrow, user-approved IMAP cleanup actions for the personal-mail bridge.

This module is deliberately separate from ``mailru_mcp.py``: the registered
MCP server remains read-only. These functions only accept opaque message
references emitted by the bridge, re-check UIDVALIDITY immediately before the
operation, and use UID MOVE without a copy/delete fallback.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from mailru_imap import (
    MAX_SEARCH_RESULTS,
    MailruBridge,
    MailruError,
    _decode_message_ref,
    _imap_quote,
    _modified_utf7_encode,
)


def _validated_uids(
    message_refs: Iterable[str], *, account_id: str, source_folder: str
) -> tuple[int, list[int]]:
    rows = list(message_refs)
    if not rows or len(rows) > MAX_SEARCH_RESULTS:
        raise ValueError(f"message_refs must contain between 1 and {MAX_SEARCH_RESULTS} items")
    expected_uidvalidity: int | None = None
    uids: list[int] = []
    for reference in rows:
        ref_account, ref_folder, uidvalidity, uid = _decode_message_ref(reference)
        if ref_account != account_id or ref_folder != source_folder:
            raise ValueError("All message references must belong to the requested account and source folder")
        if expected_uidvalidity is None:
            expected_uidvalidity = uidvalidity
        elif uidvalidity != expected_uidvalidity:
            raise ValueError("All message references must share the same folder UIDVALIDITY")
        uids.append(uid)
    return expected_uidvalidity or 0, sorted(set(uids))


def _select_writable(client: Any, folder: str) -> int:
    status, _ = client.select(_imap_quote(_modified_utf7_encode(folder)), readonly=False)
    if str(status).upper() != "OK":
        raise MailruError(f"Cannot open mail folder for approved cleanup: {folder}")
    response = client.response("UIDVALIDITY")
    values = response[1] if isinstance(response, tuple) and len(response) > 1 else None
    candidate = values[0] if values else None
    if isinstance(candidate, bytes):
        candidate = candidate.decode("ascii", errors="ignore")
    try:
        uidvalidity = int(candidate)
    except (TypeError, ValueError) as exc:
        raise MailruError(f"Mail server did not return UIDVALIDITY for folder: {folder}") from exc
    if uidvalidity <= 0:
        raise MailruError(f"Mail server returned invalid UIDVALIDITY for folder: {folder}")
    return uidvalidity


def move_referenced_messages(
    bridge: MailruBridge,
    *,
    account_id: str,
    source_folder: str,
    destination_folder: str,
    message_refs: Iterable[str],
) -> dict[str, object]:
    """Move pre-reviewed messages using UID MOVE, or fail without a fallback."""
    if not destination_folder or any(char in destination_folder for char in "\r\n\x00"):
        raise ValueError("Invalid destination folder")
    expected_uidvalidity, uids = _validated_uids(
        message_refs, account_id=account_id, source_folder=source_folder
    )
    account = bridge.account(account_id)
    with bridge._connect(account) as client:
        current_uidvalidity = _select_writable(client, source_folder)
        if current_uidvalidity != expected_uidvalidity:
            raise MailruError("Mail message reference is stale because folder UIDVALIDITY changed")
        status, _ = client.uid(
            "MOVE",
            ",".join(str(uid) for uid in uids),
            _imap_quote(_modified_utf7_encode(destination_folder)),
        )
        if str(status).upper() != "OK":
            raise MailruError("Mail server rejected approved cleanup move")
    return {
        "account_id": account_id,
        "source_folder": source_folder,
        "destination_folder": destination_folder,
        "moved": len(uids),
        "uids": uids,
    }


def archive_referenced_messages(
    bridge: MailruBridge,
    *,
    account_id: str,
    message_refs: Iterable[str],
    archive_folder: str = "Archive",
) -> dict[str, object]:
    """Archive specifically approved INBOX messages; no message is deleted."""
    return move_referenced_messages(
        bridge,
        account_id=account_id,
        source_folder="INBOX",
        destination_folder=archive_folder,
        message_refs=message_refs,
    )


def trash_referenced_messages(
    bridge: MailruBridge,
    *,
    account_id: str,
    message_refs: Iterable[str],
    trash_folder: str = "Корзина",
) -> dict[str, object]:
    """Move specifically approved INBOX messages to Trash; no EXPUNGE occurs."""
    return move_referenced_messages(
        bridge,
        account_id=account_id,
        source_folder="INBOX",
        destination_folder=trash_folder,
        message_refs=message_refs,
    )
