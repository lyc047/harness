"""Regression tests for CLI stdin/stdout encoding.

Piped stdin on Windows decodes with the locale codec plus
``errors="surrogateescape"``, so UTF-8 input from scripts arrives as mojibake
containing lone surrogates that crash SQLite writes. ``_force_utf8_stdio``
reconfigures stdin to UTF-8 when it is not a TTY; this test exercises that
path through a real subprocess.
"""

from __future__ import annotations

import subprocess
import sys

_UTF8_LINE = "用 bash 运行 echo 测试"


def _run_stdio_probe() -> str:
    code = (
        "from harness.cli.main import _force_utf8_stdio; "
        "_force_utf8_stdio(); "
        "import sys; "
        "line = input(); "
        "sys.stdout.write('match=' + str(line == " + repr(_UTF8_LINE) + "))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=_UTF8_LINE.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8", "replace")


def test_force_utf8_stdio_piped_utf8_input() -> None:
    assert _run_stdio_probe() == "match=True"


def test_force_utf8_stdio_no_surrogates_in_piped_input() -> None:
    code = (
        "from harness.cli.main import _force_utf8_stdio; "
        "_force_utf8_stdio(); "
        "import sys; "
        "line = input(); "
        "sys.stdout.write(str(any(0xD800 <= ord(c) <= 0xDFFF for c in line)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=_UTF8_LINE.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode("utf-8", "replace") == "False"
