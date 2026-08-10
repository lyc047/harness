"""Sandbox isolation providers."""

from harness.config import Settings
from harness.sandbox.base import SandboxedExecutor, SandboxProvider, SandboxResult
from harness.sandbox.local import LocalSandbox
from harness.sandbox.remote_ssh import SSHSandbox


def build_sandbox(settings: Settings) -> SandboxProvider:
    """Build the sandbox for the configured mode (local | ssh)."""
    if settings.sandbox_mode == "ssh":
        if not settings.sandbox_host:
            raise ValueError("SANDBOX_MODE=ssh requires SANDBOX_HOST to be set")
        return SSHSandbox(
            host=settings.sandbox_host,
            port=settings.sandbox_port,
            user=settings.sandbox_user,
            key_path=settings.sandbox_key_path,
            workdir=settings.sandbox_workdir,
        )
    return LocalSandbox()


__all__ = [
    "SandboxProvider",
    "SandboxResult",
    "SandboxedExecutor",
    "LocalSandbox",
    "SSHSandbox",
    "build_sandbox",
]
