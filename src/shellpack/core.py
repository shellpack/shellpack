"""Pack a shell script and what it sources, or runs, into something that
travels on its own.

Scripts are written against shared fragments: ``source _console.sh``,
``source ../lib/_aws.sh``. A host without the checkout cannot run them.
``pack`` inlines every sourced fragment into the script in place, so one
file is enough; ``archive_members`` and ``build_archive`` do the same for
a script that also runs sibling scripts, packing the whole set as one tree.

``shfmt`` is required. Its ``--to-json`` syntax tree is what tells a
``source`` statement from the same words inside a heredoc or a string, and
what the invocation lint below reads; a script that writes another script
would otherwise have its own text rewritten.

Resolution of ``source`` / ``.`` lines, in order of preference:

1. A ``# shellcheck source=<path>`` directive on the preceding line, the
   path resolved relative to the project root. This is also what makes
   ``shellcheck -x`` follow the include, so it is the most reliable signal.
2. The ``"$(dirname -- "${BASH_SOURCE[0]}")/<rel>"`` idiom, ``<rel>``
   resolved relative to the sourcing file's own directory.
3. A plain relative path (``source ./_lib.sh``, ``source _lib.sh``) with no
   shell expansion, resolved relative to the sourcing file's directory.

A ``source`` that resolves to nothing (a path built from a variable, or a
file that does not exist at pack time) is left as it is, so an optional
runtime include keeps working. Each fragment is inlined once; a later
``source`` of the same file becomes a marker comment. File-level
``# shellcheck disable=`` directives from fragments are hoisted to the top
of the packed script, since they lose their file scope once inlined, and a
fragment's ``# shellcheck shell=`` is dropped when it matches the entry's
shebang and kept with a warning when it does not.

Directives
----------

A script states what shellpack may do with it in its leading comment block,
the comments before its first command. There are three directives:

``# shellpack: non-packable - <REASON>``
    The script cannot work outside a checkout, and shellpack refuses it. Use
    it for anything that resolves a project path at runtime or invokes a
    program written in another language: inlining sourced fragments does
    nothing for either, so such a script packs cleanly and then fails on the
    target host.

``# shellpack: requires <PATH>``
    The script executes the sibling shell script at ``<PATH>``, written
    relative to the script itself exactly as the call is written in the code
    (``./configure.sh``, ``../tests/check.sh``). One directive per sibling.
    Such a script cannot be a single file: shellpack refuses to pack it
    alone and packs it as an archive instead. Every script in the transitive
    ``requires`` closure is packed and stored at its path below the
    closure's common directory, the smallest tree in which the relative
    calls resolve as they do in the checkout. The archive is run by the file
    at its root named after the entry script: the entry itself when the
    closure's common directory is its own, otherwise a forwarder of that
    name, so the archive runs from wherever it was unpacked.

``# shellpack: requires-dynamic <PATH>``
    The same, for a script the entry runs without naming it: a runner that
    finds its steps with ``find ./steps`` writes down no call to any one
    step. The target is packed exactly as with ``requires``; the difference
    is that the lint below does not expect to see a call to it and says
    nothing when it finds none.

Archived scripts are also linted, against that same tree, so quoting,
heredocs and comments are the parser's business rather than this file's. A
``./`` or ``../`` path to a ``.sh`` or ``.py`` file that a call runs, as
its command, past any wrapper such as ``exec``, ``sudo`` or ``env``, and
that no ``requires`` directive declares fails the pack. An element of an
array counts too: a list of scripts is there to be run in a loop. A path
that is not a literal word stays invisible, whether it is built from a
variable or sits in a string handed to ``eval``. The directive is therefore
the source of truth and the lint is the drift check. A project whose
scripts run siblings through a function of its own (``run_command
./x.sh``) names it in ``wrappers`` so the lint looks past it.

One archive for several entries
-------------------------------

Several entries may share one archive instead of getting one each. The
tree is built exactly as for a single entry, over the union of their
closures: the common directory is the smallest tree in which every entry's
relative calls resolve, and a script two entries require is stored once.
There is no forwarder, one name being one file: each entry is run at the
path it has in the checkout, below the archive root.

Config fragments kept as files
------------------------------

A single packed file inlines everything it sources. An archive can keep
some of it as files instead: a fragment whose name is listed in ``keep``
(a per-directory ``_env.sh`` holding configuration, say) and that sits
inside the archive tree stays a ``source`` line, and the file becomes a
member at its own path, so it is still editable after unpacking and a
local override beside it still applies. A kept fragment at the archive
root is where such a chain leaves the tree, so that one is packed: the
fragments it sources from outside the tree are inlined into it. The
``# shellcheck source=`` directive on a kept line is rewritten to the
archive-relative path, so ``shellcheck -x`` from the archive root follows
it.
"""

