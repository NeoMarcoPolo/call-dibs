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

`claim --wait` waits in line: waiters get their turn oldest first, and a
claim without --wait can't jump ahead of one. A waiter still blocked on
something else doesn't hold up devices it isn't using yet. Its place lasts
only while the wait runs.

Exit codes:  0 ok · 2 busy · 3 not held by you · 4 wait timeout · 1 error
"""
import argparse
import getpass
import json
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.4.0"

LEDGER = Path(os.environ.get("DIBS_DIR", Path.home() / ".dibs"))
QUEUE = LEDGER / "queue"  # one ticket per waiting claim
STALE = 15  # seconds without a heartbeat before a waiter counts as gone
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_str(ts):
    try:
        since = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "?"
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


def queue_line(resource, waiting):
    def one(w):
        also = " ".join(f"+{r}" for r in w["resources"] if r != resource)
        return f"{w['owner']} ({age_str(w['since'])}{', ' + also if also else ''})"
    return "  queue: " + ", ".join(one(w) for w in waiting)


def ledger_rows():
    """All resources: registry entries plus any live locks, each with the
    line waiting for it (the "waiting" key only when someone waits)."""
    reg = registry() or {}
    rows = {name: {"resource": name, "description": desc}
            for name, desc in sorted(reg.items())
            if isinstance(desc, str)}  # list values are groups, not resources
    for rec in current_locks():
        name = rec["resource"]
        desc = reg.get(name, "")
        rec.setdefault("description", desc if isinstance(desc, str) else "")
        rows[name] = {**rows.get(name, {}), **rec}
    for t in live_tickets():
        for r in t["resources"]:
            rows.setdefault(r, {"resource": r, "description": ""}).setdefault(
                "waiting", []).append(
                {k: t.get(k) for k in ("owner", "since", "note", "resources", "host")})
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


def current_locks():
    """Every live lock record, sorted by resource."""
    LEDGER.mkdir(parents=True, exist_ok=True)
    locks = []
    for p in sorted(LEDGER.glob("*.lock.json")):
        rec = read_lock(p)
        if isinstance(rec, dict):
            rec.setdefault("resource", p.name[:-len(".lock.json")])
            locks.append(rec)
    return locks


def read_ticket(path):
    """A waiter's ticket, or None if the file isn't one."""
    try:
        t = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (isinstance(t, dict) and {"id", "owner", "resources", "since", "t"} <= t.keys()
            and isinstance(t["id"], str) and isinstance(t["owner"], str)
            and isinstance(t["resources"], list)
            and all(isinstance(r, str) for r in t["resources"])
            and isinstance(t["t"], (int, float))):
        return t
    return None


def live_tickets():
    """Waiting claims, oldest first. A ticket whose waiter stopped
    heartbeating is removed, as dibs wrote it and knows it's abandoned.
    Anything else in the queue dir — unreadable, or not a ticket dibs
    wrote — is skipped and left on disk, however old; the queue dir may
    also not exist as a directory at all, in which case there's simply
    nothing waiting."""
    out = []
    try:
        paths = list(QUEUE.glob("*.json"))
    except OSError:
        paths = []
    for p in paths:
        try:
            age = time.time() - p.stat().st_mtime
        except OSError:
            continue
        t = read_ticket(p)
        if t is None:
            continue  # not a ticket dibs wrote; never ours to delete
        poll = t.get("poll")
        limit = max(STALE, 3 * poll if isinstance(poll, (int, float)) else 0)
        if age <= limit:
            out.append(t)
        else:
            try:
                p.unlink()
            except OSError:
                pass
    return sorted(out, key=lambda t: (t["t"], str(t["id"])))


def next_in_line(resources, owner, me=None):
    """The waiter ahead of this claim who is next for one of `resources`,
    or None. Oldest first, a waiter whose devices are all free (and not
    promised to someone older) is next for them; a waiter still blocked
    elsewhere reserves nothing. Without a ticket (`me`) a claim stands at
    the back of the line. Tickets never block their own owner."""
    taken = {rec["resource"]: rec.get("owner") for rec in current_locks()}
    for t in live_tickets():
        if t["id"] == me:
            break
        if all(taken.get(r, t["owner"]) == t["owner"] for r in t["resources"]):
            if t["owner"] != owner and set(resources) & set(t["resources"]):
                return t
            taken.update((r, t["owner"]) for r in t["resources"])
    return None


def busy_reason(resources, owner, me=None):
    """Why this claim can't go ahead right now, or None if it can try."""
    for r in resources:
        rec = read_lock(lock_path(r))
        if rec and rec.get("owner") != owner:
            return holder_line(r, rec)
    t = next_in_line(resources, owner, me)
    if t is None:
        return None
    r = next(r for r in resources if r in t["resources"])
    note = f' — "{t["note"]}"' if t.get("note") else ""
    return (f"{r}: free, but next in line is {t['owner']} "
            f"(waiting {age_str(t['since'])}){note}")


