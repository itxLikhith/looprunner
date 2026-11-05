"""Tests for looprunner tools (unittest, no pytest needed)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools


class TestLoopRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        tools._STATE_DIR = self.tmp / "looprunner"
        tools._STATE_FILE = self.tmp / "looprunner" / "loops.json"

    def _repo(self):
        r = self.tmp / "repo"
        r.mkdir(exist_ok=True)
        return r

    def test_loop_start_requires_repo_path(self):
        res = json.loads(tools.loop_start({}, **{}))
        self.assertIn("error", res)

    def test_loop_start_rejects_missing_repo(self):
        res = json.loads(tools.loop_start({"repo_path": "/nonexistent/path"}, **{}))
        self.assertIn("error", res)

    def test_loop_start_creates_loop(self):
        repo = self._repo()
        res = json.loads(tools.loop_start({"repo_path": str(repo), "verify_cmd": "echo ok"}, **{}))
        self.assertEqual(res["status"], "running")
        self.assertIn("loop_id", res)

    def test_loop_status_no_loops(self):
        res = json.loads(tools.loop_status({}, **{}))
        self.assertIn("error", res)

    def test_loop_status_after_start(self):
        repo = self._repo()
        start = json.loads(tools.loop_start({"repo_path": str(repo)}, **{}))
        status = json.loads(tools.loop_status({"loop_id": start["loop_id"]}, **{}))
        self.assertEqual(status["loop_id"], start["loop_id"])
        self.assertEqual(status["round"], 0)

    def test_loop_tick_pass(self):
        repo = self._repo()
        start = json.loads(tools.loop_start({"repo_path": str(repo), "verify_cmd": "python -c \"print(42)\""}, **{}))
        tick = json.loads(tools.loop_tick({"loop_id": start["loop_id"]}, **{}))
        self.assertEqual(tick["result"], "PASS")
        self.assertEqual(tick["round"], 1)

    def test_loop_tick_fail(self):
        repo = self._repo()
        start = json.loads(tools.loop_start({"repo_path": str(repo), "verify_cmd": "python -c \"import sys; sys.exit(1)\""}, **{}))
        tick = json.loads(tools.loop_tick({"loop_id": start["loop_id"]}, **{}))
        self.assertEqual(tick["result"], "FAIL")
        self.assertEqual(tick["round"], 0)

    def test_loop_tick_deadline(self):
        repo = self._repo()
        start = json.loads(tools.loop_start({"repo_path": str(repo), "verify_cmd": "echo ok", "deadline": "2020-01-01T00:00:00+00:00"}, **{}))
        tick = json.loads(tools.loop_tick({"loop_id": start["loop_id"]}, **{}))
        self.assertEqual(tick["status"], "completed")
        self.assertEqual(tick["reason"], "deadline passed")

    def test_loop_tick_max_rounds(self):
        repo = self._repo()
        start = json.loads(tools.loop_start({"repo_path": str(repo), "verify_cmd": "echo ok", "max_rounds": 1}, **{}))
        tools.loop_tick({"loop_id": start["loop_id"]}, **{})
        tick2 = json.loads(tools.loop_tick({"loop_id": start["loop_id"]}, **{}))
        self.assertEqual(tick2["status"], "completed")
        self.assertEqual(tick2["reason"], "max_rounds reached")

    def test_loop_pause_resume_stop(self):
        repo = self._repo()
        start = json.loads(tools.loop_start({"repo_path": str(repo)}, **{}))
        lid = start["loop_id"]
        self.assertEqual(json.loads(tools.loop_pause({"loop_id": lid}, **{}))["status"], "paused")
        self.assertEqual(json.loads(tools.loop_resume({"loop_id": lid}, **{}))["status"], "running")
        self.assertEqual(json.loads(tools.loop_stop({"loop_id": lid}, **{}))["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
