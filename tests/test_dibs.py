"""Black-box tests for the dibs CLI. Stdlib only: python3 -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIBS = str(Path(__file__).resolve().parent.parent / "dibs.py")


class DibsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "resources.json").write_text(json.dumps(
            {"gpu": "RTX 4090", "phone": "test phone", "c2": "dev lock", "slider": "slider rig"}))

    def tearDown(self):
        self.tmp.cleanup()

    def dibs(self, *args, owner="a"):
        env = dict(os.environ, DIBS_DIR=str(self.dir), DIBS_OWNER=owner)
        return subprocess.run([sys.executable, DIBS, *args], env=env,
                              capture_output=True, text=True)

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


if __name__ == "__main__":
    unittest.main()