def take_all(resources, owner, note, group):
    """Take every resource or none. Returns None, or why not."""
    got = []
    for r in resources:
        res = try_claim_one(r, owner, note, group)
        if res == "claimed":
            got.append(r)
        elif res != "yours":
            for g in got:  # roll back this call's partial set
                try:
                    lock_path(g).unlink()
                except FileNotFoundError:
                    pass
            return holder_line(r, res)
    return None


def write_ticket(t):
    """Atomically (re)write a waiter's ticket."""
    QUEUE.mkdir(parents=True, exist_ok=True)
    path = QUEUE / f"{t['id']}.json"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(t, indent=2))
    os.replace(tmp, path)
    return path


def join_line(resources, owner, note, poll):
    t = {"id": os.urandom(4).hex(), "owner": owner, "resources": resources,
         "note": note or None, "host": socket.gethostname().split(".")[0],
         "since": now_iso(), "t": time.time(), "poll": poll}
    write_ticket(t)
    return t


def heartbeat(t):
    """Keep our place. If a reader dropped the ticket as stale (say the
    laptop slept), put it back with its original place."""
    try:
        os.utime(QUEUE / f"{t['id']}.json")
    except FileNotFoundError:
        try:
            write_ticket(t)
        except OSError:
            pass  # queue dir unwritable right now; the next beat retries
    except OSError:
        pass  # e.g. Windows while a reader has it open; the next beat will do


def leave_line(t):
    try:
        (QUEUE / f"{t['id']}.json").unlink()
    except OSError:
        pass


def ahead_of(t):
    """Live waiters older than `t` that want any of the same devices."""
    return sum(1 for o in live_tickets()
               if (o["t"], str(o["id"])) < (t["t"], t["id"])
               and set(o["resources"]) & set(t["resources"]))


def _exit_on_term(signum, frame):
    raise SystemExit(128 + signum)  # so `finally` drops the ticket


def cmd_claim(a):
    """All-or-nothing: on any BUSY, locks newly taken by this call are rolled
    back (sorted claim order keeps overlapping sets deadlock-free). Claiming
    several resources at once forms a group — a tag stamped on each lock —
    so the whole set can later be discarded with `dibs release <group>`.

    With --wait a blocked claim joins the line (see next_in_line) and keeps
    its place only while this process runs."""
    resources = check_names(a.resources)
    owner = a.owner or default_owner()
    group = getattr(a, "as_group", None)
    if group is None and len(resources) > 1:
        group = "g-" + os.urandom(3).hex()
    if group and not NAME_RE.match(group):
        raise SystemExit(f"dibs: bad group name {group!r}")
    if group and group in (registry() or {}):
        raise SystemExit(f"dibs: group name {group!r} is a resource name; pick another")
    deadline = time.time() + a.timeout if a.timeout else None
    ticket = None
    tried_to_join = False  # only attempt join_line() once; never re-retry per poll
    term_installed = False
    term_prev_handler = signal.SIG_DFL
    try:
        while True:
            why = (busy_reason(resources, owner, ticket and ticket["id"])
                   or take_all(resources, owner, a.note, group))
            if why is None:
                for r in resources:
                    print(f"claimed {r} as {owner}")
                if group:
                    print(f"group {group}: {', '.join(resources)} "
                          f"(discard with: dibs release {group})")
                return 0
            if not a.wait:
                print("BUSY " + why, file=sys.stderr)
                return 2
            if deadline and time.time() > deadline:
                print("timeout waiting; " + why, file=sys.stderr)
                return 4
            if ticket is None and not tried_to_join:
                tried_to_join = True
                term_prev_handler = signal.getsignal(signal.SIGTERM)
                if term_prev_handler == signal.SIG_DFL:
                    signal.signal(signal.SIGTERM, _exit_on_term)
                    term_installed = True
                try:
                    ticket = join_line(resources, owner, a.note, a.poll)
                    print(f"queued for {', '.join(resources)} — {why}; "
                          f"{ahead_of(ticket)} ahead", file=sys.stderr, flush=True)
                except OSError as e:
                    print(f"not queued ({e}); waiting without a place in line",
                          file=sys.stderr, flush=True)
            elif ticket is not None:
                heartbeat(ticket)
            time.sleep(a.poll)
    finally:
        if ticket:
            leave_line(ticket)
        if term_installed:
            signal.signal(signal.SIGTERM, term_prev_handler)  # `run` keeps 0.3 behaviour


