"""Fronteira controlada entre o backend V2 e a extensão Chrome."""

from .protocol import (
    COMMANDS,
    PROTOCOL_VERSION,
    Command,
    ProtocolError,
    Request,
    Response,
)
from .client import open_page_html

__all__ = [
    "COMMANDS",
    "PROTOCOL_VERSION",
    "Command",
    "ProtocolError",
    "Request",
    "Response",
    "open_page_html",
]
