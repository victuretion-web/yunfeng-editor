"""Compatibility shim for Python 3.13+ where stdlib audioop was removed.

Pydub falls back to importing ``pyaudioop`` on newer Python versions.
This module bridges that import to the maintained ``audioop-lts`` package,
which restores the ``audioop`` module name.
"""

from audioop import *  # noqa: F401,F403
