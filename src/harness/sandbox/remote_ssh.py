"""Remote SSH sandbox.

Runs commands on a rented server over SSH (paramiko), so local directories are
never touched. Each command runs under a configured work directory
(``mkdir -p && cd``), giving a stable remote workspace. paramiko is blocking,
so I/O happens in a worker thread via ``asyncio.to_thread``.

A fake ``transport`` can be injected for tests; it only needs an ``exec``
method matching :meth:`SSHSandbox._run_sync`.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from harness.observability.logging import get_logger
from harness.sandbox.base import SandboxResult

logger = get_logger("sandbox.ssh")


class SSHSandbox:
    """Runs commands on a remote host over SSH with a fixed work directory."""

    name = "ssh"

    def __init__(
        self,
        *,
        host: str,
        port: int = 22,
        user: str = "",
        key_path: str = "",
        workdir: str = "~/harness-workspace",
        transport: Any | None = None,
        connect_timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._key_path = key_path
        self._workdir = workdir
        self._transport = transport  # injectable fake for tests
        self._connect_timeout = connect_timeout
        self._client: Any | None = None

    @property
    def workdir(self) -> str:
        return self._workdir

    async def check_available(self) -> bool:
        try:
            await asyncio.to_thread(self._connect)
            return True
        except Exception as exc:  # noqa: BLE001 — availability probe
            logger.warning("ssh sandbox unavailable: %s", exc)
            return False

    async def run_command(
        self, command: str, *, timeout: float | None = None
    ) -> SandboxResult:
        try:
            return await asyncio.to_thread(self._run_sync, command, timeout)
        except TimeoutError:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"timed out after {timeout}s on remote host",
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 — surface remote failures cleanly
            logger.warning("ssh exec failed: %s", exc)
            return SandboxResult(
                exit_code=-1, stdout="", stderr=f"ssh error: {type(exc).__name__}: {exc}"
            )

    # -- internals -- #

    def _connect(self) -> None:
        if self._transport is not None or self._client is not None:
            return
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self._host,
            port=self._port,
            username=self._user or None,
            key_filename=self._key_path or None,
            timeout=self._connect_timeout,
        )
        self._client = client

    def _run_sync(self, command: str, timeout: float | None) -> SandboxResult:
        if self._transport is not None:
            return cast(SandboxResult, self._transport.exec(command, timeout=timeout))

        self._connect()
        client = self._client
        assert client is not None  # _connect() guarantees a connected client
        remote_command = self._build_remote_command(command)
        _stdin, stdout, stderr = client.exec_command(remote_command, timeout=timeout)
        out = _decode(stdout.read())
        err = _decode(stderr.read())
        code = stdout.channel.recv_exit_status()
        return SandboxResult(exit_code=code, stdout=out, stderr=err)

    def _build_remote_command(self, command: str) -> str:
        """Prefix a command with workdir setup (mkdir + cd)."""
        return f"mkdir -p {self._workdir} && cd {self._workdir} && {command}"

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
