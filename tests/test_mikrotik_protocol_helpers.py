from __future__ import annotations

import pytest

import mikrotik_mcp


class FakeSocket:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = chunks or []
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        assert len(chunk) == size
        return chunk


@pytest.mark.parametrize(
    ("length", "encoded"),
    [
        (0x7F, b"\x7f"),
        (0x80, b"\x80\x80"),
        (0x4000, b"\xc0\x40\x00"),
        (0x200000, b"\xe0\x20\x00\x00"),
    ],
)
def test_write_len_matches_routeros_boundaries(length: int, encoded: bytes) -> None:
    sock = FakeSocket()

    mikrotik_mcp._write_len(sock, length)

    assert bytes(sock.sent) == encoded


def test_write_word_prefixes_utf8_byte_length() -> None:
    sock = FakeSocket()

    mikrotik_mcp._write_word(sock, "wifi")

    assert bytes(sock.sent) == b"\x04wifi"


def test_read_word_decodes_utf8_payload() -> None:
    sock = FakeSocket([b"\x04", b"test"])

    assert mikrotik_mcp._read_word(sock) == "test"


def test_parse_reply_collects_sentence_metadata_and_data_rows() -> None:
    rows = mikrotik_mcp._parse_reply(
        [
            ["!re", "=name=ether1", "=running=true"],
            ["!done"],
        ]
    )

    assert rows == [
        {"reply": "!re", "name": "ether1", "running": "true"},
        {"reply": "!done"},
    ]
