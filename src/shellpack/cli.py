"""The ``shellpack`` command.

A thin layer over :mod:`shellpack.core`: it reads the arguments the way
``cp`` does, turns a :class:`ShellpackError` into a usage error, and writes
what the library returns.
"""

from __future__ import annotations

import sys
from pathlib import Path

import rich_click as click

from . import __version__
from .core import (
    ShellpackError,
    _non_packable_reason,
    _requires,
    build_archive,
    pack,
    project_root,
)


@click.command()
@click.argument(
    "paths",
    nargs=-1,
    required=True,
    metavar="SOURCE... DEST",
    type=click.Path(path_type=Path),
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "Project root that '# shellcheck source=' paths are relative to "
        "(default: the git toplevel of SOURCE)."
    ),
)
@click.option(
    "--archive",
    is_flag=True,
    help=(
        "Pack each SOURCE together with its 'shellpack: requires' closure "
        "into a .tar.gz run by the file at its root named after SOURCE."
    ),
)
@click.option(
    "--combine",
    is_flag=True,
    help=(
        "Pack every SOURCE into ONE archive sharing a tree, run at each "
        "SOURCE's own path below the root. Implies --archive; DEST is that "
        "archive."
    ),
)
@click.option(
    "--keep",
    "kept",
    metavar="NAME",
    multiple=True,
    help=(
        "In an archive, a sourced fragment with this name (a glob) stays a "
        "file beside the scripts instead of being inlined, so it can be "
        "edited after unpacking. Repeatable."
    ),
)
@click.option(
    "--wrapper",
    "wrappers",
    metavar="NAME",
    multiple=True,
    help=(
        "A command or function that runs its first argument, which the "
        "invocation lint looks past to find the script a call runs. "
        "'sudo', 'exec' and 'env' are built in. Repeatable."
    ),
)
@click.version_option(__version__, prog_name="shellpack")
def main(
    paths: tuple[Path, ...],
    root: Path | None,
    archive: bool,
    combine: bool,
    kept: tuple[str, ...],
    wrappers: tuple[str, ...],
) -> None:
    """Inline each SOURCE's sourced fragments into a standalone script at DEST.

    Arguments read as 'cp' does: SOURCE... DEST, where DEST is a file for a
    single SOURCE and must be an existing directory for several. A DEST of
    '-' writes to stdout instead, which only one SOURCE can do.

    A SOURCE that declares 'shellpack: requires' siblings must be packed
    with --archive; one that declares none must not be. --combine lifts
    that second rule: an entry with no sibling of its own is still a
    member of a tree the others share.
    """
    try:
        _run(paths, root, archive, combine, kept, wrappers)
    except ShellpackError as error:
        raise click.UsageError(str(error)) from None


def _run(
    paths: tuple[Path, ...],
    root: Path | None,
    archive: bool,
    combine: bool,
    kept: tuple[str, ...],
    wrappers: tuple[str, ...],
) -> None:
    archive = archive or combine
    if len(paths) < 2:
        raise ShellpackError(
            f"missing destination after {str(paths[0])!r} - pass a DEST file, "
            "a DEST directory, or '-' for stdout"
        )

    *sources, dest = paths

    for source in sources:
        if not source.is_file():
            raise ShellpackError(f"{str(source)!r} is not a file")

        # Refuse here rather than let the failure surface on the target
        # host after an scp. A script that resolves a project path at
        # runtime, or runs a program in another language, packs cleanly
        # and then cannot work anywhere - inlining sourced fragments does
        # nothing for a sibling program that is not a fragment at all.
        reason = _non_packable_reason(source)
        if reason is not None:
            raise ShellpackError(f"{str(source)!r} is non-packable: {reason}")

        # The two modes are exclusive on purpose: a single file would lose
        # the siblings, and an archive of one script is a single file with
        # a wrapper. Either mismatch is a stale directive or a stale
        # manifest entry, and both should be fixed at the source.
        required = [spec for spec, _dynamic in _requires(source)]
        if required and not archive:
            raise ShellpackError(
                f"{str(source)!r} requires sibling scripts "
                f"({', '.join(required)}); pack it with --archive"
            )
        if archive and not required and not combine:
            raise ShellpackError(
                f"{str(source)!r} declares no 'shellpack: requires' sibling; "
                "pack it as a single file instead"
            )

    # '-' is the destination rather than a flag, so the cp shape holds for
    # the stdout case too: a DEST is always given. Several sources cannot
    # share it - each packed script carries its own shebang, so the
    # concatenation would not be runnable and the split point is not
    # recoverable.
    to_stdout = str(dest) == "-"

    if len(sources) > 1 and not combine:
        if to_stdout:
            raise ShellpackError(
                "several SOURCEs cannot go to stdout - each packed output is "
                "one unit; pass a DEST directory"
            )
        if not dest.is_dir():
            raise ShellpackError(f"target {str(dest)!r} is not a directory")

    # A combined archive is one output, and the entries share no name to
    # take inside a directory, so DEST names the file itself.
    if combine and dest.is_dir():
        raise ShellpackError(
            f"--combine writes one archive, but {str(dest)!r} is a directory - "
            "pass the archive path itself, or '-' for stdout"
        )

    # Each unit is one output: every SOURCE when combining, one SOURCE at
    # a time otherwise.
    units = [sources] if combine else [[source] for source in sources]

    for unit in units:
        source = unit[0]
        resolved_root = (root or project_root(source.resolve().parent)).resolve()

        if archive:
            data, members, warnings = build_archive(
                unit, resolved_root, keep=kept, wrappers=wrappers
            )
            default_name = f"{source.stem}.tar.gz"
        else:
            text, warnings, _ = pack(source, resolved_root)
            data, members, default_name = text.encode(), [], source.name

        for warning in warnings:
            click.echo(f"shellpack: warning: {warning}", err=True)

        if to_stdout:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            continue

        # cp's rule: an EXISTING directory takes the source's own name, so
        # an interactive pack is '/tmp' rather than the basename typed
        # twice. A path with nothing at it is the destination FILE, so
        # 'shellpack x.sh /tmp/pack' writes the file 'pack'. The 'wrote'
        # line reports whichever won.
        target = dest / default_name if dest.is_dir() else dest
        target.write_bytes(data)
        if not archive:
            target.chmod(0o755)
        click.echo(f"shellpack: wrote {target}", err=True)
        for member in members:
            click.echo(f"shellpack:   {member}", err=True)