from __future__ import annotations

import functools
import gzip
import io
import json
import os
import re
import subprocess
import tarfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from fnmatch import fnmatch
from pathlib import Path


class ShellpackError(Exception):
    """A script, a directive or an argument that shellpack refuses.

    The message is complete on its own: what was refused and, where there
    is one, the change at the source that fixes it.
    """


# A 'source FILE' or '. FILE' statement (must have an argument).
_SOURCE_RE = re.compile(r"^\s*(?:source|\.)\s+(?P<arg>\S.*?)\s*$")
# A '# shellcheck source=PATH' directive.
_DIRECTIVE_RE = re.compile(r"^\s*#\s*shellcheck\s+source=(?P<path>\S+)")
# The '$(dirname -- "${BASH_SOURCE[0]}")/REL' idiom; capture REL.
_DIRNAME_RE = re.compile(r"\$\(\s*dirname\b[^)]*\)\s*/(?P<rel>[^\"']+)")
# A file-level '# shellcheck disable=CODE,CODE' directive.
_DISABLE_RE = re.compile(r"^\s*#\s*shellcheck\s+disable=(?P<codes>\S+)")
# A '# shellcheck shell=NAME' directive (file-scoped; meaningful only at file top).
_SHELL_RE = re.compile(r"^\s*#\s*shellcheck\s+shell=(?P<shell>\S+)")
# A '# shellpack: non-packable - <REASON>' directive.
_NON_PACKABLE_RE = re.compile(
    r"^\s*#\s*shellpack:\s*non-packable\b[\s:-]*(?P<reason>.*?)\s*$"
)
# A '# shellpack: requires <PATH>' directive, or its 'requires-dynamic'
# form for a target the entry runs without naming it.
_REQUIRES_RE = re.compile(
    r"^\s*#\s*shellpack:\s*requires(?P<dynamic>-dynamic)?\s+(?P<path>\S+)\s*$"
)

# Interpreters a 'requires' target may have: it is packed as a shell script.
_SHELLS = {"bash", "sh", "dash", "ksh", "zsh"}

# --- The invocation lint -------------------------------------------------
# A './' or '../' path to a script.
_SCRIPT_PATH_RE = re.compile(r"\.{1,2}/[A-Za-z0-9_./-]+\.(?:sh|py)")
# Words that precede the command they run without being it. A project
# adds its own (a 'run_command' function, say) through 'wrappers'.
WRAPPERS = frozenset({"command", "env", "exec", "nice", "sudo", "time"})
# 'VAR=value' written as a word rather than parsed as an assignment, which
# is how 'env' takes one.
_ASSIGN_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# shfmt prints the syntax tree the lint reads; '--to-json' arrived in 3.7.0
# and reads stdin only.
_SHFMT_TO_JSON = ("shfmt", "--to-json")
_SHFMT_MIN_VERSION = "3.7"


def _shebang_shell(line: str) -> str | None:
    """Return the interpreter basename from a shebang line, or None.

    Handles the plain form (``#!/bin/bash``) and the env form
    (``#!/usr/bin/env [-S] bash ...``), skipping env options.
    """
    if not line.startswith("#!"):
        return None
    parts = line[2:].strip().split()
    if not parts:
        return None
    interp = parts[0].rsplit("/", 1)[-1]
    if interp != "env":
        return interp
    for arg in parts[1:]:
        if arg.startswith("-"):  # env options such as -S
            continue
        return arg.rsplit("/", 1)[-1]
    return None


