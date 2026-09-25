"""Black-box tests for the dibs CLI. Stdlib only: python3 -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIBS = str(Path(__file__).resolve().parent.parent / "dibs.py")


class DibsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # after any background dibs is stopped
        self.dir = Path(self.tmp.name)
        (self.dir / "resources.json").write_text(json.dumps(
            {"gpu": "RTX 4090", "phone": "test phone", "c2": "dev lock", "slider": "slider rig"}))

    def env(self, owner, **extra):
        base = {k: v for k, v in os.environ.items() if k != "SWIFTBAR_PLUGIN_PATH"}
        return dict(base, DIBS_DIR=str(self.dir), DIBS_OWNER=owner,
                    PYTHONIOENCODING="utf-8", **extra)

    def dibs(self, *args, owner="a", **extra):
        return subprocess.run([sys.executable, DIBS, *args], env=self.env(owner, **extra),
                              capture_output=True, encoding="utf-8", timeout=60)

    def ticket(self, owner, *resources, age=0):
        """Hand-write a waiter's ticket, `age` seconds old (stale past 15)."""
        q = self.dir / "queue"
        q.mkdir(exist_ok=True)
        tid = os.urandom(4).hex()
        path = q / f"{tid}.json"
        path.write_text(json.dumps({
            "id": tid, "owner": owner, "resources": sorted(resources), "note": None,
            "host": "test", "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "t": time.time(), "poll": 5}))
        if age:
            past = time.time() - age
            os.utime(path, (past, past))
        return path

    def tickets(self):
        return sorted((self.dir / "queue").glob("*.json"))

    def wait_for(self, cond, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def holders(self):
        rows = json.loads(self.dibs("status", "--json").stdout)
        return {r["resource"]: r["owner"] for r in rows if r.get("owner")}

    def test_claim_busy_release(self):
        self.assertEqual(self.dibs("claim", "gpu", "--note", "train").returncode, 0)
        busy = self.dibs("claim", "gpu", owner="b")
        self.assertEqual(busy.returncode, 2)
        self.assertIn("held by a", busy.stderr)
        self.assertIn('"train"', busy.stderr)
        self.assertEqual(self.dibs("release", "gpu", owner="b").returncode, 3)
        self.assertEqual(self.dibs("release", "gpu").returncode, 0)
        self.assertEqual(self.holders(), {})

    def test_force_breaks_lock(self):
        self.dibs("claim", "gpu")
        out = self.dibs("release", "gpu", "--force", owner="b")
        self.assertEqual(out.returncode, 0)
        self.assertIn("broke gpu", out.stdout)

    def test_reclaim_is_idempotent(self):
        self.dibs("claim", "gpu")
        self.assertEqual(self.dibs("claim", "gpu").returncode, 0)
        self.assertEqual(self.holders(), {"gpu": "a"})

    def test_race_has_one_winner(self):
        with ThreadPoolExecutor(10) as ex:
            results = list(ex.map(lambda i: self.dibs("claim", "gpu", owner=f"r{i}"), range(10)))
        self.assertEqual(sum(r.returncode == 0 for r in results), 1)
        self.assertEqual(sum(r.returncode == 2 for r in results), 9)

    def test_group_all_or_nothing(self):
        self.dibs("claim", "c2", owner="other")
        out = self.dibs("claim", "phone", "c2", "slider")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(self.holders(), {"c2": "other"})  # rollback: no partial set

    def test_group_tag_release(self):
        out = self.dibs("claim", "phone", "c2", "--as", "bench")
        self.assertEqual(out.returncode, 0)
        self.assertIn("group bench: c2, phone", out.stdout)
        self.assertEqual(self.dibs("release", "bench", owner="b").returncode, 3)
        self.assertEqual(self.holders(), {"phone": "a", "c2": "a"})
        self.assertEqual(self.dibs("release", "bench").returncode, 0)
        self.assertEqual(self.holders(), {})

    def test_auto_group_tag(self):
        out = self.dibs("claim", "phone", "c2").stdout
        tag = next(l.split()[1].rstrip(":") for l in out.splitlines() if l.startswith("group "))
        self.assertTrue(tag.startswith("g-"))
        self.assertEqual(self.dibs("release", tag).returncode, 0)
        self.assertEqual(self.holders(), {})

    def test_only_defined_names(self):
        out = self.dibs("claim", "gpi")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not defined", out.stderr)
        self.assertIn("RTX 4090", self.dibs("status").stdout)

    def test_registry_required(self):
        (self.dir / "resources.json").unlink()
        out = self.dibs("claim", "gpu")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no resources defined", out.stderr)

    def test_run_releases_on_failure(self):
        out = self.dibs("run", "gpu", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(out.returncode, 7)
        self.assertEqual(self.holders(), {})

    def test_wait_timeout(self):
        self.dibs("claim", "gpu")
        out = self.dibs("wait", "gpu", "--timeout", "1", "--poll", "1")
        self.assertEqual(out.returncode, 4)

    def test_claim_cannot_cut_in_front_of_a_waiter(self):
        self.ticket("b", "gpu")
        out = self.dibs("claim", "gpu")
        self.assertEqual(out.returncode, 2)
        self.assertIn("gpu: free, but next in line is b", out.stderr)
        self.assertEqual(self.holders(), {})

    def test_waiter_that_could_go_reserves_its_devices(self):
        self.ticket("g", "phone", "c2")
        out = self.dibs("claim", "c2")
        self.assertEqual(out.returncode, 2)
        self.assertIn("next in line is g", out.stderr)

    def test_blocked_waiter_reserves_nothing(self):
        self.dibs("claim", "c2", owner="x")
        self.ticket("g", "phone", "c2")  # g still needs c2, so phone stays usable
        self.assertEqual(self.dibs("claim", "phone").returncode, 0)

    def test_own_ticket_does_not_block_its_owner(self):
        self.ticket("a", "gpu")
        self.assertEqual(self.dibs("claim", "gpu").returncode, 0)

    def test_stale_ticket_is_ignored_and_removed(self):
        stale = self.ticket("b", "gpu", age=60)
        self.assertEqual(self.dibs("claim", "gpu").returncode, 0)
        self.assertFalse(stale.exists())

    def test_junk_in_the_queue_dir_is_ignored(self):
        q = self.dir / "queue"
        q.mkdir()
        (q / "junk.json").write_text("not json")
        (q / "partial.json").write_text(json.dumps({"id": "x", "owner": "b"}))
        self.assertEqual(self.dibs("claim", "gpu").returncode, 0)
        self.assertEqual(self.dibs("status").returncode, 0)


if __name__ == "__main__":
    unittest.main()
