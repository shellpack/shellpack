"""Fixtures: a project tree in the shape shellpack is written for, and a runner
for the command.

Every script in the tree cd's to its own directory, sources a shared fragment
through a 'shellcheck source=' directive, and calls siblings by a path relative
to itself. A chain of ``_env.sh`` config fragments runs from the project root
down to ``a/b/c``; packed as an archive rooted at ``a/b``, ``a/b/_env.sh`` is
packed with its parents inlined and ``a/b/c/_env.sh`` travels as a file.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

FRAGMENT = """\
# shellcheck shell=bash
say() { echo "say: $*"; }
"""


def env_fragment(level: str, parent: str | None) -> str:
    """One chain level; ``parent`` is the root-relative directive path."""
    text = "# shellcheck shell=bash\n# shellcheck disable=SC2034\n"
    if parent is not None:
        text += (
            f"# shellcheck source={parent}\n"
            'source "$(dirname -- "${BASH_SOURCE[0]}")/../_env.sh"\n'
        )
    text += f'LEVEL_{level}="{level}"\nVALUE="{level}"\n'
    text += (
        '_ENV_LOCAL="$(dirname -- "${BASH_SOURCE[0]}")/_env.local.sh"\n'
        'if [[ -f "${_ENV_LOCAL}" ]]; then\n'
        "  # shellcheck source=/dev/null\n"
        '  source "${_ENV_LOCAL}"\n'
        "fi\n"
    )
    return text


# a/b/c/script.sh requires a/b/other.sh - the archive root must be a/b.
SCRIPT = """\
#!/usr/bin/env bash
# shellpack: requires ../other.sh

cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
set -euo pipefail
# shellcheck source=a/b/c/_env.sh
source _env.sh
# shellcheck source=lib/_util.sh
source ../../../lib/_util.sh
echo "env: ${VALUE} ${LEVEL_ROOT} ${LEVEL_A} ${LEVEL_B} ${LEVEL_C}"

usage() {
  cat <<EOF
Usage: run ../not-a-call.sh from the checkout
EOF
}

echo "hint: try ./also-not-a-call.sh first"
say "script: $*"
out="$(../other.sh "$@")"
echo "${out}"
"""

OTHER = """\
#!/usr/bin/env bash

cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
set -euo pipefail
# shellcheck source=a/b/_env.sh
source _env.sh
echo "other: $* ${VALUE}"
"""

# The options the tree is written for: its _env.sh files are config to
# keep as files, and run_command is its own wrapper function.
TREE_OPTIONS = ("--keep", "_env.sh", "--wrapper", "run_command")


def pytest_sessionstart(session: pytest.Session) -> None:
    if shutil.which("shfmt") is None:
        pytest.exit(
            "shfmt is not on PATH; shellpack reads the syntax tree it prints, "
            "so every test needs it",
            returncode=3,
        )


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    path.chmod(0o755)
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """The base project tree, under a fresh temporary root."""
    write(tmp_path, "lib/_util.sh", FRAGMENT)
    write(tmp_path, "_env.sh", env_fragment("ROOT", parent=None))
    write(tmp_path, "a/_env.sh", env_fragment("A", parent="_env.sh"))
    write(tmp_path, "a/b/_env.sh", env_fragment("B", parent="a/_env.sh"))
    write(tmp_path, "a/b/c/_env.sh", env_fragment("C", parent="a/b/_env.sh"))
    write(tmp_path, "a/b/c/script.sh", SCRIPT)
    write(tmp_path, "a/b/other.sh", OTHER)
    (tmp_path / "out").mkdir()
    return tmp_path


@pytest.fixture
def transitive_tree(tree: Path) -> Path:
    """The base tree with other.sh requiring a third script, also.sh."""
    write(
        tree,
        "a/b/other.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ./also.sh
        cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
        ./also.sh "$@"
        """,
    )
    write(tree, "a/b/also.sh", OTHER)
    return tree


Runner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def shellpack(tree: Path) -> Runner:
    """Run the command against the tree, with the tree's options."""

    def run(*args: str, options: tuple[str, ...] = TREE_OPTIONS):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "shellpack",
                "--root",
                str(tree),
                *options,
                *args,
            ],
            capture_output=True,
            text=True,
            cwd=tree,
        )

    return run


def collapse(text: str) -> str:
    """rich-click wraps error text; compare on the whitespace-collapsed form."""
    return " ".join(text.split())