def _leading_disable_codes(lines: list[str]) -> set[str]:
    """Collect 'shellcheck disable=' codes from a file's leading comment block.

    Such directives are file-scoped only while they precede the first command;
    once a fragment is inlined they lose that scope, so the codes are hoisted to
    the top of the packed script instead.
    """
    codes: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        match = _DISABLE_RE.match(stripped)
        if match:
            codes.update(match.group("codes").split(","))
            continue
        if stripped.startswith("#"):
            continue
        break  # first real command ends the file-level directive block
    return codes


def _leading_comments(path: Path) -> Iterator[str]:
    """Yield the stripped comment lines of ``path``'s leading block.

    The same scope ``_leading_disable_codes`` uses: a directive below the
    first command is not a statement about the file, and scanning the whole
    file would also match the line inside a heredoc that documents the very
    directive being looked for.
    """
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        if not stripped.startswith("#"):
            break  # first real command ends the file-level directive block
        yield stripped


def _non_packable_reason(path: Path) -> str | None:
    """Return why ``path`` refuses to be packed, or None if it is packable."""
    for stripped in _leading_comments(path):
        match = _NON_PACKABLE_RE.match(stripped)
        if match:
            return match.group("reason") or "no reason given"
    return None


def _requires(path: Path) -> list[tuple[str, bool]]:
    """Return what ``path`` declares with 'shellpack: requires'.

    One ``(sibling path, dynamic)`` pair per directive, ``dynamic`` telling
    the two spellings apart: a 'requires-dynamic' target is run without
    being named, so the invocation lint does not look for a call to it.
    """
    return [
        (match.group("path"), bool(match.group("dynamic")))
        for stripped in _leading_comments(path)
        if (match := _REQUIRES_RE.match(stripped))
    ]


def requires_siblings(path: Path) -> bool:
    """Whether ``path`` declares a sibling script, so it needs an archive.

    A script that runs a sibling cannot travel as a single packed file:
    inlining what it sources does nothing for a program it executes.
    """
    return bool(_requires(path))


def project_root(start: Path) -> Path:
    """The root that ``# shellcheck source=`` paths are relative to.

    The git toplevel of ``start`` when it is inside a repository, else the
    filesystem root. A caller that knows better passes its own.
    """
    try:
        top = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if top:
            return Path(top)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return start.anchor and Path(start.anchor) or start


def _resolve_source(
    arg: str, directive: str | None, sourcing_file: Path, root: Path
) -> Path | None:
    """Resolve a source argument to a real file, or None if not resolvable."""
    base = sourcing_file.parent

    if directive is not None:
        candidate = (root / directive).resolve()
        return candidate if candidate.is_file() else None

    # '$(dirname -- "${BASH_SOURCE[0]}")/REL' — relative to the sourcing file.
    match = _DIRNAME_RE.search(arg)
    if match:
        candidate = (base / match.group("rel")).resolve()
        return candidate if candidate.is_file() else None

    # Plain relative path with no shell expansion.
    cleaned = arg.strip().strip('"').strip("'")
    if "$" in cleaned or "`" in cleaned:
        return None
    candidate = (base / cleaned).resolve()
    return candidate if candidate.is_file() else None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _leading_shell(lines: list[str]) -> str | None:
    """Return the shell a fragment declares with a leading 'shellcheck shell='."""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        match = _SHELL_RE.match(stripped)
        if match:
            return match.group("shell")
        if stripped.startswith("#"):
            continue
        break
    return None


