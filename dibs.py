#!/usr/bin/env python3
"""dibs — call dibs on shared stuff.

A tiny advisory lock ledger for physical resources shared by humans and AI
agents: test phones, GPUs, staging environments, the label printer. One JSON
file per resource. No daemon, no dependencies — Python 3.8+ stdlib only.
macOS / Linux / Windows.

    dibs claim gpu-0 --note "training run"
    dibs status
    dibs release gpu-0

A lock is held until it is released. If a holder vanished, break the lock
with `dibs release <r> --force` (loud on purpose).

Ledger dir:  $DIBS_DIR   (default ~/.dibs)
Owner id:    $DIBS_OWNER (default user@host:pid<ppid> — set a stable one!)
Resources:   $DIBS_DIR/resources.json — you write it by hand, once:
             {"gpu-0": "the shared GPU", "phone-a": "test phone"}
             Only defined names can be claimed. dibs never edits this file.

Claiming several resources in one call takes them all-or-nothing and forms
a group (auto-named, or `--as NAME`); release the group to drop them all:

    dibs claim phone-a gpu-0      ->  group g-3fa2c1: gpu-0, phone-a
    dibs release g-3fa2c1

Exit codes:  0 ok · 2 busy · 3 not held by you · 4 wait timeout · 1 error
"""
import argparse
import getpass
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.3.1"

LEDGER = Path(os.environ.get("DIBS_DIR", Path.home() / ".dibs"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_str(ts):
    since = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - since).total_seconds())
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 129600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d"


def default_owner():
    o = os.environ.get("DIBS_OWNER")
    if o:
        return o
    return f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}:pid{os.getppid()}"


def lock_path(resource):
    if not NAME_RE.match(resource):
        raise SystemExit(f"dibs: bad resource name {resource!r} "
                         "(letters/digits/._- only)")
    return LEDGER / f"{resource}.lock.json"


