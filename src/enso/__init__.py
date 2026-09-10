"""Enso — bridge chat platforms to agent CLIs running on your machine."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("enso")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"