def pack(
    entry: Path, root: Path, *, keep: Callable[[Path], str | None] | None = None
) -> tuple[str, list[str], list[Path]]:
    """Return the packed text for ``entry``, any warnings, and the kept files.

    ``keep`` decides, for a resolved ``source`` target, whether it stays a
    runtime file rather than being inlined: it returns the path to write
    into the line's ``# shellcheck source=`` directive, or None to inline.
    Kept targets are returned so the caller can pack them in turn.
    """
    entry = entry.resolve()
    out: list[str] = []
    seen: set[Path] = set()
    kept: list[Path] = []
    hoisted_disables: set[str] = set()
    hoist_at = 0  # where to insert the merged disable directive once known
    warnings: list[str] = []

    entry_lines = entry.read_text().splitlines()
    # A fragment packed as an entry (a kept config fragment at an
    # archive's root) has no shebang; its leading 'shell=' directive
    # names the shell instead.
    entry_shell = (
        _shebang_shell(entry_lines[0]) if entry_lines else None
    ) or _leading_shell(entry_lines)

    def process(path: Path, *, is_entry: bool) -> None:
        nonlocal hoist_at
        rel = _rel(path, root)
        lines = path.read_text().splitlines()
        hoisted_disables.update(_leading_disable_codes(lines))
        # What the parser, rather than the shape of a line, decides: which
        # lines hold a 'source' statement and which are heredoc body.
        sourced_at = _source_statements(path)
        heredoc_at = _heredoc_lines(path)
        index = 0

        # Keep the entry's shebang; strip any inlined fragment's shebang.
        if lines and lines[0].startswith("#!"):
            if is_entry:
                out.append(lines[0])
            index = 1

        if is_entry:
            out.append(
                f"# --- shellpack: packed from {rel}; "
                "edit the source, not this file. ---"
            )
            hoist_at = len(out)  # after the shebang/header, before any code

        if not is_entry:
            out.append(f"# >>> shellpack: begin {rel}")

        held_directive: str | None = None
        in_leading = True  # leading comment/blank block, before the first command
        for number, line in enumerate(lines[index:], index + 1):
            stripped = line.strip()

            # Heredoc body is data the script emits: copied out as it is,
            # whatever it happens to say.
            if number in heredoc_at:
                if held_directive is not None:
                    out.append(f"# shellcheck source={held_directive}")
                    held_directive = None
                out.append(line)
                continue

            # 'shell=' is file-scoped and redundant once inlined under the
            # entry's shebang. Drop it when it matches the entry shell; keep it
            # and warn when it does not (a real inconsistency worth surfacing).
            # The entry's own directive is what a shebang-less entry (a
            # kept fragment packed at an archive's root) has instead of a
            # shebang, so it stays.
            shell_match = _SHELL_RE.match(line)
            if shell_match:
                if held_directive is not None:
                    out.append(f"# shellcheck source={held_directive}")
                    held_directive = None
                name = shell_match.group("shell")
                if is_entry:
                    out.append(line)
                    continue
                if entry_shell is not None and name == entry_shell:
                    continue  # redundant: drop it
                if entry_shell is not None and name != entry_shell:
                    warnings.append(
                        f"{rel}: '# shellcheck shell={name}' does not match the "
                        f"entry shebang shell '{entry_shell}'; keeping the directive"
                    )
                out.append(line)
                continue

            # File-level 'disable=' directives (in the leading block) are
            # hoisted to the top of the packed script; drop the originals so
            # they do not pile up. Inline 'disable=' directives (after code
            # starts) apply to a specific line and are left in place.
            if in_leading and _DISABLE_RE.match(line):
                continue

            directive_match = _DIRECTIVE_RE.match(line)
            if directive_match:
                # Hold it; only emit if the next line is not a source we inline.
                held_directive = directive_match.group("path")
                continue

            source_match = _SOURCE_RE.match(line) if number in sourced_at else None
            if source_match:
                in_leading = False  # a source statement is a command
                directive = held_directive
                held_directive = None
                target = _resolve_source(
                    source_match.group("arg"), directive, path, root
                )
                if target is not None:
                    kept_as = keep(target) if keep is not None else None
                    if kept_as is not None:
                        # Stays a runtime file: the line is kept, and its
                        # directive now points where the file will be.
                        kept.append(target)
                        out.append(f"# shellcheck source={kept_as}")
                        out.append(line)
                        continue
                    if target in seen:
                        out.append(
                            f"# >>> shellpack: {_rel(target, root)} already inlined"
                        )
                    else:
                        seen.add(target)
                        process(target, is_entry=False)
                    continue
                # Unresolvable: keep the original line, and its directive with
                # it. An optional runtime-only include (a local override file)
                # carries 'source=/dev/null' precisely so shellcheck stays
                # quiet about it, and the packed script is shellchecked too.
                if directive is not None:
                    out.append(f"# shellcheck source={directive}")
                out.append(line)
                continue

            if held_directive is not None:
                out.append(f"# shellcheck source={held_directive}")
                held_directive = None
            if stripped and not stripped.startswith("#"):
                in_leading = False  # first real command ends the leading block
            out.append(line)

        if held_directive is not None:
            out.append(f"# shellcheck source={held_directive}")
        if not is_entry:
            out.append(f"# <<< shellpack: end {rel}")

    seen.add(entry)
    process(entry, is_entry=True)

    if hoisted_disables:
        directive = "# shellcheck disable=" + ",".join(sorted(hoisted_disables))
        out.insert(hoist_at, directive)

    return "\n".join(out) + "\n", warnings, kept