def resolve_targets(names):
    """Each name is a resource with a live lock, or a group tag: expands to
    every locked resource carrying that tag. Unknown names pass through."""
    locks = current_locks()
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


def xbar_force(plugin, name, label):
    """A SwiftBar submenu item that asks the plugin to force-release `name`."""
    return (f'--{label} | bash="{plugin}" param1=force param2={name} '
            "terminal=false refresh=true")


def xbar_safe(s):
    """A ledger-derived string (owner, note, resource/group name), made
    safe to interpolate into an xbar/SwiftBar menu line: those lines are
    '|'-delimited key=value pairs, so an unescaped '|' could inject a fake
    param, and any line break could inject a fake extra menu line/item.
    SwiftBar splits on every Unicode line break (\\v, \\f, \\x85, \\u2028,
    \\u2029, ...), not just \\r/\\n, so fold all of them via splitlines()."""
    return " ".join(str(s).splitlines()).replace("|", "¦")


def cmd_status(a):
    rows = ledger_rows()
    if a.resource:
        names = set(resolve_targets([a.resource]))
        rows = [r for r in rows if r["resource"] in names]
        if not rows:
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                print(f"{a.resource}: free")
            return 0
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if a.xbar:
        held = [r for r in rows if r.get("owner")]
        waiting = live_tickets()
        counts = ([f"✋{len(held)}"] if held else []) + ([f"⏳{len(waiting)}"] if waiting else [])
        print("dibs " + (" ".join(counts) or "✓"))
        print("---")
        if not rows:
            print("no resources.json yet | color=gray")
        plugin = os.environ.get("SWIFTBAR_PLUGIN_PATH")  # SwiftBar sets it; xbar doesn't
        for r in rows:
            name = xbar_safe(r["resource"])
            if not r.get("owner"):
                print(f"{name} — free | color=#44a05d")
                continue
            note = f" · {xbar_safe(r['note'])}" if r.get("note") else ""
            grp = f" · {xbar_safe(r['group'])}" if r.get("group") else ""
            print(f"{name} — {xbar_safe(r['owner'])}{note}{grp} | color=#e05d44")
            if (plugin and isinstance(r["resource"], str)
                    and NAME_RE.match(r["resource"])):
                print(xbar_force(plugin, r["resource"], f"Force release {name}…"))
                if (r.get("group") and isinstance(r["group"], str)
                        and NAME_RE.match(r["group"])):
                    members = ", ".join(xbar_safe(h["resource"]) for h in held
                                        if h.get("group") == r["group"])
                    print(xbar_force(plugin, r["group"],
                                     f"Force release group {xbar_safe(r['group'])} ({members})…"))
        if waiting:
            print("---")
            print("Waiting | color=gray")
            for t in waiting:
                resources = ", ".join(xbar_safe(x) for x in t["resources"])
                print(f"⏳ {xbar_safe(t['owner'])} · {age_str(t['since'])} — {resources}")
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
        if r.get("waiting"):
            print(queue_line(r["resource"], r["waiting"]))
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
    # Fix the owner now so the `finally` below compares against the same
    # value we claimed with. Recomputing default_owner() there would embed
    # whatever our *current* parent pid is, which can differ from claim
    # time if our parent has already exited (e.g. a backgrounded shell) —
    # the lock would then look like someone else's and never be released.
    a.owner = a.owner or default_owner()
    rc = cmd_claim(a)
    if rc != 0:
        return rc
    try:
        return subprocess.call(argv)
    finally:
        # The command may have run long enough for someone to force-release
        # and re-claim this resource; only drop the lock if it's still ours.
        # A non-dict record (corrupt/hand-edited) is treated as not ours.
        rec = read_lock(lock_path(a.resource))
        if isinstance(rec, dict) and rec.get("owner") == a.owner:
            try:
                lock_path(a.resource).unlink()
            except FileNotFoundError:
                pass
            print(f"released {a.resource}", file=sys.stderr)
        elif isinstance(rec, dict):
            print(f"left {a.resource} alone: now held by {rec.get('owner')}",
                  file=sys.stderr)


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
                if r.get("waiting"):
                    print("  " + queue_line(r["resource"], r["waiting"]))
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
        sp.add_argument("--wait", action="store_true", help="wait in line until claimable")
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
    sp.add_argument("resource", nargs="?", help="a resource or group tag")
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
