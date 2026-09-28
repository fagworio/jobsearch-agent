"""Fronteira controlada entre o backend V2 e a extensão Chrome."""

from .protocol import (
    COMMANDS,
    PROTOCOL_VERSION,
    Command,
    ProtocolError,
    Request,
    Response,
)
from .client import NativeMessagingClient, open_page_html

__all__ = [
    "COMMANDS",
    "PROTOCOL_VERSION",
    "Command",
    "ProtocolError",
    "Request",
    "Response",
    "NativeMessagingClient",
    "open_page_html",
]
