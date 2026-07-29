from __future__ import annotations

import base64
import ctypes
import hashlib
import imaplib
import json
import os
import re
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


SCHEMA_VERSION = 1
IMAP_PROVIDERS = {
    "mailru": {"host": "imap.mail.ru", "port": 993, "credential_prefix": "CodexMailru/"},
    "yandex": {"host": "imap.yandex.ru", "port": 993, "credential_prefix": "CodexYandex/"},
}
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_BODY_CHARS = 65_536
DEFAULT_MAX_MESSAGE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 100
MAX_BATCH_MESSAGES = 10
MAX_THREAD_SCAN = 500
MAX_MIME_PARTS = 100
MAX_ACCOUNTS = 10
ACCOUNT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAFE_CREDENTIAL_PREFIXES = tuple(str(row["credential_prefix"]) for row in IMAP_PROVIDERS.values())
HEADER_FETCH = (
    "(BODY.PEEK[HEADER.FIELDS "
    "(FROM TO CC BCC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE CONTENT-DISPOSITION)] "
    "FLAGS RFC822.SIZE)"
)
WRITE_IMAP_COMMANDS = frozenset(
    {
        "APPEND",
        "COPY",
        "CREATE",
        "DELETE",
        "EXPUNGE",
        "MOVE",
        "RENAME",
        "STORE",
        "SUBSCRIBE",
        "UNSUBSCRIBE",
    }
)


class MailruError(RuntimeError):
    """Base error safe to expose through the MCP boundary."""


class MailruConfigError(MailruError):
    """Configuration is missing or unsafe."""


class StaleMessageReference(MailruError):
    """The folder UIDVALIDITY changed after a reference was issued."""


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    provider: str
    address: str
    login: str
    credential_target: str
    enabled: bool = True


def default_accounts_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Codex" / "mailru-mcp" / "accounts.json"
    return Path.home() / ".codex" / "mailru-mcp" / "accounts.json"


def _safe_positive_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _load_accounts(path: Path) -> list[AccountConfig]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailruConfigError(f"Cannot read mail account configuration: {path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        raise MailruConfigError(f"Mail account configuration must use version {SCHEMA_VERSION}")
    rows = payload.get("accounts")
    if not isinstance(rows, list):
        raise MailruConfigError("Mail account configuration must contain an accounts list")
    if len(rows) > MAX_ACCOUNTS:
        raise MailruConfigError(f"At most {MAX_ACCOUNTS} mail accounts are supported")

    accounts: list[AccountConfig] = []
    seen: set[str] = set()
    forbidden = {"password", "app_password", "secret", "token"}
    allowed = {"account_id", "provider", "address", "login", "credential_target", "enabled"}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MailruConfigError(f"accounts[{index}] must be an object")
        lowered_keys = {str(key).lower() for key in row}
        if lowered_keys & forbidden:
            raise MailruConfigError("Passwords and secrets are forbidden in accounts.json; use Windows Credential Manager")
        unknown = set(row) - allowed
        if unknown:
            raise MailruConfigError(f"accounts[{index}] contains unsupported fields: {', '.join(sorted(unknown))}")
        account_id = str(row.get("account_id", "")).strip()
        provider = str(row.get("provider", "mailru")).strip().lower()
        address = str(row.get("address", "")).strip()
        login = str(row.get("login", address)).strip()
        provider_config = IMAP_PROVIDERS.get(provider)
        if provider_config is None:
            raise MailruConfigError(f"accounts[{index}].provider is unsupported")
        expected_target = f"{provider_config['credential_prefix']}{account_id}"
        credential_target = str(row.get("credential_target", expected_target)).strip()
        if not ACCOUNT_ID_RE.fullmatch(account_id):
            raise MailruConfigError(f"accounts[{index}].account_id is invalid")
        if account_id in seen:
            raise MailruConfigError(f"Duplicate mail account_id: {account_id}")
        if not address or "@" not in address or any(char in address for char in "\r\n\x00"):
            raise MailruConfigError(f"accounts[{index}].address is invalid")
        if not login or len(login) > 320 or any(char in login for char in "\r\n\x00"):
            raise MailruConfigError(f"accounts[{index}].login is invalid")
        if credential_target != expected_target:
            raise MailruConfigError(f"accounts[{index}].credential_target must be {expected_target}")
        seen.add(account_id)
        accounts.append(
            AccountConfig(
                account_id=account_id,
                provider=provider,
                address=address,
                login=login,
                credential_target=credential_target,
                enabled=bool(row.get("enabled", True)),
            )
        )
    return accounts


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_uint64),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def read_windows_credential(target: str) -> tuple[str, str]:
    """Read a generic credential without returning it in logs or configuration."""
    if os.name != "nt":
        raise MailruConfigError("Windows Credential Manager is required for mail app passwords")
    if not target.startswith(SAFE_CREDENTIAL_PREFIXES):
        raise MailruConfigError("Unsafe credential target")
    advapi32 = ctypes.WinDLL("Advapi32.dll")
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(_CredentialW))]
    cred_read.restype = ctypes.c_bool
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(_CredentialW)()
    if not cred_read(target, 1, 0, ctypes.byref(pointer)):
        raise MailruConfigError(f"Credential is not configured for {target}")
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        if size <= 0 or not credential.CredentialBlob:
            raise MailruConfigError(f"Credential has no secret for {target}")
        raw = ctypes.string_at(credential.CredentialBlob, size)
        secret = raw.decode("utf-16-le")
        username = str(credential.UserName or "")
        return username, secret
    finally:
        cred_free(pointer)


