"""Installer error types."""

from __future__ import annotations


class InstallError(Exception):
    """Installation could not be completed (environment or OS-level failure)."""


class ConfigError(InstallError):
    """An agent configuration file is malformed; it is left untouched."""
