"""shellpack packs a shell script and the fragments it sources into one
standalone file, or a script and the sibling scripts it runs into one
archive, for hosts that do not have the checkout.

The command is ``shellpack``; see :mod:`shellpack.core` for the library and
for what the ``# shellpack:`` directives mean.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .core import (
    WRAPPERS,
    ShellpackError,
    archive_members,
    build_archive,
    pack,
    project_root,
    requires_siblings,
    resolve_closure,
)

try:
    __version__ = version("shellpack")
except PackageNotFoundError:  # a checkout imported without being installed
    __version__ = "0+unknown"

__all__ = [
    "WRAPPERS",
    "ShellpackError",
    "__version__",
    "archive_members",
    "build_archive",
    "pack",
    "project_root",
    "requires_siblings",
    "resolve_closure",
]
