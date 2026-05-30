"""The command against the fixture tree: the single-file pack, the 'requires'
closure and archive, the smallest-tree rule, the root entry point (a forwarder,
or the entry itself), several entries sharing one archive, the kept config
fragments, and the invocation lint's accepts and rejects.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

from conftest import OTHER, Runner, collapse, write

# A usage text a script prints is data: an include-looking line in a heredoc
# must come out of the pack as it went in.
HEREDOC = """\
#!/usr/bin/env bash
# shellpack: requires ../other.sh

cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
set -euo pipefail
# shellcheck source=lib/_util.sh
source ../../../lib/_util.sh

cat <<EOF
source ../other.sh
# shellcheck source=a/b/other.sh
EOF

../other.sh
"""


def members(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return sorted(tar.getnames())


def member_text(archive: Path, name: str) -> str:
    with tarfile.open(archive) as tar:
        return tar.extractfile(name).read().decode()


def unpack(archive: Path, into: Path) -> Path:
    into.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(into, filter="fully_trusted")
    return into


def run_script(path: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(path), *args], capture_output=True, text=True, cwd=cwd)


# ----------------------------------------------------------------------
# Modes


def test_single_file_refuses_a_requires_script(tree: Path, shellpack: Runner):
    res = shellpack("a/b/c/script.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "--archive" in collapse(res.stderr)


def test_archive_refuses_a_script_without_requires(tree: Path, shellpack: Runner):
    res = shellpack("--archive", "a/b/other.sh", str(tree / "out"))
    assert res.returncode == 2


def test_single_file_pack_of_a_plain_script(tree: Path, shellpack: Runner):
    res = shellpack("a/b/other.sh", str(tree / "out"))
    assert res.returncode == 0, res.stderr
    packed = tree / "out" / "other.sh"
    assert os.access(packed, os.X_OK)
    text = packed.read_text()
    # Single file: the whole _env.sh chain is inlined, and runs.
    assert 'LEVEL_ROOT="ROOT"' in text and "source _env.sh" not in text
    run = run_script(packed, "q", cwd=tree)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "other: q B"


def test_stdout_destination(tree: Path, shellpack: Runner):
    res = shellpack("a/b/other.sh", "-")
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("#!/usr/bin/env bash\n# --- shellpack: packed from")


def test_missing_destination(tree: Path, shellpack: Runner):
    res = shellpack("a/b/other.sh")
    assert res.returncode == 2
    assert "missing destination" in collapse(res.stderr)


# ----------------------------------------------------------------------
# The archive


def test_archive_is_the_smallest_tree_with_config_as_files(
    tree: Path, shellpack: Runner
):
    res = shellpack("--archive", "a/b/c/script.sh", str(tree / "out"))
    assert res.returncode == 0, collapse(res.stderr)
    archive = tree / "out" / "script.tar.gz"
    assert members(archive) == [
        "_env.sh",
        "c/_env.sh",
        "c/script.sh",
        "other.sh",
        "script.sh",
    ]

    root_env = member_text(archive, "_env.sh")
    assert 'LEVEL_ROOT="ROOT"' in root_env
    assert 'source "$(dirname' not in root_env, "out-of-tree parents are inlined"

    c_env = member_text(archive, "c/_env.sh")
    assert '# shellcheck source=_env.sh\nsource "$(dirname' in c_env, (
        "a deeper _env.sh still sources its parent, directive rewritten"
    )

    packed_script = member_text(archive, "c/script.sh")
    assert "# shellcheck source=c/_env.sh\nsource _env.sh" in packed_script
    assert "say()" in packed_script, "code fragments are inlined"

    with tarfile.open(archive) as tar:
        modes = {m.name: m.mode for m in tar.getmembers()}
    for name, mode in modes.items():
        assert mode == (0o644 if name.endswith("_env.sh") else 0o755), modes


def test_archive_is_reproducible(tree: Path, shellpack: Runner):
    shellpack("--archive", "a/b/c/script.sh", str(tree / "out" / "one.tar.gz"))
    shellpack("--archive", "a/b/c/script.sh", str(tree / "out" / "two.tar.gz"))
    assert (tree / "out" / "one.tar.gz").read_bytes() == (
        tree / "out" / "two.tar.gz"
    ).read_bytes()


def test_forwarder_runs_from_elsewhere(tree: Path, shellpack: Runner):
    shellpack("--archive", "a/b/c/script.sh", str(tree / "out"))
    unpacked = unpack(tree / "out" / "script.tar.gz", tree / "out" / "unpacked")

    # Not from the archive root: the forwarder must not need it.
    run = run_script(unpacked / "script.sh", "one", "two words", cwd=tree)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "env: C ROOT A B C",
        "hint: try ./also-not-a-call.sh first",
        "say: script: one two words",
        "other: one two words B",
    ]


def test_root_override_reaches_every_script(tree: Path, shellpack: Runner):
    shellpack("--archive", "a/b/c/script.sh", str(tree / "out"))
    unpacked = unpack(tree / "out" / "script.tar.gz", tree / "out" / "unpacked")

    # A deeper level that reassigns a name still wins, as in the checkout,
    # so override a name only the root sets.
    (unpacked / "_env.local.sh").write_text('LEVEL_ROOT="override"\nVALUE="override"\n')
    run = run_script(unpacked / "script.sh", "x", cwd=tree)
    assert run.stdout.splitlines()[0] == "env: C override A B C"
    assert run.stdout.splitlines()[-1] == "other: x override"


def test_without_keep_the_config_is_inlined(tree: Path, shellpack: Runner):
    res = shellpack("--archive", "a/b/c/script.sh", str(tree / "out"), options=())
    assert res.returncode == 0, collapse(res.stderr)
    archive = tree / "out" / "script.tar.gz"
    assert members(archive) == ["c/script.sh", "other.sh", "script.sh"]
    assert 'LEVEL_ROOT="ROOT"' in member_text(archive, "c/script.sh")


def test_keep_is_a_glob(tree: Path, shellpack: Runner):
    res = shellpack(
        "--archive", "a/b/c/script.sh", str(tree / "out"), options=("--keep", "_env*")
    )
    assert res.returncode == 0, collapse(res.stderr)
    assert "c/_env.sh" in members(tree / "out" / "script.tar.gz")


def test_heredoc_body_is_copied_verbatim(tree: Path, shellpack: Runner):
    write(tree, "a/b/c/heredoc.sh", HEREDOC)
    res = shellpack("--archive", "a/b/c/heredoc.sh", str(tree / "out" / "h.tar.gz"))
    assert res.returncode == 0, res.stderr
    packed = member_text(tree / "out" / "h.tar.gz", "c/heredoc.sh")
    assert "\nsource ../other.sh\n# shellcheck source=a/b/other.sh\nEOF\n" in packed
    assert "say()" in packed, "a real 'source' outside it is still inlined"


def test_requires_is_transitive(transitive_tree: Path, shellpack: Runner):
    tree = transitive_tree
    res = shellpack("--archive", "a/b/c/script.sh", str(tree / "out" / "t.tar.gz"))
    assert res.returncode == 0, collapse(res.stderr)
    assert members(tree / "out" / "t.tar.gz") == [
        "_env.sh",
        "also.sh",
        "c/_env.sh",
        "c/script.sh",
        "other.sh",
        "script.sh",
    ]


def test_entry_at_the_closure_root_needs_no_forwarder(
    transitive_tree: Path, shellpack: Runner
):
    tree = transitive_tree
    write(
        tree,
        "a/b/top.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ./other.sh

        cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
        set -euo pipefail
        ./other.sh "$@"
        """,
    )
    res = shellpack("--archive", "a/b/top.sh", str(tree / "out" / "top.tar.gz"))
    assert res.returncode == 0, collapse(res.stderr)
    assert members(tree / "out" / "top.tar.gz") == [
        "_env.sh",
        "also.sh",
        "other.sh",
        "top.sh",
    ]
    unpacked = unpack(tree / "out" / "top.tar.gz", tree / "out" / "top")
    run = run_script(unpacked / "top.sh", "z", cwd=tree)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "other: z B"