# ======================================================================
# Archives
# ----------------------------------------------------------------------


@functools.cache
def _syntax_tree(path: Path) -> dict:
    """Return the mvdan/sh syntax tree of ``path``, as shfmt prints it.

    Cached: the inliner and the invocation lint both read the tree of the
    same file, and nothing rewrites a source file mid-run.
    """
    with path.open("rb") as source:
        try:
            result = subprocess.run(
                _SHFMT_TO_JSON,
                stdin=source,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            raise ShellpackError(
                f"{_SHFMT_TO_JSON[0]} is not on PATH; the invocation lint reads "
                f"the syntax tree it prints (version {_SHFMT_MIN_VERSION} or "
                "newer)"
            ) from None

    if result.returncode != 0:
        raise ShellpackError(
            f"{' '.join(_SHFMT_TO_JSON)} failed on {str(path)!r}: "
            f"{result.stderr.strip()}"
        )

    return json.loads(result.stdout)


def _source_statements(path: Path) -> set[int]:
    """Return the lines of ``path`` on which a 'source' statement begins.

    A ``source`` in a heredoc body or in a multi-line string is text the
    script writes or prints, not an include; only a line the parser calls
    a command belongs to the inliner.
    """
    lines: set[int] = set()

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("Type") == "CallExpr":
            args = node.get("Args") or []
            if args and _word_text(args[0]) in ("source", "."):
                lines.add(args[0]["Pos"]["Line"])
        for value in node.values():
            visit(value)

    visit(_syntax_tree(path))

    return lines


def _heredoc_lines(path: Path) -> set[int]:
    """Return the lines of ``path`` that are heredoc body, terminator and all.

    The body is data the script emits. Directives are not directives there
    and ``source`` is not an include, so those lines are copied out as they
    are.
    """
    lines: set[int] = set()

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        # A redirection carries no 'Type'; the heredoc body hangs off it
        # as 'Hdoc', spanning its first line to the terminator.
        body = node.get("Hdoc")
        if body:
            lines.update(range(body["Pos"]["Line"], body["End"]["Line"] + 1))
        for value in node.values():
            visit(value)

    visit(_syntax_tree(path))

    return lines


def _word_text(word: dict) -> str | None:
    """Return a word's literal text, or None when it is not all literal.

    Quoting is spelling, not meaning: ``./x.sh``, ``'./x.sh'`` and
    ``"./x.sh"`` all run the same file. A word holding an expansion or a
    substitution has no text known here and gives None.
    """
    text: list[str] = []

    for part in word.get("Parts") or []:
        kind = part.get("Type")
        if kind in ("Lit", "SglQuoted"):
            text.append(part.get("Value", ""))
        elif kind == "DblQuoted":
            for inner in part.get("Parts") or []:
                if inner.get("Type") != "Lit":
                    return None
                text.append(inner.get("Value", ""))
        else:
            return None

    return "".join(text)


def _called_script(
    args: list[dict], wrappers: frozenset[str]
) -> tuple[int, str] | None:
    """Return the sibling script a call runs, as (line number, path).

    Walks the words until the command itself is reached: a wrapper, one of
    its flags and a 'VAR=value' argument to 'env' all stand before it, and
    a word whose text is unknown is stepped over rather than stopping the
    walk, so 'env ${VAR:+FOO=1} ../x.sh' is still seen. Any other word is
    the command, and if it is not a script path this call runs something
    else.
    """
    for word in args:
        text = _word_text(word)
        if text is None:
            continue
        if _SCRIPT_PATH_RE.fullmatch(text):
            return word["Pos"]["Line"], text
        if text in wrappers or text.startswith("-") or _ASSIGN_WORD_RE.match(text):
            continue
        return None

    return None


def _array_scripts(assigns: list[dict]) -> Iterator[tuple[int, str]]:
    """Yield the sibling scripts listed in array assignments.

    A list of script paths exists to be run in a loop, so the archive needs
    every one of them even though no call names any.
    """
    for assign in assigns:
        for element in (assign.get("Array") or {}).get("Elems") or []:
            word = element.get("Value")
            text = _word_text(word) if word else None
            if text and _SCRIPT_PATH_RE.fullmatch(text):
                yield word["Pos"]["Line"], text


def _invoked_scripts(path: Path, wrappers: frozenset[str]) -> list[tuple[int, str]]:
    """Return ``(line number, path)`` for every sibling script ``path`` runs.

    Reads the syntax tree rather than the text, so comments, heredocs and
    quoting are the parser's business: the usage line an installer echoes
    is a word of an 'echo' call and never a command. See ``_called_script``
    and ``_array_scripts`` for what counts as running one.
    """
    found: list[tuple[int, str]] = []

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return

        if node.get("Type") == "CallExpr":
            called = _called_script(node.get("Args") or [], wrappers)
            if called:
                found.append(called)
            found.extend(_array_scripts(node.get("Assigns") or []))

        for value in node.values():
            visit(value)

    visit(_syntax_tree(path))

    return sorted(set(found))


def resolve_closure(entry: Path, root: Path) -> dict[Path, set[Path]]:
    """Return the transitive 'shellpack: requires' closure of ``entry``.

    Maps each script (the entry first, then in discovery order) to the set
    of scripts its own directives declare, every path resolved. Fails on a
    target that is missing, non-packable, or not a shell script.
    """
    closure: dict[Path, set[Path]] = {}
    pending = [entry.resolve()]

    while pending:
        script = pending.pop(0)
        if script in closure:
            continue
        rel = _rel(script, root)

        reason = _non_packable_reason(script)
        if reason is not None:
            raise ShellpackError(f"{rel!r} is non-packable: {reason}")

        first = script.read_text().splitlines()[:1]
        shell = _shebang_shell(first[0]) if first else None
        if shell not in _SHELLS:
            raise ShellpackError(
                f"{rel!r} is not a shell script (shebang interpreter "
                f"{shell!r}); shellpack cannot pack it"
            )

        declared: set[Path] = set()
        for spec, _dynamic in _requires(script):
            target = (script.parent / spec).resolve()
            if not target.is_file():
                raise ShellpackError(f"{rel!r} requires {spec!r}, which does not exist")
            declared.add(target)
            pending.append(target)
        closure[script] = declared

    return closure


def _lint_invocations(
    closure: dict[Path, set[Path]], root: Path, wrappers: frozenset[str]
) -> list[str]:
    """Check every direct sibling invocation against the declarations.

    An undeclared invocation is an error: the archive would unpack without
    the script it calls. A declaration with no visible invocation is only a
    warning, since the lint cannot see a path run through a variable — and
    no warning at all for a 'requires-dynamic' target, which says up front
    that there is no call to find.
    """
    warnings: list[str] = []

    for script, declared in closure.items():
        rel = _rel(script, root)
        dynamic = {
            (script.parent / spec).resolve()
            for spec, is_dynamic in _requires(script)
            if is_dynamic
        }
        invoked: set[Path] = set()
        for number, spec in _invoked_scripts(script, wrappers):
            target = (script.parent / spec).resolve()
            invoked.add(target)
            if target in declared or target == script:
                continue
            hint = (
                "a Python script cannot be packed; mark the script non-packable"
                if spec.endswith(".py")
                else f"add '# shellpack: requires {spec}' to its leading comments"
            )
            raise ShellpackError(
                f"{rel}:{number}: invokes {spec!r}, which no 'shellpack: "
                f"requires' directive declares - {hint}"
            )
        for target in sorted(declared - invoked - dynamic):
            warnings.append(
                f"{rel}: requires {_rel(target, root)} but no direct "
                "invocation of it is visible"
            )

    return warnings


def _entry_script(entry_arcname: str, entry_rel: str) -> str:
    """Return the forwarding script stored at the archive root."""
    return (
        "#!/usr/bin/env bash\n"
        f"# --- shellpack: archive entry for {entry_rel}; "
        "edit the source, not this file. ---\n"
        "#\n"
        "# Runs the entry script from wherever the archive was unpacked and\n"
        "# forwards every argument to it.\n"
        "set -euo pipefail\n"
        f'exec "$(dirname -- "${{BASH_SOURCE[0]:-$0}}")/{entry_arcname}" "$@"\n'
    )


def archive_members(
    entries: Sequence[Path],
    root: Path,
    *,
    keep: Iterable[str] = (),
    wrappers: Iterable[str] = (),
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return the 'requires' closures of ``entries`` as archive path -> text.

    Members sit below the closures' common directory, the smallest tree
    that keeps every relative call valid. A sourced fragment whose name
    matches a pattern in ``keep`` and that sits inside that tree travels
    as a file rather than being inlined (see the module docstring).
    ``wrappers`` are words the invocation lint steps over to reach the
    command, in addition to the built-in ``WRAPPERS``. Also returns the
    archive-relative path that runs each entry, in the order given, and
    any warnings.

    Several entries share one tree, so a script more than one of them
    requires is stored once and each entry keeps the path it has in the
    checkout. Only a lone entry gets the root file named after it, since
    one name is one file.

    ``build_archive`` tars this; a caller that wants the closure as a
    directory writes the members itself.
    """
    resolved = [entry.resolve() for entry in entries]

    # Merged, so a script two entries require is linted once and stored
    # once. Reaching it twice yields the same declarations either way.
    closure: dict[Path, set[Path]] = {}
    for entry in resolved:
        for script, declared in resolve_closure(entry, root).items():
            closure.setdefault(script, declared)

    warnings = _lint_invocations(closure, root, WRAPPERS | frozenset(wrappers))

    base = Path(os.path.commonpath([str(script.parent) for script in closure]))
    kept_names = tuple(keep)

    def keep_as(target: Path) -> str | None:
        if target.is_relative_to(base) and any(
            fnmatch(target.name, pattern) for pattern in kept_names
        ):
            return target.relative_to(base).as_posix()
        return None

    members: dict[str, str] = {}
    pending = list(closure)
    while pending:
        path = pending.pop(0)
        arcname = path.relative_to(base).as_posix()
        if arcname in members:
            continue
        text, pack_warnings, kept = pack(path, root, keep=keep_as)
        warnings.extend(pack_warnings)
        members[arcname] = text
        pending.extend(kept)

    arcnames = [entry.relative_to(base).as_posix() for entry in resolved]

    # A single-entry archive is run by the file at its root named after
    # the entry script. When the closure's common directory is the
    # entry's own, the entry is already that file and stands as its own
    # entry point; otherwise it sits further down and a forwarder takes
    # its name. Several entries share a root and cannot each hold the
    # name of one, so a combined archive is run at the entries' own
    # paths.
    if len(resolved) == 1 and "/" in arcnames[0]:
        entry = resolved[0]
        if entry.name in members:
            raise ShellpackError(
                f"the closure of {_rel(entry, root)!r} already contains "
                f"{entry.name!r} at the archive root, so the forwarder "
                "cannot take that name"
            )
        members[entry.name] = _entry_script(arcnames[0], _rel(entry, root))
        arcnames = [entry.name]

    return members, arcnames, warnings


def build_archive(
    entries: Sequence[Path],
    root: Path,
    *,
    keep: Iterable[str] = (),
    wrappers: Iterable[str] = (),
) -> tuple[bytes, list[str], list[str]]:
    """Pack the 'requires' closures of ``entries`` into a gzipped tar.

    ``keep`` and ``wrappers`` are as for ``archive_members``. Returns the
    archive bytes, its member names in archive order, and any warnings.
    Modes come from content: a member with a shebang is 0755, a fragment
    without one is 0644. The archive is reproducible: fixed owner, modes
    from content, and a timestamp from ``SOURCE_DATE_EPOCH`` (default 0),
    so its checksum changes only when the sources do.
    """
    members, _arcnames, warnings = archive_members(
        entries, root, keep=keep, wrappers=wrappers
    )

    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=mtime) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for arcname in sorted(members):
            data = members[arcname].encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o755 if data.startswith(b"#!") else 0o644
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))

    return buffer.getvalue(), sorted(members), warnings
