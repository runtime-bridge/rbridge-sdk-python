"""Tests against a real rbridge daemon whose only runtime is the fake ACP agent.

RBRIDGE_BIN and RBRIDGE_FAKE_AGENT point at the binaries; by default they come
from a sibling rbridge checkout (make build fakeagent).
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

import rbridge

HERE = os.path.dirname(os.path.abspath(__file__))
SIBLING = os.path.normpath(os.path.join(HERE, "..", "..", "rbridge", "bin"))
RBRIDGE_BIN = os.environ.get("RBRIDGE_BIN", os.path.join(SIBLING, "rbridge"))
FAKE_AGENT = os.environ.get("RBRIDGE_FAKE_AGENT", os.path.join(SIBLING, "rbridge-fakeagent"))


class Daemon:
    def __init__(self, scenario="echo", execution=()):
        self.home = tempfile.mkdtemp(prefix="rbpy-")
        self.workspace = os.path.join(self.home, "ws")
        os.makedirs(self.workspace)
        os.makedirs(os.path.join(self.home, "config"))
        with open(os.path.join(self.home, "config", "config.yaml"), "w") as f:
            f.write("\n".join([
                "version: 1",
                "providers:",
                "  order: [fake]",
                "  custom:",
                "    - id: fake",
                f"      launch: {{ command: {json.dumps(FAKE_AGENT)}, env_passthrough: [RBRIDGE_FAKE_ACP_SCENARIO] }}",
                "      version_args: [--version]",
                "daemon:",
                "  http: { enabled: true, port: 0 }",
                "  requests_per_second: 1000",
                "execution:",
                "  kill_grace_period: 1s",
                *(f"  {line}" for line in execution),
                "",
            ]))
        env = dict(os.environ, RBRIDGE_HOME=self.home, RBRIDGE_FAKE_ACP_SCENARIO=scenario)
        self.env = {"RBRIDGE_HOME": self.home}
        self.proc = subprocess.Popen([RBRIDGE_BIN, "daemon", "run"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        state = os.path.join(self.home, "run", "daemon.json")
        for _ in range(100):
            if os.path.exists(state):
                break
            time.sleep(0.1)
        else:
            self.stop()
            raise RuntimeError("daemon did not start")

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=30)
        self.proc.stderr.close()
        shutil.rmtree(self.home, ignore_errors=True)


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon("echo")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_run_over_ipc(self):
        types = []
        with rbridge.connect(client="py", env=self.d.env) as c:
            res = c.run({"workspace": self.d.workspace, "prompt": "from python"}, on_event=lambda e: types.append(e["type"]))
            self.assertEqual(res["output"], "echo: from python")
            self.assertEqual(res["stop_reason"], "end_turn")
            self.assertIn("session.started", types)
            self.assertIn("message.delta", types)
            self.assertEqual(c.sessions()[0]["state"], "closed")

    def test_multi_turn_and_resume(self):
        with rbridge.connect(client="py-multi", env=self.d.env) as c:
            s = c.open({"workspace": self.d.workspace})
            self.assertEqual(s.prompt("one")["output"], "echo: one")
            self.assertEqual(s.prompt(rbridge.text("two"))["output"], "echo: two")
            s.close()
            with c.resume(s.id) as again:
                self.assertEqual(again.prompt("three")["output"], "echo: three")

    def test_errors_keep_their_code(self):
        with rbridge.connect(env=self.d.env) as c:
            with self.assertRaises(rbridge.RbridgeError) as cm:
                c.run({"workspace": "relative", "prompt": "x"})
            self.assertEqual(cm.exception.code, "invalid_input")
            with self.assertRaises(rbridge.RbridgeError) as cm:
                c.resume("ses_missing")
            self.assertEqual(cm.exception.code, "session_not_found")
        with self.assertRaises(rbridge.RbridgeError) as cm:
            rbridge.connect(socket_path=os.path.join(self.d.home, "nope.sock"))
        self.assertEqual(cm.exception.code, "provider_unavailable")

    def test_clients_are_isolated(self):
        with rbridge.connect(client="py-a", env=self.d.env) as a, rbridge.connect(client="py-b", env=self.d.env) as b:
            s = a.open({"workspace": self.d.workspace})
            self.assertEqual(b.sessions(), [])
            with self.assertRaises(rbridge.RbridgeError):
                b.resume(s.id)
            s.close()

    def test_http(self):
        c = rbridge.connect_http(client="py-http", env=self.d.env)
        self.assertGreater(c.status()["pid"], 0)
        self.assertTrue(any(p["id"] == "fake" and p["runtime"]["health"] == "available" for p in c.providers()))
        types = []
        res = c.run({"workspace": self.d.workspace, "prompt": "over http"}, on_event=lambda e: types.append(e["type"]))
        self.assertEqual(res["output"], "echo: over http")
        for _ in range(20):
            if "turn.started" in types:
                break
            time.sleep(0.05)
        self.assertIn("turn.started", types)


class ApprovalTest(unittest.TestCase):
    def test_approver(self):
        d = Daemon("permission")
        try:
            with rbridge.connect(env=d.env) as c:
                asked = []
                res = c.run({"workspace": d.workspace, "prompt": "write", "profile": "controlled"},
                            approver=lambda req: asked.append(req["title"]) or True)
                self.assertEqual(res["output"], "allowed")
                self.assertEqual(asked, ["Write notes.txt"])
                os.remove(os.path.join(d.workspace, "notes.txt"))
                res = c.run({"workspace": d.workspace, "prompt": "write", "profile": "controlled"})
                self.assertEqual(res["output"], "denied")
                calls = []
                with c.open({"workspace": d.workspace, "profile": "controlled"},
                            approver=lambda req: calls.append(1) or {"allow": True, "remember": True}) as s:
                    self.assertEqual(s.prompt("write")["output"], "allowed")
                    self.assertEqual(s.prompt("write")["output"], "allowed")
                self.assertEqual(len(calls), 1)
        finally:
            d.stop()


class ExtensionsTest(unittest.TestCase):
    def test_modes_and_options(self):
        d = Daemon("echo")
        try:
            for c in (rbridge.connect(env=d.env), rbridge.connect_http(env=d.env)):
                with c, c.open({"workspace": d.workspace}, mode="code") as s:
                    m = s.modes()
                    self.assertEqual(m["current"], "code")
                    self.assertEqual([x["id"] for x in m["modes"]], ["ask", "code"])
                    s.set_mode("ask")
                    self.assertEqual(s.set_option("model", "large")[0]["current"], "large")
                    self.assertEqual(s.prompt("mode?")["output"], "mode: ask model: large")
                    with self.assertRaises(rbridge.RbridgeError) as cm:
                        s.set_mode("nope")
                    self.assertEqual(cm.exception.code, "invalid_input")
        finally:
            d.stop()

    def test_on_input(self):
        d = Daemon("elicit")
        try:
            with rbridge.connect(env=d.env) as c:
                asked = []
                res = c.run({"workspace": d.workspace, "prompt": "x"},
                            on_input=lambda req: asked.append(req) or {"action": "accept", "content": {"name": "Ada"}})
                self.assertEqual(res["output"], "hello Ada")
                self.assertEqual(asked[0]["mode"], "form")
                self.assertEqual(c.run({"workspace": d.workspace, "prompt": "x"})["output"], "declined")
        finally:
            d.stop()


class SkillsTest(unittest.TestCase):
    def test_add_and_trust(self):
        d = Daemon("echo")
        try:
            skill = os.path.join(d.home, "src", "py-style")
            os.makedirs(skill)
            with open(os.path.join(skill, "SKILL.md"), "w") as f:
                f.write("---\nname: py-style\ndescription: Python style rules.\n---\nbody\n")
            for c in (rbridge.connect(env=d.env), rbridge.connect_http(env=d.env)):
                with c:
                    added = c.add_skill(skill)
                    self.assertEqual(added["record"]["id"], "py-style")
                    self.assertEqual(c.set_skill_trust("py-style", "reviewed")["trust"], "reviewed")
                    self.assertIn("py-style", [s["id"] for s in c.skills()])
                    with self.assertRaises(rbridge.RbridgeError) as cm:
                        c.search_skills("x")
                    self.assertEqual(cm.exception.code, "invalid_input")
                    many = c.add_skills(os.path.dirname(skill), only=["py-*"], trust="trusted")
                    self.assertEqual([(i["id"], i["status"]) for i in many["items"]], [("py-style", "unchanged")])
                    self.assertEqual(many["items"][0]["result"]["record"]["trust"], "trusted")
                    with self.assertRaises(rbridge.RbridgeError) as cm:
                        c.add_skills(os.path.dirname(skill), only=["nosuch"])
                    self.assertEqual(cm.exception.code, "skill_not_found")
        finally:
            d.stop()


class RuntimeDirTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(rbridge.runtime_dir({"RBRIDGE_HOME": "/r"}), os.path.join("/r", "run"))
        if os.name == "posix" and not __import__("sys").platform == "darwin":
            self.assertEqual(rbridge.runtime_dir({"XDG_RUNTIME_DIR": "/run/user/1"}), "/run/user/1/rbridge")


if __name__ == "__main__":
    unittest.main()
