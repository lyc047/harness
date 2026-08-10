"""Built-in file/shell tools available to every agent.

For P1 these execute directly on the local machine (development mode). From
P7 onwards the runner may delegate their execution to a SandboxProvider.
"""

from harness.tools.builtin.files import glob_files, grep_files, read_file, write_file
from harness.tools.builtin.shell import bash
from harness.tools.registry import ToolRegistry

__all__ = ["read_file", "write_file", "glob_files", "grep_files", "bash", "builtin_registry"]


def builtin_registry() -> ToolRegistry:
    """A registry pre-populated with all built-in tools."""
    registry = ToolRegistry()
    registry.register_all([read_file, write_file, glob_files, grep_files, bash])
    return registry