def write_windows_credential(target: str, username: str, secret: str) -> None:
    """Store a generic credential locally; this helper is intentionally not exposed as an MCP tool."""
    if os.name != "nt":
        raise MailruConfigError("Windows Credential Manager is required for mail app passwords")
    if not target.startswith(SAFE_CREDENTIAL_PREFIXES):
        raise MailruConfigError("Unsafe credential target")
    if not username or any(char in username for char in "\r\n\x00"):
        raise MailruConfigError("Credential username is invalid")
    encoded = bytearray(str(secret).encode("utf-16-le"))
    if not encoded or len(encoded) > 512:
        raise MailruConfigError("Credential secret is empty or too large")
    buffer = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
    credential = _CredentialW()
    credential.Type = 1
    credential.TargetName = target
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = username
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(_CredentialW), ctypes.c_uint32]
    cred_write.restype = ctypes.c_bool
    try:
        if not cred_write(ctypes.byref(credential), 0):
            raise MailruConfigError(f"Cannot store credential for {target}; Windows error {ctypes.get_last_error()}")
    finally:
        for index in range(len(encoded)):
            encoded[index] = 0


def _mask_address(address: str) -> str:
    local, separator, domain = address.partition("@")
    if not separator:
        return "***"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"


def _imap_quote(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("IMAP search values cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_iso_date(value: str | None, field: str) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc


def build_search_criteria(
    *,
    from_address: str | None = None,
    to_address: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    unread: bool | None = None,
    since: str | None = None,
    before: str | None = None,
) -> list[str]:
    criteria: list[str] = []
    for key, value in (("FROM", from_address), ("TO", to_address), ("SUBJECT", subject), ("TEXT", text)):
        if value is not None and str(value).strip():
            criteria.extend([key, _imap_quote(str(value).strip())])
    if unread is True:
        criteria.append("UNSEEN")
    elif unread is False:
        criteria.append("SEEN")
    since_date = _parse_iso_date(since, "since")
    before_date = _parse_iso_date(before, "before")
    if since_date:
        criteria.extend(["SINCE", since_date.strftime("%d-%b-%Y")])
    if before_date:
        criteria.extend(["BEFORE", before_date.strftime("%d-%b-%Y")])
    return criteria or ["ALL"]


def _modified_utf7_encode(value: str) -> str:
    result: list[str] = []
    non_ascii: list[str] = []

    def flush() -> None:
        if not non_ascii:
            return
        raw = "".join(non_ascii).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        result.append(f"&{encoded}-")
        non_ascii.clear()

    for char in value:
        codepoint = ord(char)
        if 0x20 <= codepoint <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            non_ascii.append(char)
    flush()
    return "".join(result)


def _modified_utf7_decode(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            result.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end < 0:
            raise ValueError("Invalid modified UTF-7 folder name")
        token = value[index + 1 : end]
        if not token:
            result.append("&")
        else:
            encoded = token.replace(",", "/")
            encoded += "=" * (-len(encoded) % 4)
            result.append(base64.b64decode(encoded).decode("utf-16-be"))
        index = end + 1
    return "".join(result)


def _encode_message_ref(account_id: str, folder: str, uidvalidity: int, uid: int) -> str:
    payload = json.dumps(
        {"a": account_id, "f": folder, "v": int(uidvalidity), "u": int(uid)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"mru1.{token}"


def _decode_message_ref(reference: str) -> tuple[str, str, int, int]:
    if not reference.startswith("mru1.") or len(reference) > 1024:
        raise ValueError("Invalid mail message reference")
    token = reference[5:]
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        account_id = str(payload["a"])
        folder = str(payload["f"])
        uidvalidity = int(payload["v"])
        uid = int(payload["u"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid mail message reference") from exc
    if not ACCOUNT_ID_RE.fullmatch(account_id) or not folder or len(folder) > 512:
        raise ValueError("Invalid mail message reference")
    if any(char in folder for char in "\r\n\x00") or uidvalidity <= 0 or uid <= 0:
        raise ValueError("Invalid mail message reference")
    return account_id, folder, uidvalidity, uid


class _HTMLTextExtractor(HTMLParser):
    BLOCKED = {"script", "style", "form", "noscript"}
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.BLOCKED:
            self._blocked_depth += 1
        elif tag in self.BREAKS and not self._blocked_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.BLOCKED and self._blocked_depth:
            self._blocked_depth -= 1
        elif tag in self.BREAKS and not self._blocked_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _clean_filename(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", str(value)).strip(" ._")
    cleaned = cleaned.replace("..", "_")
    return cleaned[:180] or "attachment"


def _safe_header(message: Message, name: str, max_chars: int = 4096) -> str:
    try:
        value = str(message.get(name, ""))
    except Exception:
        # Some historic messages contain malformed address headers that make
        # Python's structured-header parser raise while reading a mailbox.
        # Use the raw header text when available so one bad message cannot
        # block a bounded inventory or cleanup scan.
        value = ""
        try:
            for key, raw_value in message.raw_items():
                if key.casefold() == name.casefold():
                    value = str(raw_value)
                    break
        except Exception:
            value = ""
    return value[:max_chars]


def _addresses(message: Message, *names: str) -> list[str]:
    values = [_safe_header(message, name) for name in names]
    return [address for display, address in getaddresses(values) if address]


def _message_date(message: Message) -> str | None:
    raw = _safe_header(message, "Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_flags(meta: bytes) -> list[str]:
    match = re.search(rb"\bFLAGS \(([^)]*)\)", meta)
    if not match:
        return []
    return [token.decode("ascii", errors="replace") for token in match.group(1).split()]


def _parse_uid(meta: bytes) -> int | None:
    match = re.search(rb"\bUID (\d+)\b", meta)
    return int(match.group(1)) if match else None


def _parse_size(meta: bytes) -> int | None:
    match = re.search(rb"\bRFC822\.SIZE (\d+)\b", meta)
    return int(match.group(1)) if match else None


def _fetch_tuples(data: list[Any] | None) -> Iterator[tuple[bytes, bytes]]:
    for row in data or []:
        if isinstance(row, tuple) and len(row) >= 2 and isinstance(row[0], bytes) and isinstance(row[1], bytes):
            yield row[0], row[1]


def _message_ids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def _normalize_subject(value: str) -> str:
    subject = value.strip().lower()
    while True:
        stripped = re.sub(r"^(?:re|fw|fwd|ответ|пересл)\s*:\s*", "", subject, flags=re.IGNORECASE)
        if stripped == subject:
            break
        subject = stripped
    return re.sub(r"\s+", " ", subject)


def _body_and_attachments(message: Message, max_body_chars: int) -> tuple[str, bool, list[dict[str, Any]], list[str]]:
    body = ""
    html_fallback = ""
    attachments: list[dict[str, Any]] = []
    warnings: list[str] = []
    parts_seen = 0
    for part_id, part in enumerate(message.walk(), start=1):
        parts_seen += 1
        if parts_seen > MAX_MIME_PARTS:
            warnings.append(f"MIME part limit {MAX_MIME_PARTS} reached")
            break
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = _clean_filename(part.get_filename())
        content_type = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
            warnings.append(f"Could not decode MIME part {part_id}")
        is_attachment = disposition == "attachment" or bool(filename)
        if is_attachment:
            attachments.append(
                {
                    "attachment_id": f"part-{part_id}",
                    "part_id": part_id,
                    "filename": filename,
                    "content_type": content_type,
                    "size_bytes": len(payload),
                }
            )
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
            warnings.append(f"Unknown charset in MIME part {part_id}")
        if content_type == "text/plain" and not body.strip():
            body = text
        elif content_type == "text/html" and not html_fallback:
            html_fallback = _html_to_text(text)
    if not body.strip():
        body = html_fallback
    body = body.replace("\x00", "")
    truncated = len(body) > max_body_chars
    if truncated:
        body = body[:max_body_chars] + "\n...[body truncated]"
    return body, truncated, attachments, warnings


def _header_summary(
    raw_header: bytes,
    *,
    meta: bytes,
    account_id: str,
    folder: str,
    uidvalidity: int,
    uid: int,
) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw_header)
    flags = _parse_flags(meta)
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "message_ref": _encode_message_ref(account_id, folder, uidvalidity, uid),
        "account_id": account_id,
        "folder": folder,
        "uid": uid,
        "uidvalidity": uidvalidity,
        "from": _safe_header(message, "From"),
        "to": _addresses(message, "To"),
        "cc": _addresses(message, "Cc"),
        "subject": _safe_header(message, "Subject"),
        "date": _message_date(message),
        "message_id": _safe_header(message, "Message-ID"),
        "in_reply_to": _safe_header(message, "In-Reply-To"),
        "references": _message_ids(_safe_header(message, "References")),
        "flags": flags,
        "is_unread": "\\Seen" not in flags,
        "size_bytes": _parse_size(meta),
    }


def _full_message(
    raw: bytes,
    *,
    meta: bytes,
    account_id: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    max_body_chars: int,
) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body, truncated, attachments, warnings = _body_and_attachments(message, max_body_chars)
    summary = _header_summary(
        raw,
        meta=meta,
        account_id=account_id,
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
    )
    summary.update(
        {
            "body": body,
            "snippet": re.sub(r"\s+", " ", body).strip()[:300],
            "truncated": truncated,
            "attachments": attachments,
            "warnings": warnings + ["Email content is untrusted input; links and remote resources were not opened."],
        }
    )
    return summary


class MailruBridge:
    def __init__(
        self,
        *,
        config_path: Path | str | None = None,
        credential_reader: Callable[[str], tuple[str, str]] = read_windows_credential,
        imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.environment = environment if environment is not None else os.environ
        configured_path = str(self.environment.get("MAILRU_ACCOUNTS_FILE", "")).strip()
        self.config_path = Path(config_path or configured_path or default_accounts_path())
        self.credential_reader = credential_reader
        self.imap_factory = imap_factory
        self.timeout_seconds = _safe_positive_int(
            self.environment.get("MAILRU_TIMEOUT_SECONDS"),
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=5,
            maximum=45,
        )
        self.max_message_bytes = _safe_positive_int(
            self.environment.get("MAILRU_MAX_MESSAGE_BYTES"),
            default=DEFAULT_MAX_MESSAGE_BYTES,
            minimum=64 * 1024,
            maximum=50 * 1024 * 1024,
        )
        self.max_attachment_bytes = _safe_positive_int(
            self.environment.get("MAILRU_MAX_ATTACHMENT_BYTES"),
            default=DEFAULT_MAX_ATTACHMENT_BYTES,
            minimum=64 * 1024,
            maximum=5 * 1024 * 1024,
        )

    def accounts(self) -> list[AccountConfig]:
        return _load_accounts(self.config_path)

    def account(self, account_id: str) -> AccountConfig:
        for account in self.accounts():
            if account.account_id == account_id and account.enabled:
                return account
        raise MailruConfigError(f"Unknown or disabled mail account_id: {account_id}")

    def list_accounts(self) -> dict[str, Any]:
        rows = self.accounts()
        return {
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "count": len(rows),
            "accounts": [
                {
                    "account_id": row.account_id,
                    "provider": row.provider,
                    "address": _mask_address(row.address),
                    "enabled": row.enabled,
                    "credential_target": row.credential_target,
                }
                for row in rows
            ],
        }

    @contextmanager
    def _connect(self, account: AccountConfig) -> Iterator[Any]:
        credential_username, password = self.credential_reader(account.credential_target)
        if credential_username and credential_username.casefold() != account.login.casefold():
            raise MailruConfigError(f"Credential username does not match accounts.json for {account.account_id}")
        provider = IMAP_PROVIDERS[account.provider]
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        client = None
        try:
            client = self.imap_factory(
                str(provider["host"]),
                int(provider["port"]),
                ssl_context=context,
                timeout=self.timeout_seconds,
            )
            client._encoding = "utf-8"
            status, _ = client.login(account.login, password)
            if str(status).upper() != "OK":
                raise MailruError(f"Mail authentication failed for account {account.account_id}")
            yield client
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailruError(f"Mail connection or authentication failed for account {account.account_id}") from exc
        finally:
            password = ""
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

    @staticmethod
    def _select_readonly(client: Any, folder: str) -> int:
        encoded_folder = _modified_utf7_encode(folder)
        status, _ = client.select(_imap_quote(encoded_folder), readonly=True)
        if str(status).upper() != "OK":
            raise MailruError(f"Cannot open mail folder read-only: {folder}")
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

    def health(self, account_id: str | None = None, connect: bool = False) -> dict[str, Any]:
        rows = [self.account(account_id)] if account_id else [row for row in self.accounts() if row.enabled]
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "ok": bool(rows),
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "tls_required": True,
            "live_check": connect,
            "accounts": [],
        }
        for account in rows:
            provider = IMAP_PROVIDERS[account.provider]
            item: dict[str, Any] = {
                "account_id": account.account_id,
                "provider": account.provider,
                "address": _mask_address(account.address),
                "host": provider["host"],
                "port": provider["port"],
            }
            if connect:
                try:
                    with self._connect(account) as client:
                        self._select_readonly(client, "INBOX")
                    item["ok"] = True
                except MailruError as exc:
                    item.update({"ok": False, "error": str(exc)})
                    result["ok"] = False
            else:
                item["ok"] = True
            result["accounts"].append(item)
        if not rows:
            result["warning"] = "No enabled mail accounts are configured"
        return result

    def list_folders(self, account_id: str, include_counts: bool = True, limit: int = 100) -> dict[str, Any]:
        account = self.account(account_id)
        bounded_limit = _safe_positive_int(limit, default=100, minimum=1, maximum=100)
        folders: list[dict[str, Any]] = []
        with self._connect(account) as client:
            status, data = client.list()
            if str(status).upper() != "OK":
                raise MailruError(f"Cannot list folders for account {account_id}")
            for raw in (data or [])[:bounded_limit]:
                if not isinstance(raw, bytes):
                    continue
                decoded = raw.decode("ascii", errors="replace")
                quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', decoded)
                encoded_name = quoted[-1].replace('\\"', '"').replace("\\\\", "\\") if quoted else decoded.split()[-1]
                try:
                    folder_name = _modified_utf7_decode(encoded_name)
                except ValueError:
                    folder_name = encoded_name
                flags_match = re.match(r"\(([^)]*)\)", decoded)
                item: dict[str, Any] = {
                    "name": folder_name,
                    "flags": flags_match.group(1).split() if flags_match else [],
                }
                if include_counts:
                    try:
                        stat_status, stat_data = client.status(_imap_quote(encoded_name), "(MESSAGES UNSEEN UIDVALIDITY)")
                        if str(stat_status).upper() == "OK" and stat_data:
                            text = stat_data[0].decode("ascii", errors="replace") if isinstance(stat_data[0], bytes) else str(stat_data[0])
                            for key in ("MESSAGES", "UNSEEN", "UIDVALIDITY"):
                                match = re.search(rf"\b{key}\s+(\d+)", text)
                                if match:
                                    item[key.lower()] = int(match.group(1))
                    except Exception:
                        item["counts_unavailable"] = True
                folders.append(item)
        return {
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "account_id": account_id,
            "count": len(folders),
            "truncated": len(folders) >= bounded_limit,
            "folders": folders,
        }

    @staticmethod
    def _search_uids(client: Any, criteria: list[str]) -> list[int]:
        # Mail.ru rejects the optional UTF-8 charset token even for `ALL`.
        # Keep search criteria quoted and let the selected IMAP session encoding
        # handle non-ASCII values rather than advertising an unsupported charset.
        status, data = client.uid("SEARCH", None, *criteria)
        if str(status).upper() != "OK":
            raise MailruError("Mail search failed")
        raw = data[0] if data else b""
        if isinstance(raw, str):
            raw = raw.encode("ascii", errors="ignore")
        return [int(token) for token in raw.split() if token.isdigit()]

    @staticmethod
    def _fetch_headers(client: Any, uids: list[int], account_id: str, folder: str, uidvalidity: int) -> dict[int, dict[str, Any]]:
        if not uids:
            return {}
        status, data = client.uid("FETCH", ",".join(str(uid) for uid in uids), HEADER_FETCH)
        if str(status).upper() != "OK":
            raise MailruError("Mail header fetch failed")
        results: dict[int, dict[str, Any]] = {}
        for meta, raw_header in _fetch_tuples(data):
            uid = _parse_uid(meta)
            if uid is None:
                continue
            results[uid] = _header_summary(
                raw_header,
                meta=meta,
                account_id=account_id,
                folder=folder,
                uidvalidity=uidvalidity,
                uid=uid,
            )
        return results

    def search_emails(
        self,
        account_id: str,
        folder: str = "INBOX",
        from_address: str | None = None,
        to_address: str | None = None,
        subject: str | None = None,
        text: str | None = None,
        unread: bool | None = None,
        since: str | None = None,
        before: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        account = self.account(account_id)
        bounded_limit = _safe_positive_int(limit, default=20, minimum=1, maximum=MAX_SEARCH_RESULTS)
        bounded_offset = _safe_positive_int(offset, default=0, minimum=0, maximum=100_000)
        criteria = build_search_criteria(
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            text=text,
            unread=unread,
            since=since,
            before=before,
        )
        with self._connect(account) as client:
            uidvalidity = self._select_readonly(client, folder)
            all_uids = self._search_uids(client, criteria)
            ordered = list(reversed(all_uids))
            page = ordered[bounded_offset : bounded_offset + bounded_limit]
            summaries = self._fetch_headers(client, page, account_id, folder, uidvalidity)
        rows = [summaries[uid] for uid in page if uid in summaries]
        next_offset = bounded_offset + len(page) if bounded_offset + len(page) < len(ordered) else None
        return {
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "account_id": account_id,
            "folder": folder,
            "criteria": {
                "from_address": from_address,
                "to_address": to_address,
                "subject": subject,
                "text": text,
                "unread": unread,
                "since": since,
                "before": before,
            },
            "total_matches": len(ordered),
            "offset": bounded_offset,
            "next_offset": next_offset,
            "count": len(rows),
            "emails": rows,
            "warnings": ["Search returns headers only. Read shortlisted messages for body text and attachment metadata."],
        }

    def _fetch_raw(self, client: Any, uid: int) -> tuple[bytes, bytes]:
        size_status, size_data = client.uid("FETCH", str(uid), "(RFC822.SIZE)")
        if str(size_status).upper() != "OK":
            raise MailruError("Mail message size check failed")
        size = None
        for meta, _ in _fetch_tuples(size_data):
            size = _parse_size(meta)
        if size is None:
            for row in size_data or []:
                if isinstance(row, bytes):
                    size = _parse_size(row)
                    if size is not None:
                        break
        if size is not None and size > self.max_message_bytes:
            raise MailruError(f"Message exceeds the configured {self.max_message_bytes}-byte safety limit")
        status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[] FLAGS RFC822.SIZE)")
        if str(status).upper() != "OK":
            raise MailruError("Mail message fetch failed")
        for meta, raw in _fetch_tuples(data):
            if len(raw) > self.max_message_bytes:
                raise MailruError(f"Message exceeds the configured {self.max_message_bytes}-byte safety limit")
            return meta, raw
        raise MailruError("Mail server returned no message body")

    def read_email(self, message_ref: str, max_body_chars: int = DEFAULT_BODY_CHARS) -> dict[str, Any]:
        account_id, folder, expected_uidvalidity, uid = _decode_message_ref(message_ref)
        account = self.account(account_id)
        bounded_body = _safe_positive_int(max_body_chars, default=DEFAULT_BODY_CHARS, minimum=1024, maximum=262_144)
        with self._connect(account) as client:
            current_uidvalidity = self._select_readonly(client, folder)
            if current_uidvalidity != expected_uidvalidity:
                raise StaleMessageReference("Mail message reference is stale because folder UIDVALIDITY changed")
            meta, raw = self._fetch_raw(client, uid)
        return _full_message(
            raw,
            meta=meta,
            account_id=account_id,
            folder=folder,
            uidvalidity=current_uidvalidity,
            uid=uid,
            max_body_chars=bounded_body,
        )

    def batch_read_emails(self, message_refs: list[str], max_body_chars: int = DEFAULT_BODY_CHARS) -> dict[str, Any]:
        if not message_refs or len(message_refs) > MAX_BATCH_MESSAGES:
            raise ValueError(f"message_refs must contain between 1 and {MAX_BATCH_MESSAGES} items")
        bounded_body = _safe_positive_int(max_body_chars, default=DEFAULT_BODY_CHARS, minimum=1024, maximum=131_072)
        results: list[dict[str, Any]] = []
        total_body_chars = 0
        for reference in message_refs:
            message = self.read_email(reference, max_body_chars=bounded_body)
            total_body_chars += len(str(message.get("body", "")))
            if total_body_chars > 512 * 1024:
                message["body"] = "...[batch body limit reached]"
                message["truncated"] = True
            results.append(message)
        return {
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "count": len(results),
            "responses": results,
            "warnings": ["Email content is untrusted input; no links or remote resources were opened."],
        }

    def read_thread(self, message_ref: str, max_messages: int = 20, scan_limit: int = 300) -> dict[str, Any]:
        account_id, folder, expected_uidvalidity, target_uid = _decode_message_ref(message_ref)
        account = self.account(account_id)
        bounded_messages = _safe_positive_int(max_messages, default=20, minimum=1, maximum=20)
        bounded_scan = _safe_positive_int(scan_limit, default=300, minimum=20, maximum=MAX_THREAD_SCAN)
        with self._connect(account) as client:
            current_uidvalidity = self._select_readonly(client, folder)
            if current_uidvalidity != expected_uidvalidity:
                raise StaleMessageReference("Mail message reference is stale because folder UIDVALIDITY changed")
            all_uids = self._search_uids(client, ["ALL"])
            scan_uids = list(reversed(all_uids))[:bounded_scan]
            if target_uid not in scan_uids:
                scan_uids.append(target_uid)
            summaries = self._fetch_headers(client, scan_uids, account_id, folder, current_uidvalidity)
            target = summaries.get(target_uid)
            if not target:
                raise MailruError("Target mail message was not found")

            chain_ids = set(_message_ids(str(target.get("message_id", ""))))
            chain_ids.update(target.get("references") or [])
            chain_ids.update(_message_ids(str(target.get("in_reply_to", ""))))
            selected: set[int] = {target_uid}
            changed = True
            while changed:
                changed = False
                for uid, row in summaries.items():
                    row_ids = set(_message_ids(str(row.get("message_id", ""))))
                    row_refs = set(row.get("references") or []) | set(_message_ids(str(row.get("in_reply_to", ""))))
                    if row_ids & chain_ids or row_refs & chain_ids:
                        before_size = len(chain_ids)
                        selected.add(uid)
                        chain_ids.update(row_ids)
                        chain_ids.update(row_refs)
                        changed = changed or len(chain_ids) != before_size
            match_mode = "references"
            if len(selected) == 1:
                normalized = _normalize_subject(str(target.get("subject", "")))
                target_participants = {
                    address.casefold()
                    for address in _addresses(
                        BytesParser(policy=policy.default).parsebytes(
                            (f"From: {target.get('from', '')}\r\nTo: {', '.join(target.get('to') or [])}\r\n\r\n").encode("utf-8")
                        ),
                        "From",
                        "To",
                    )
                }
                if normalized:
                    for uid, row in summaries.items():
                        if _normalize_subject(str(row.get("subject", ""))) != normalized:
                            continue
                        row_participants = {str(row.get("from", "")).casefold(), *[item.casefold() for item in row.get("to") or []]}
                        if not target_participants or target_participants & row_participants:
                            selected.add(uid)
                    match_mode = "subject_fallback"

            ordered_selected = sorted(
                selected,
                key=lambda uid: str(summaries.get(uid, {}).get("date") or ""),
            )[-bounded_messages:]
            messages: list[dict[str, Any]] = []
            for uid in ordered_selected:
                meta, raw = self._fetch_raw(client, uid)
                messages.append(
                    _full_message(
                        raw,
                        meta=meta,
                        account_id=account_id,
                        folder=folder,
                        uidvalidity=current_uidvalidity,
                        uid=uid,
                        max_body_chars=DEFAULT_BODY_CHARS,
                    )
                )
        return {
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "account_id": account_id,
            "folder": folder,
            "target_message_ref": message_ref,
            "thread_confidence": "high" if match_mode == "references" else "low",
            "match_mode": match_mode,
            "scanned_messages": len(scan_uids),
            "scan_truncated": len(all_uids) > len(scan_uids),
            "total_messages": len(messages),
            "messages": messages,
            "warnings": ["IMAP threading is best-effort. Subject fallback may miss or over-group messages."],
        }

    def read_attachment(self, message_ref: str, attachment_id: str, max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES) -> dict[str, Any]:
        match = re.fullmatch(r"part-(\d+)", attachment_id or "")
        if not match:
            raise ValueError("attachment_id must come from read_email attachments")
        requested_part = int(match.group(1))
        account_id, folder, expected_uidvalidity, uid = _decode_message_ref(message_ref)
        account = self.account(account_id)
        bounded_bytes = _safe_positive_int(
            max_bytes,
            default=DEFAULT_MAX_ATTACHMENT_BYTES,
            minimum=1024,
            maximum=self.max_attachment_bytes,
        )
        with self._connect(account) as client:
            current_uidvalidity = self._select_readonly(client, folder)
            if current_uidvalidity != expected_uidvalidity:
                raise StaleMessageReference("Mail message reference is stale because folder UIDVALIDITY changed")
            _, raw = self._fetch_raw(client, uid)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        for part_id, part in enumerate(message.walk(), start=1):
            if part_id != requested_part:
                continue
            if part.is_multipart():
                raise ValueError("attachment_id does not reference a leaf MIME part")
            payload = part.get_payload(decode=True) or b""
            if len(payload) > bounded_bytes:
                raise MailruError(f"Attachment exceeds the configured {bounded_bytes}-byte response limit")
            return {
                "schema_version": SCHEMA_VERSION,
                "read_only": True,
                "message_ref": message_ref,
                "attachment_id": attachment_id,
                "filename": _clean_filename(part.get_filename()),
                "content_type": part.get_content_type(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "encoding": "base64",
                "content_base64": base64.b64encode(payload).decode("ascii"),
                "warnings": ["Attachment bytes are untrusted and were not opened, executed, or written to disk."],
            }
        raise ValueError("attachment_id was not found in the message")
