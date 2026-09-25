"""Adapters de ATS. Um provider por incremento; nenhum framework generico."""

from .lever import find_apply_url, inspect_form

__all__ = ["find_apply_url", "inspect_form"]
