"""Stream-event rendering shared by the REPL and the /plan command."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from harness.core.runner import RunDone, ToolResultEvent
from harness.llm.base import StreamReasoning, StreamText, StreamToolCall

TRUNCATE_CHARS = 800


def render_stream_event(event: object, console: Console) -> None:
    """Render a single runner/planning stream event to the console."""
    if isinstance(event, StreamText):
        console.print(event.text, end="")
    elif isinstance(event, StreamReasoning):
        console.print(f"[dim italic]{event.text}[/]", end="")
    elif isinstance(event, StreamToolCall) and event.tool_call:
        tc = event.tool_call
        console.print(f"\n[bold cyan]▶ {tc.name}[/]({tc.arguments})")
    elif isinstance(event, ToolResultEvent):
        body = event.result.content
        if len(body) > TRUNCATE_CHARS:
            truncated = f"(truncated {len(event.result.content)} chars)"
            body = body[:TRUNCATE_CHARS] + f"\n… [dim]{truncated}[/]"
        color = "red" if event.result.is_error else "green"
        console.print(
            Panel(body, title=f"← {event.tool_call.name}", border_style=color, expand=False)
        )
    elif isinstance(event, RunDone):
        console.print("")
