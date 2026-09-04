<div align="center">

# ✋ call-dibs

**Call dibs on shared stuff.**

A tiny lock ledger so humans and AI agents stop fighting over the same phone, GPU, or test bench.

[![CI](https://github.com/NeoMarcoPolo/call-dibs/actions/workflows/ci.yml/badge.svg)](https://github.com/NeoMarcoPolo/call-dibs/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/call-dibs)](https://pypi.org/project/call-dibs/)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

```console
$ dibs claim phone-a --note "regression run"
claimed phone-a as alice

$ dibs claim phone-a                      # …from another session
BUSY phone-a: held by alice since 2026-09-02T21:14:03Z (3m ago) — "regression run"

$ dibs release phone-a
released phone-a (was alice)
```

## Why

You have one test phone, one GPU, one hardware bench — and several
terminals (or several coding agents) that can all reach it. `dibs` is the
smallest thing that makes them take turns.

- **One file, no dependencies.** A single Python script, stdlib only. No
  daemon, no server, no database.
- **Cross-platform.** macOS, Linux, Windows.
- **Legible.** The ledger is one human-readable JSON file per resource, and
  every "busy" says *who*, *since when*, and *why*.
- **Agent-friendly.** Stable exit codes, `--json`, a blocking `--wait`, and a
  `run` wrapper that always releases — even when the command fails.

## Install

```sh
pipx install call-dibs        # or: pip install call-dibs · uv tool install call-dibs
```

Works the same on macOS, Linux, and Windows. Prefer no package manager? It's
one file — [`dibs.py`](dibs.py) — drop it on your `PATH`.

## Set up

Tell `dibs` what you share. Write `~/.dibs/resources.json` once, by hand:

```json
{
  "phone-a": "Android test phone on the bench",
  "gpu-0": "the shared training GPU",
  "printer": "label printer, room 2"
}
```

Only these names can be claimed, so a typo can't silently create a new lock.
`dibs` never edits this file.

## Use

| command | what it does |
|---|---|
| `dibs claim <r...> [--note ...] [--wait [--timeout N]]` | claim one or more resources. Exit `2` + holder info if busy |
| `dibs release <r...> [--force]` | release |
| `dibs status [r] [--json]` | who has what |
| `dibs wait <r...> [--timeout N]` | block until free, without claiming |
| `dibs run <r> -- <cmd...>` | claim → run → always release |
| `dibs watch` | live terminal view |

Exit codes: `0` ok · `2` busy · `3` not yours · `4` wait timeout · `1` error.

### Who's asking

Every claim records an owner, and only that owner can release it. Name
yourself once per session with `DIBS_OWNER`:

```sh
export DIBS_OWNER=alice                    # people
DIBS_OWNER=agent:task-123 dibs claim …     # agents: inline, so every call agrees
```

If it's unset, `dibs` falls back to `user@host:pid` — fine for one-off use,
wrong for agents whose shell changes every command. `--force` releases
someone else's lock when they're gone.

### Groups

Claim several resources in one call and they form a group. The claim is
all-or-nothing — if any resource is busy, nothing is taken — and the group
gets a tag so you can drop the whole set at once:

```console
$ dibs claim phone-a gpu-0 --note "regression run"
claimed gpu-0 as alice
claimed phone-a as alice
group g-3fa2c1: gpu-0, phone-a (discard with: dibs release g-3fa2c1)

$ dibs release g-3fa2c1
```

Name the group yourself with `--as NAME`.

## For AI agents

`skills/dibs/` is a drop-in skill for Claude Code (and reads fine as an
`AGENTS.md` snippet for anything else). It teaches the protocol: claim before
touching hardware, one call per set, release when done, never force-break
someone else's lock.

```sh
cp -r skills/dibs ~/.claude/skills/
```

## Menu bar (macOS)

See the ledger at a glance: `dibs ✋2` in the top bar when two things are
claimed, `dibs ✓` when everything is free, and who-has-what in the dropdown.
Install [SwiftBar](https://swiftbar.app), then add the plugin:

```sh
open "swiftbar://addplugin?src=https://raw.githubusercontent.com/NeoMarcoPolo/call-dibs/main/contrib/dibs.5s.sh"
```

Start the UI when you want it with `open -a SwiftBar`; quit it from the
dropdown's **Quit** item. (SwiftBar only runs when launched — set it as a
login item if you'd rather have it always there.)

## How it works

One JSON file per resource under `~/.dibs/` (override with `DIBS_DIR`). A
claim is an atomic `O_CREAT|O_EXCL` create, so racing claimers get exactly
one winner. Locks are advisory and are held until released — there is no
expiry. The ledger is per-machine by default; point `DIBS_DIR` at a shared
directory to span machines.

## Contributing

`python3 -m unittest discover tests`. Keep it small.

Releases: bump `version` in `pyproject.toml` and `dibs.py`, then push a
`v*` tag — CI publishes to PyPI via trusted publishing (maintainer approval
required).

## License

[MIT](LICENSE)
