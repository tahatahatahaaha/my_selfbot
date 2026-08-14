"""
Message Parser layer.

Pure text -> structure. Knows nothing about Telegram events, plugins, or
the AI — just whether a string is a `PREFIX`-command and, if so, its
command word and remaining argument text.
"""

from dataclasses import dataclass

from config import PREFIX


@dataclass
class ParsedCommand:
    cmd: str    # lowercased first word after the prefix
    arg: str    # lowercased remainder (empty string if none)
    body: str   # original-case text after the prefix, for handlers that need it verbatim


def parse(text: str) -> ParsedCommand | None:
    """Returns a ParsedCommand if `text` is a `PREFIX`-prefixed command with
    a non-empty body, else None (not a command at all, or an empty one)."""
    if text is None or not text.startswith(PREFIX):
        return None

    body = text[len(PREFIX):].strip()
    if not body:
        return None

    parts = body.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    return ParsedCommand(cmd=cmd, arg=arg, body=body)
