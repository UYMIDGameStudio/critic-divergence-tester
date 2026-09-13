"""Compatibility entry point for the packaged browser shell."""
from studio_web import shell_template

SHELL_TEMPLATE = shell_template()

__all__ = ["SHELL_TEMPLATE"]
