import asyncio
import os
import unittest

import rbridge
from rbridge import AsyncRbridge, Rbridge

from test_client import Daemon


class SimpleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon("echo")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def rb(self, **kw):
        return Rbridge(workspace=self.d.workspace, env=self.d.env, **kw)

    def test_run_returns_text_and_usage(self):
        with self.rb() as rb:
            r = rb.run("hello")
            self.assertEqual((r.text, str(r), r.runtime, r.stop_reason), ("echo: hello", "echo: hello", "fake", "end_turn"))
            self.assertTrue(r.session_id.startswith("ses_"))

    def test_stream_is_one_argument(self):
        with self.rb() as rb:
            s = rb.run("streamed", stream=True)
            self.assertEqual("".join(s), "echo: streamed")
            self.assertEqual(s.response.text, "echo: streamed")
            self.assertIn("message.delta", [e["type"] for e in s.events])
            self.assertEqual(rb.run("again", stream=True).text(), "echo: again")

    def test_chat_keeps_one_session(self):
        with self.rb() as rb, rb.chat() as chat:
            first, second = chat.send("one"), chat.send("two")
            self.assertEqual((first.text, second.text), ("echo: one", "echo: two"))
            self.assertEqual(first.session_id, second.session_id)
            self.assertEqual("".join(chat.send("three", stream=True)), "echo: three")

    def test_run_many_keeps_order(self):
        with self.rb() as rb:
            out = rb.run_many([f"p{i}" for i in range(6)] + [{"prompt": "own", "workspace": self.d.workspace}])
            self.assertEqual([r.text for r in out], [f"echo: p{i}" for i in range(6)] + ["echo: own"])
            bad = rb.run_many(["ok", {"prompt": "x", "runtime": "nope"}], return_exceptions=True)
            self.assertEqual(bad[0].text, "echo: ok")
            self.assertIsInstance(bad[1], rbridge.RbridgeError)
            with self.assertRaises(rbridge.RbridgeError):
                rb.run_many(["ok", {"prompt": "x", "runtime": "nope"}])

    def test_options_and_errors(self):
        with self.rb(runtime="nope") as rb:
            with self.assertRaises(rbridge.RbridgeError) as cm:
                rb.run("x")
            self.assertEqual(cm.exception.code, "provider_not_found")
            self.assertEqual(rb.run("x", runtime="fake").text, "echo: x")
            with self.assertRaises(TypeError):
                rb.run("x", runtim="fake")

    def test_runtimes(self):
        with self.rb() as rb:
            self.assertIn({"id": "fake", "available": True}, [{k: r[k] for k in ("id", "available")} for r in rb.runtimes()])

    def test_skill_folder_is_added_and_used(self):
        skill = os.path.join(self.d.home, "src", "tone")
        os.makedirs(skill)
        with open(os.path.join(skill, "SKILL.md"), "w") as f:
            f.write("---\nname: tone\ndescription: Keep a calm tone.\n---\nBe calm.\n")
        with self.rb(runtime="fake") as rb:
            # The fake runtime cannot load skills, so the run is refused, but
            # only after the folder was added and trusted.
            with self.assertRaises(rbridge.RbridgeError) as cm:
                rb.run("x", skills=[skill])
            self.assertEqual(cm.exception.code, "capability_unavailable")
            self.assertIn(("tone", "reviewed"), [(s["id"], s["trust"]) for s in rb.client.skills()])
            self.assertEqual(rb.add_skills(os.path.dirname(skill)), ["tone"])

    def test_budget_stops_new_runs(self):
        with self.rb(max_cost_usd=0.0) as rb:
            with self.assertRaises(rbridge.RbridgeError) as cm:
                rb.run("x")
            self.assertEqual(cm.exception.code, "budget_exceeded")

    def test_async(self):
        async def main():
            rb = AsyncRbridge(workspace=self.d.workspace, env=self.d.env)
            a, b = await asyncio.gather(rb.run("a"), rb.run("b"))
            s = await rb.run("s", stream=True)
            chunks = [c async for c in s]
            chat = rb.chat()
            one = await chat.send("one")
            two = await chat.send("two")
            await chat.close()
            await rb.close()
            return a.text, b.text, "".join(chunks), one.session_id == two.session_id

        self.assertEqual(asyncio.run(main()), ("echo: a", "echo: b", "echo: s", True))


class SimpleApprovalTest(unittest.TestCase):
    def test_approve(self):
        d = Daemon("permission")
        try:
            opts = dict(workspace=d.workspace, env=d.env, profile="controlled")
            with Rbridge(**opts) as rb:
                self.assertEqual(rb.run("write").text, "denied")
            with Rbridge(approve=True, **opts) as rb:
                self.assertEqual(rb.run("write").text, "allowed")
            os.remove(os.path.join(d.workspace, "notes.txt"))
            asked = []
            with Rbridge(approve=lambda req: asked.append(req["title"]) or True, **opts) as rb:
                self.assertEqual(rb.run("write").text, "allowed")
            self.assertEqual(asked, ["Write notes.txt"])
        finally:
            d.stop()


if __name__ == "__main__":
    unittest.main()
