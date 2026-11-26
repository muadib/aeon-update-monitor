import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from aeonmon import client, collectors

FX = os.path.join(os.path.dirname(__file__), "fixtures")
HIST = os.path.join(FX, "zypp_history")
SNAP = open(os.path.join(FX, "snapper.json")).read()


class Parsing(unittest.TestCase):
    def test_history_transactions(self):
        txs = collectors.parse_history(HIST)
        self.assertEqual(len(txs), 2)
        self.assertEqual(txs[0]["time"][:10], "2026-11-24")  # más reciente primero
        self.assertEqual(txs[0]["kind"], "dup")
        acts = {c["name"]: c for c in txs[0]["changes"]}
        self.assertEqual(acts["bash"]["action"], "upgraded")
        self.assertEqual((acts["bash"]["from"], acts["bash"]["to"]), ("5.3.1-1.1", "5.3.2-1.1"))
        self.assertEqual(acts["libfoo"]["action"], "downgraded")

    def test_install_remove(self):
        old = collectors.parse_history(HIST)[1]
        acts = {c["name"]: c["action"] for c in old["changes"]}
        self.assertEqual(acts["newpkg"], "installed")
        self.assertEqual(acts["oldpkg"], "removed")
        self.assertEqual(acts["kernel-default"], "installed")  # multiversion: sin remove

    def test_since_and_limit(self):
        u = collectors.collect_updates(limit=1, since="2026-11-20", history=HIST)
        self.assertEqual(len(u["transactions"]), 1)
        self.assertEqual(u["total_transactions"], 1)

    def test_snapper(self):
        s = collectors.parse_snapper(SNAP)
        self.assertEqual([x["number"] for x in s], [42, 41, 0])
        self.assertTrue(s[0]["default"] and not s[0]["active"])
        self.assertTrue(s[1]["active"])

    def test_zypper_xml(self):
        xml = '<stream><update-status><update-list><update name="vim" edition="9.2-1" edition-old="9.1-1" arch="x86_64" kind="package"/></update-list></update-status></stream>'
        self.assertEqual(collectors.parse_zypper_updates(xml)[0]["to"], "9.2-1")

    def test_vercmp(self):
        self.assertEqual(collectors.rpmvercmp("1.10", "1.9"), 1)
        self.assertEqual(collectors.rpmvercmp("20251120-1.1", "20251118-1.1"), 1)


class Integration(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(collectors, "ZYPP_HISTORY", HIST)
        p2 = mock.patch.object(collectors, "_run", return_value=(0, SNAP, ""))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_status_reboot_pending(self):
        st = collectors.collect_status(history=HIST)
        self.assertEqual(st["running_snapshot"], 41)
        self.assertEqual(st["next_boot_snapshot"], 42)
        self.assertTrue(st["reboot_pending"])
        self.assertEqual(st["last_dup"]["packages"], 2)

    def test_backend_cli_prints_json(self):
        import subprocess, sys
        env = {**os.environ, "AEONMON_HISTORY": HIST}
        out = subprocess.run([sys.executable, "-m", "aeonmon", "updates", "--limit", "1"],
                             capture_output=True, text=True, env=env, check=True).stdout
        self.assertEqual(json.loads(out)["data"]["total_transactions"], 2)
        r = client.fetch("updates", limit=1, since="2026-11-20")  # el env no se hereda del mock
        self.assertIn("transactions", r["data"])

    def test_backend_error_exit_code(self):
        import subprocess, sys
        p = subprocess.run([sys.executable, "-m", "aeonmon", "bogus"], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)


class Elevation(unittest.TestCase):
    """fetch_auto: pkexec first, graceful fallback when cancelled."""

    def setUp(self):
        import stat, tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"AEONMON_HISTORY": HIST, "PATH": f"{self.tmp.name}:{os.environ['PATH']}"}
        for p in (mock.patch.dict(os.environ, self.env), mock.patch("os.geteuid", return_value=1000)):
            p.start(); self.addCleanup(p.stop)
        self._stat = stat

    def fake_pkexec(self, script):
        path = os.path.join(self.tmp.name, "pkexec")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + script + "\n")
        os.chmod(path, 0o755)

    def test_privileged_when_pkexec_succeeds(self):
        self.fake_pkexec('exec "$@"')  # pretend authentication worked
        resp, state = client.fetch_auto("updates", limit=1)
        self.assertEqual(state, "privileged")
        self.assertEqual(resp["data"]["total_transactions"], 2)

    def test_dialog_cancelled_falls_back(self):
        self.fake_pkexec("exit 126")
        resp, state = client.fetch_auto("updates", limit=1)
        self.assertEqual(state, "declined")
        self.assertEqual(resp["data"]["total_transactions"], 2)  # still got data

    def test_auth_failed_falls_back(self):
        self.fake_pkexec("exit 127")
        self.assertEqual(client.fetch_auto("updates")[1], "declined")

    def test_pkexec_missing_falls_back(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            # backend launcher uses sys.executable (absolute), so only pkexec is missing
            self.assertEqual(client.fetch_auto("updates")[1], "declined")

    def test_real_backend_error_is_not_swallowed(self):
        self.fake_pkexec("exit 1")  # backend failure, not a cancelled dialog
        with self.assertRaises(client.BackendError):
            client.fetch_auto("updates")

    def test_no_elevation_requested(self):
        self.fake_pkexec("exit 99")  # must never be called
        self.assertEqual(client.fetch_auto("updates", elevate=False)[1], "plain")

    def test_custom_backend_used_as_is(self):
        self.fake_pkexec("exit 99")
        r, state = client.fetch_auto("updates", backend=client.DEFAULT_BACKEND)
        self.assertEqual(state, "custom")


if __name__ == "__main__":
    unittest.main()
