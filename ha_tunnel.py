from __future__ import annotations

import os
import select
import socket
import socketserver
import sys
from pathlib import Path
from typing import Any

import paramiko
from dotenv import load_dotenv
from ssh_auth import remote_key_filename


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", encoding="utf-8-sig")


class ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(socketserver.BaseRequestHandler):
    ssh_transport: paramiko.Transport
    remote_host: str
    remote_port: int

    def handle(self) -> None:
        try:
            channel = self.ssh_transport.open_channel(
                "direct-tcpip",
                (self.remote_host, self.remote_port),
                self.request.getpeername(),
            )
        except Exception:
            return
        if channel is None:
            return

        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [])
                if self.request in readable:
                    data = self.request.recv(16384)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(16384)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()
            self.request.close()


def _ssh_transport() -> paramiko.Transport:
    host = os.getenv("REMOTE_HOST", "192.168.1.10")
    port = int(os.getenv("REMOTE_PORT", "22"))
    user = os.getenv("REMOTE_USER", "")
    password = os.getenv("REMOTE_PASSWORD") or None
    timeout = float(os.getenv("REMOTE_CONNECT_TIMEOUT", "15"))
    if not user:
        raise RuntimeError("REMOTE_USER must be set in .env")

    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    transport.start_client(timeout=timeout)
    key_filename = remote_key_filename()
    if key_filename:
        try:
            transport.auth_publickey(user, paramiko.PKey.from_path(key_filename))
        except paramiko.AuthenticationException:
            if not password:
                raise
            transport.auth_password(user, password)
    elif password:
        transport.auth_password(user, password)
    else:
        raise RuntimeError("REMOTE_KEY_FILENAME or REMOTE_PASSWORD must be set")
    transport.set_keepalive(30)
    return transport


def serve(local_host: str, local_port: int, remote_host: str, remote_port: int) -> None:
    transport = _ssh_transport()
    Handler.ssh_transport = transport
    Handler.remote_host = remote_host
    Handler.remote_port = remote_port
    server = ForwardServer((local_host, local_port), Handler)
    try:
        print(
            f"forwarding {local_host}:{local_port} -> {remote_host}:{remote_port} through SSH",
            file=sys.stderr,
            flush=True,
        )
        server.serve_forever()
    finally:
        server.server_close()
        transport.close()


def main() -> None:
    local_host = os.getenv("HA_TUNNEL_LOCAL_HOST", "127.0.0.1")
    local_port = int(os.getenv("HA_TUNNEL_LOCAL_PORT", "18123"))
    remote_host = os.getenv("HA_TUNNEL_REMOTE_HOST", "127.0.0.1")
    remote_port = int(os.getenv("HA_TUNNEL_REMOTE_PORT", "8123"))
    serve(local_host, local_port, remote_host, remote_port)


if __name__ == "__main__":
    main()
