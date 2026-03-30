"""Enso - Control your fav agent CLIs from your phone."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("enso")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