# ----------------------------------------------------------------------
# Several entries in one archive


def test_combined_archive_shares_one_tree(transitive_tree: Path, shellpack: Runner):
    tree = transitive_tree
    write(
        tree,
        "a/b/top.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ./other.sh
        cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
        ./other.sh "$@"
        """,
    )
    res = shellpack(
        "--combine", "a/b/c/script.sh", "a/b/top.sh", str(tree / "out" / "both.tar.gz")
    )
    assert res.returncode == 0, collapse(res.stderr)
    assert members(tree / "out" / "both.tar.gz") == [
        "_env.sh",
        "also.sh",
        "c/_env.sh",
        "c/script.sh",
        "other.sh",
        "top.sh",
    ], "what the entries share is stored once, and there is no forwarder"

    unpacked = unpack(tree / "out" / "both.tar.gz", tree / "out" / "both")
    # Not from the archive root: each entry cd's to its own directory.
    runs = [
        run_script(unpacked / entry, "z", cwd=tree)
        for entry in ("c/script.sh", "top.sh")
    ]
    assert all(run.returncode == 0 for run in runs)
    assert runs[0].stdout.splitlines()[-1] == "other: z B"
    assert runs[1].stdout.strip() == "other: z B"


def test_combined_archive_takes_an_entry_without_requires(
    transitive_tree: Path, shellpack: Runner
):
    # also.sh is also reached through script.sh -> other.sh: an entry that
    # another entry already requires is stored once and runs by its own
    # path like any other entry.
    tree = transitive_tree
    res = shellpack(
        "--combine", "a/b/c/script.sh", "a/b/also.sh", str(tree / "out" / "with.tar.gz")
    )
    assert res.returncode == 0, collapse(res.stderr)
    assert members(tree / "out" / "with.tar.gz") == [
        "_env.sh",
        "also.sh",
        "c/_env.sh",
        "c/script.sh",
        "other.sh",
    ]
    unpacked = unpack(tree / "out" / "with.tar.gz", tree / "out" / "with")
    run = run_script(unpacked / "also.sh", "y", cwd=tree)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "other: y B"

    # The order the entries are given in does not change the archive.
    shellpack(
        "--combine", "a/b/also.sh", "a/b/c/script.sh", str(tree / "out" / "swap.tar.gz")
    )
    assert (tree / "out" / "swap.tar.gz").read_bytes() == (
        tree / "out" / "with.tar.gz"
    ).read_bytes()


def test_combined_archive_refuses_a_directory_dest(
    transitive_tree: Path, shellpack: Runner
):
    tree = transitive_tree
    res = shellpack("--combine", "a/b/c/script.sh", "a/b/other.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "one archive" in collapse(res.stderr)


# ----------------------------------------------------------------------
# The invocation lint


def test_undeclared_sibling_fails_the_pack(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/undeclared.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        ../other.sh
        run_command ./helper.sh --flag
        """,
    )
    write(tree, "a/b/c/helper.sh", OTHER)
    res = shellpack("--archive", "a/b/c/undeclared.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "requires ./helper.sh" in collapse(res.stderr), "names the directive to add"

    # Without the wrapper named, 'run_command' is the command the call runs
    # and the script after it is an argument the lint does not see.
    res = shellpack("--archive", "a/b/c/undeclared.sh", str(tree / "out"), options=())
    assert res.returncode == 0, collapse(res.stderr)


def test_builtin_wrappers_are_looked_past(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/wrapped.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        ../other.sh
        sudo env FOO=1 ./helper.sh --flag
        """,
    )
    write(tree, "a/b/c/helper.sh", OTHER)
    res = shellpack("--archive", "a/b/c/wrapped.sh", str(tree / "out"), options=())
    assert res.returncode == 2
    assert "requires ./helper.sh" in collapse(res.stderr)


def test_undeclared_python_invocation_suggests_non_packable(
    tree: Path, shellpack: Runner
):
    write(
        tree,
        "a/b/c/python.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        ../other.sh
        ./tool.py --now
        """,
    )
    res = shellpack("--archive", "a/b/c/python.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "non-packable" in collapse(res.stderr)


def test_declared_but_unseen_sibling_warns(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/stale.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        # shellpack: requires ./helper.sh
        ../other.sh
        """,
    )
    write(tree, "a/b/c/helper.sh", OTHER)
    res = shellpack("--archive", "a/b/c/stale.sh", str(tree / "out"))
    assert res.returncode == 0
    assert "warning" in res.stderr and "helper.sh" in res.stderr


def test_requires_dynamic_packs_without_a_warning(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/dynamic.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires-dynamic ./helper.sh
        # shellpack: requires ../other.sh
        ../other.sh
        find . -name '*.sh' | while read -r step; do "${step}"; done
        """,
    )
    write(tree, "a/b/c/helper.sh", OTHER)
    res = shellpack("--archive", "a/b/c/dynamic.sh", str(tree / "out" / "dyn.tar.gz"))
    assert res.returncode == 0, collapse(res.stderr)
    assert "warning" not in res.stderr
    assert "c/helper.sh" in members(tree / "out" / "dyn.tar.gz")


def test_script_listed_in_an_array_counts_as_run(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/array.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        ../other.sh
        SCRIPTS=(
          ./helper.sh
        )
        for s in "${SCRIPTS[@]}"; do "${s}"; done
        """,
    )
    write(tree, "a/b/c/helper.sh", OTHER)
    res = shellpack("--archive", "a/b/c/array.sh", str(tree / "out"))
    assert res.returncode == 2


def test_quoted_usage_text_is_not_an_invocation(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/usage.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        ../other.sh
        echo "  export ARN=\\$(./helper.sh --get-arn)"
        echo "  cd elsewhere && ./helper.sh --now"
        echo 'or: ./helper.sh; ./helper.sh'
        """,
    )
    res = shellpack("--archive", "a/b/c/usage.sh", str(tree / "out"))
    assert res.returncode == 0, collapse(res.stderr)


def test_substitution_in_double_quotes_is_an_invocation(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/substitution.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ../other.sh
        ../other.sh
        echo "arn: $(./helper.sh --get-arn)"
        """,
    )
    write(tree, "a/b/c/helper.sh", OTHER)
    res = shellpack("--archive", "a/b/c/substitution.sh", str(tree / "out"))
    assert res.returncode == 2


def test_missing_requires_target_fails(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/missing.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ./nope.sh
        ./nope.sh
        """,
    )
    res = shellpack("--archive", "a/b/c/missing.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "does not exist" in collapse(res.stderr)


def test_non_packable_script_in_the_closure_fails(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/blocked.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: requires ./nonpackable.sh
        ./nonpackable.sh
        """,
    )
    write(
        tree,
        "a/b/c/nonpackable.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: non-packable - reads the checkout
        true
        """,
    )
    res = shellpack("--archive", "a/b/c/blocked.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "non-packable" in collapse(res.stderr)


def test_non_packable_entry_is_refused(tree: Path, shellpack: Runner):
    write(
        tree,
        "a/b/c/nonpackable.sh",
        """\
        #!/usr/bin/env bash
        # shellpack: non-packable - reads the checkout
        true
        """,
    )
    res = shellpack("a/b/c/nonpackable.sh", str(tree / "out"))
    assert res.returncode == 2
    assert "reads the checkout" in collapse(res.stderr)
