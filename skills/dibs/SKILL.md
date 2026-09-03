---
name: dibs
description: Claim and release shared physical resources (test phones, GPUs, dev boards, bench hardware) through the `dibs` lock ledger so parallel agent sessions don't collide. Use BEFORE any command that touches shared hardware (device automation, ssh to a rig, GPU jobs), and to answer "who is using X?". Claim first, wait if held, release when done.
---

# dibs — claim shared hardware before touching it

`dibs` is an advisory lock ledger. `dibs status` lists the resources this
machine defines. Locks never expire — a human breaks a stuck one. Your job:
never touch shared hardware you haven't claimed.

## Protocol

1. **Use a stable owner id on every call.** The default id changes per shell,
   so a later `release` would fail. Pass it inline each time:
   ```bash
   DIBS_OWNER="agent:<task-slug>" dibs claim phone-a --note "why"
   ```
   - exit `0` → yours. exit `2` → BUSY; stderr says who has it, since when, why.
   - On BUSY: do other prep and retry, or block with
     `--wait --timeout 1800` (exit `4` on timeout). Never `release --force`
     someone else's lock — report the holder to the user instead.
2. **Need several things at once? Claim them in ONE call.**
   ```bash
   DIBS_OWNER="agent:<slug>" dibs claim phone-a gpu-0 --note "regression run"
   # → group g-3fa2c1: gpu-0, phone-a (discard with: dibs release g-3fa2c1)
   ```
   One call is all-or-nothing and can't half-block another agent. Claiming
   members one by one can.
3. **Release the moment hardware work ends** — not after analysis, not at
   the end of the session. Release by name or by group tag:
   ```bash
   DIBS_OWNER="agent:<slug>" dibs release g-3fa2c1
   ```
4. **Single bounded command → use the wrapper** (releases even on failure):
   ```bash
   dibs run --note "smoke" gpu-0 -- python train.py --smoke
   ```
   (dibs flags go before the resource; the command after `--`.)

## Checks

- `dibs status` — whole ledger; `dibs status --json` for parsing.
- Unknown name rejected? Only names in `~/.dibs/resources.json` exist — use
  what `dibs status` lists; ask the user before adding a resource.
- If your session is ending and you still hold locks, release them first.
