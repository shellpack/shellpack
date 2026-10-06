<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/shellpack/shellpack/main/assets/logo/shellpack-logo-on-dark.png">
    <img alt="shellpack" width="360" src="https://raw.githubusercontent.com/shellpack/shellpack/main/assets/logo/shellpack-logo.png">
  </picture>
</p>

# shellpack

Pack a shell script and the fragments it sources into one standalone file, or a
script and the sibling scripts it runs into one archive, so it runs on a host
that does not have the checkout.

You write scripts the way a repository of them wants to be written: shared
functions in fragments, `source`d where they are needed. shellpack inlines every
sourced fragment in place, in the order the shell would have read them, and
writes a single script that behaves the same for deployment.

## Install

```sh
# Install with uv ...
uv tool install shellpack
# ... or pipx ...
pipx install shellpack
# ... or run it without installing
uvx shellpack --help
```

shellpack needs [`shfmt`](https://github.com/mvdan/sh) 3.7 or newer on the
`PATH`. It reads the syntax tree `shfmt --to-json` prints, which is what tells a
`source` statement from the same words inside a heredoc or a string.

## Use

The arguments read as `cp`'s do: sources, then a destination.

```sh
# Write to the given file
shellpack install/setup.sh /tmp/setup.sh
# Write under its own name into the given folder
shellpack install/setup.sh /tmp/
# Write several into the given folder
shellpack a.sh b.sh c.sh /tmp/packed/
# Write to stdout
shellpack install/setup.sh -
```

A `source` or `.` line is resolved, in order of preference, by:

1. a `# shellcheck source=<path>` directive on the line before it, the path
   relative to the project root (the git toplevel, or `--root`);
2. the `"$(dirname -- "${BASH_SOURCE[0]}")/<rel>"` idiom, relative to the
   sourcing file;
3. a plain relative path with no shell expansion, relative to the sourcing file.

A `source` that resolves to nothing, because the path is built from a variable
or the file does not exist yet, is left as it is. An optional include of a local
override file thus keeps working. Each fragment is inlined once. File-level
`# shellcheck disable=` directives are hoisted to the top of the packed script,
which stays `shellcheck`-clean.

## Directives

A script says what shellpack may do with it in its leading comment block, before
the first command.

```sh
#!/usr/bin/env bash
# shellpack: requires ./configure.sh
# shellpack: requires-dynamic ./steps/10-prepare.sh
```

- `# shellpack: non-packable - <reason>` marks a script that cannot work outside
  a checkout, because it resolves a repository path at run time or runs a
  program in another language. shellpack refuses to pack it, and refuses any
  archive whose closure reaches it.
- `# shellpack: requires <path>` declares a sibling shell script the script
  runs, written relative to itself exactly as the call is written in the code.
  Such a script cannot travel as one file; it must be packed with `--archive`.
- `# shellpack: requires-dynamic <path>` declares a sibling the script runs
  without naming it. For instance an installer that finds its steps with
  `find ./steps`. It is packed just like a `requires` target, but the lint does
  not expect to see an explicit call to it.

## Archives

```sh
shellpack --archive install/setup.sh /tmp/setup.tar.gz
shellpack --combine install/a.sh install/b.sh /tmp/installers.tar.gz
```

`--archive` packs the script together with the transitive closure of its
`requires` directives. Every script is packed on its own and stored at its path
below the closure's common directory, the smallest tree in which the relative
calls resolve as they do in the checkout. The archive is run by the file at its
root named after the entry: the entry itself when it already sits there,
otherwise a forwarder of that name, so the archive runs directly from wherever
it was unpacked.

`--combine` packs several entries into one archive sharing one tree. A script
required by two entries is stored once, and each entry is run at the path it has
in the checkout. There is no forwarder.

Archived scripts are linted against that same tree.

### --wrapper

Projects that run siblings through a wrapper function of their own can give it
with `--wrapper`:

```sh
shellpack --archive --wrapper run_command install/setup.sh /tmp/
```

### --keep

A packed file inlines everything by default. Project that wnat to keep certain
fragments in the archive can exclude them with `--keep`:

```sh
shellpack --archive --keep _env.sh install/setup.sh /tmp/
```

The `# shellcheck source=` directive on a kept line is rewritten to the
archive-relative path, so `shellcheck -x` from the archive root follows it.

## As a library

```python
import shellpack

text, warnings, kept = shellpack.pack(entry, root)
members, entry_points, warnings = shellpack.archive_members(
    [entry], root, keep=["_env.sh"], wrappers=["run_command"]
)
data, names, warnings = shellpack.build_archive([entry], root, keep=["_env.sh"])
```

Everything shellpack refuses raises `shellpack.ShellpackError` with a message
that names the script and the change at the source that fixes it.

## License

MIT.