def read_lock(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def registry():
    """Hand-written {"name": "description"} map, or None if absent."""
    try:
        reg = json.loads((LEDGER / "resources.json").read_text())
        if isinstance(reg, dict):
            return reg
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise SystemExit(f"dibs: {LEDGER / 'resources.json'} is not valid JSON: {e}")
    raise SystemExit(f"dibs: {LEDGER / 'resources.json'} must be a JSON object "
                     '{"name": "description"}')


NO_REGISTRY = (f"dibs: no resources defined yet. Create {LEDGER / 'resources.json'} "
               'by hand, e.g. {"gpu-0": "the shared GPU", "phone-a": "test phone"}')


def check_names(names):
    """Validate claim targets against the registry; return sorted, deduped."""
    reg = registry()
    if not reg:
        raise SystemExit(NO_REGISTRY)
    for name in names:
        if name not in reg:
            raise SystemExit(f"dibs: {name!r} is not defined in resources.json "
                             f"(known: {', '.join(sorted(reg))})")
    return sorted(set(names))


def holder_line(resource, rec):
    note = f' — "{rec["note"]}"' if rec.get("note") else ""
    grp = f" [group {rec['group']}]" if rec.get("group") else ""
    return (f"{resource}: held by {rec['owner']} "
            f"since {rec['since']} ({age_str(rec['since'])} ago){note}{grp}")


def ledger_rows():
    """All resources: registry entries plus any live locks."""
    LEDGER.mkdir(parents=True, exist_ok=True)
    reg = registry() or {}
    rows = {name: {"resource": name, "description": desc}
            for name, desc in sorted(reg.items())
            if isinstance(desc, str)}  # list values are groups, not resources
    for p in sorted(LEDGER.glob("*.lock.json")):
        rec = read_lock(p)
        if rec is None:
            continue
        name = rec.get("resource", p.name[:-len(".lock.json")])
        desc = reg.get(name, "")
        rec.setdefault("description", desc if isinstance(desc, str) else "")
        rows[name] = {**rows.get(name, {}), **rec}
    return list(rows.values())


def try_claim_one(resource, owner, note, group=None):
    """Returns 'claimed' | 'yours' | the blocking record."""
    path = lock_path(resource)
    rec = {"resource": resource, "owner": owner,
           "host": socket.gethostname().split(".")[0],
           "since": now_iso(), "note": note or None, "group": group}
    while True:
        LEDGER.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                json.dump(rec, f, indent=2)
            return "claimed"
        except FileExistsError:
            cur = read_lock(path)
            if cur is None:
                continue  # holder released between our create and read; retry
            if cur.get("owner") == owner:
                if note and note != cur.get("note"):
                    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
                    tmp.write_text(json.dumps(dict(cur, note=note), indent=2))
                    os.replace(tmp, path)
                return "yours"
            return cur


def cmd_claim(a):
    """All-or-nothing: on any BUSY, locks newly taken by this call are rolled
    back (sorted claim order keeps overlapping sets deadlock-free). Claiming
    several resources at once forms a group — a tag stamped on each lock —
    so the whole set can later be discarded with `dibs release <group>`."""
    resources = check_names(a.resources)
    owner = a.owner or default_owner()
    group = getattr(a, "as_group", None)
    if group is None and len(resources) > 1:
        group = "g-" + os.urandom(3).hex()
    if group and not NAME_RE.match(group):
        raise SystemExit(f"dibs: bad group name {group!r}")
    deadline = time.time() + a.timeout if a.timeout else None
    while True:
        got, blocker = [], None
        for r in resources:
            res = try_claim_one(r, owner, a.note, group)
            if res == "claimed":
                got.append(r)
            elif res != "yours":
                blocker = (r, res)
                break
        if blocker is None:
            for r in resources:
                print(f"claimed {r} as {owner}")
            if group:
                print(f"group {group}: {', '.join(resources)} "
                      f"(discard with: dibs release {group})")
            return 0
        for r in got:  # roll back this call's partial set
            try:
                lock_path(r).unlink()
            except FileNotFoundError:
                pass
        if not a.wait:
            print("BUSY " + holder_line(*blocker), file=sys.stderr)
            return 2
        if deadline and time.time() > deadline:
            print("timeout waiting; " + holder_line(*blocker), file=sys.stderr)
            return 4
        time.sleep(a.poll)


def resolve_targets(names):
    """Each name is a resource with a live lock, or a group tag: expands to
    every locked resource carrying that tag. Unknown names pass through."""
    LEDGER.mkdir(parents=True, exist_ok=True)
    locks = [rec for p in sorted(LEDGER.glob("*.lock.json"))
             if (rec := read_lock(p))]
    out = []
    for name in names:
        members = [r["resource"] for r in locks if r.get("group") == name]
        if members and not any(r["resource"] == name for r in locks):
            out.extend(members)
        else:
            out.append(name)
    return sorted(set(out))


def cmd_release(a):
    owner = a.owner or default_owner()
    rc = 0
    for resource in resolve_targets(a.resources):
        path = lock_path(resource)
        rec = read_lock(path)
        if rec is None:
            print(f"{resource}: not held")
            continue
        if rec.get("owner") != owner and not a.force:
            print(f"NOT YOURS {holder_line(resource, rec)}\n"
                  f"(you are {owner}; use --force to break it)", file=sys.stderr)
            rc = 3
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        verb = "broke" if rec.get("owner") != owner else "released"
        print(f"{verb} {resource} (was {rec.get('owner')})")
    return rc


def cmd_status(a):
    rows = ledger_rows()
    if a.resource:
        rows = [r for r in rows if r["resource"] == a.resource]
        if not rows:
            print(f"{a.resource}: free")
            return 0
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if a.xbar:
        held = [r for r in rows if r.get("owner")]
        print(f"dibs ✋{len(held)}" if held else "dibs ✓")
        print("---")
        if not rows:
            print("no resources.json yet | color=gray")
        for r in rows:
            if r.get("owner"):
                note = f" · {r['note']}" if r.get("note") else ""
                grp = f" · {r['group']}" if r.get("group") else ""
                print(f"{r['resource']} — {r['owner']}{note}{grp} | color=#e05d44")
            else:
                print(f"{r['resource']} — free | color=#44a05d")
        return 0
    if not rows:
        print(NO_REGISTRY, file=sys.stderr)
        return 1
    for r in rows:
        if r.get("owner"):
            print(holder_line(r["resource"], r))
        else:
            desc = f"  ({r['description']})" if r.get("description") else ""
            print(f"{r['resource']}: free{desc}")
    return 0


def cmd_wait(a):
    deadline = time.time() + a.timeout if a.timeout else None
    while True:
        resources = resolve_targets(a.resources)
        held = next(((r, rec) for r in resources
                     if (rec := read_lock(lock_path(r)))), None)
        if held is None:
            print(", ".join(a.resources) + ": free")
            return 0
        if deadline and time.time() > deadline:
            print("timeout: " + holder_line(*held), file=sys.stderr)
            return 4
        time.sleep(a.poll)


def cmd_run(a):
    import subprocess
    if "--" in a.argv:
        i = a.argv.index("--")
        if a.argv[:i]:
            print("dibs run: put dibs flags BEFORE <resource> "
                  f"(these were ignored: {' '.join(a.argv[:i])})", file=sys.stderr)
            return 1
        argv = a.argv[i + 1:]
    else:
        argv = a.argv
    if argv and argv[0].startswith("-"):
        print("dibs run: put dibs flags BEFORE <resource>; the command goes "
              "after -- (dibs run --note x <resource> -- cmd ...)", file=sys.stderr)
        return 1
    if not argv:
        print("dibs run: no command given (dibs run <resource> -- cmd ...)",
              file=sys.stderr)
        return 1
    a.resources = [a.resource]
    rc = cmd_claim(a)
    if rc != 0:
        return rc
    try:
        return subprocess.call(argv)
    finally:
        try:
            lock_path(a.resource).unlink()
        except FileNotFoundError:
            pass
        print(f"released {a.resource}", file=sys.stderr)


def cmd_watch(a):
    try:
        while True:
            rows = ledger_rows()
            sys.stdout.write("\x1b[2J\x1b[H")
            print(f"dibs ledger · {LEDGER}  (ctrl-c to quit)\n")
            if not rows:
                print("  " + NO_REGISTRY)
            for r in rows:
                if r.get("owner"):
                    print("  🔴 " + holder_line(r["resource"], r))
                else:
                    print(f"  🟢 {r['resource']}: free")
            sys.stdout.flush()
            time.sleep(a.poll)
    except KeyboardInterrupt:
        return 0


def main():
    p = argparse.ArgumentParser(
        prog="dibs", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"dibs {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_claim_args(sp, multi):
        if multi:
            sp.add_argument("resources", nargs="+", metavar="resource",
                            help="resource or group names")
        else:
            sp.add_argument("resource", help="resource or group name")
        sp.add_argument("--note", help="why you have it")
        sp.add_argument("--as", dest="as_group", metavar="GROUP",
                        help="name the group (default: auto g-xxxxxx when "
                             "claiming several resources)")
        sp.add_argument("--owner", help="override owner id (or set $DIBS_OWNER)")
        sp.add_argument("--wait", action="store_true", help="block until claimable")
        sp.add_argument("--timeout", type=int,
                        help="give up after N seconds (with --wait)")
        sp.add_argument("--poll", type=int, default=5, help="poll interval seconds")

    sp = sub.add_parser(
        "claim", help="claim resources/groups, all-or-nothing (exit 2 if busy)")
    add_claim_args(sp, multi=True)
    sp.set_defaults(fn=cmd_claim)

    sp = sub.add_parser("release", help="release resources/groups you hold")
    sp.add_argument("resources", nargs="+", metavar="resource")
    sp.add_argument("--owner")
    sp.add_argument("--force", action="store_true",
                    help="break someone else's lock")
    sp.set_defaults(fn=cmd_release)

    sp = sub.add_parser("status", help="show the ledger (all, or one resource)")
    sp.add_argument("resource", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--xbar", action="store_true",
                    help="xbar/SwiftBar plugin output")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("wait", help="block until resources are free (no claim)")
    sp.add_argument("resources", nargs="+", metavar="resource")
    sp.add_argument("--timeout", type=int)
    sp.add_argument("--poll", type=int, default=5)
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser(
        "run", help="claim, run a command, auto-release: dibs run gpu -- make train")
    add_claim_args(sp, multi=False)
    sp.add_argument("argv", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("watch", help="live terminal view of the ledger")
    sp.add_argument("--poll", type=int, default=2)
    sp.set_defaults(fn=cmd_watch)

    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
