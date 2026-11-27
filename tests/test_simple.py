import asyncio
import contextlib
import io
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

    def test_stream_true_prints_and_returns_the_answer(self):
        out = io.StringIO()
        with self.rb() as rb, contextlib.redirect_stdout(out):
            r = rb.run("streamed", stream=True)
        self.assertEqual((out.getvalue(), r.text), ("echo: streamed\n", "echo: streamed"))
        chunks = []
        with self.rb(stream=chunks.append) as rb:
            self.assertEqual(rb.run("to a function").text, "echo: to a function")
        self.assertEqual("".join(chunks), "echo: to a function")

    def test_stream_method_is_the_iterator(self):
        with self.rb() as rb:
            s = rb.stream("streamed")
            self.assertEqual("".join(s), "echo: streamed")
            self.assertEqual(s.response.text, "echo: streamed")
            self.assertIn("message.delta", [e["type"] for e in s.events])
            self.assertEqual(rb.stream("again").text(), "echo: again")

    def test_everything_set_once(self):
        out = io.StringIO()
        with self.rb(chat=True, stream=True) as rb, contextlib.redirect_stdout(out):
            first = rb.run("one")
            rest = rb.run(["two", "three"])
            self.assertEqual([r.session_id for r in rest], [first.session_id] * 2)
            rb.reset()
            self.assertNotEqual(rb.run("four").session_id, first.session_id)
        self.assertEqual(out.getvalue(), "echo: one\necho: two\necho: three\necho: four\n")
        with self.rb() as rb:
            with self.assertRaises(TypeError):
                rb.run(["a", "b"])
            self.assertEqual([r.text for r in rb.run(["a", "b"], chat=True)], ["echo: a", "echo: b"])

    def test_chat_keeps_one_session(self):
        with self.rb() as rb, rb.chat() as chat:
            first, second = chat.send("one"), chat.send("two")
            self.assertEqual((first.text, second.text), ("echo: one", "echo: two"))
            self.assertEqual(first.session_id, second.session_id)
            self.assertEqual("".join(chat.stream("three")), "echo: three")
            self.assertEqual([r.text for r in chat.send(["four", "five"])], ["echo: four", "echo: five"])

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

    def test_models(self):
        with self.rb() as rb:
            r = rb.run("mode?", model="large")
            self.assertIn("model: large", r.text)
            self.assertEqual(r.model, "large")
            self.assertTrue(rb.run("mode?", model="auto").model)
            with self.assertRaises(rbridge.RbridgeError) as cm:
                rb.run("x", model="larg")
            self.assertEqual(cm.exception.code, "invalid_input")
            self.assertIn("did you mean large?", cm.exception.message)
            fake = next(x for x in rb.runtimes() if x["id"] == "fake")
            self.assertIn("latest", fake["models"])

    def test_budget_stops_new_runs(self):
        with self.rb(max_cost_usd=0.0) as rb:
            with self.assertRaises(rbridge.RbridgeError) as cm:
                rb.run("x")
            self.assertEqual(cm.exception.code, "budget_exceeded")

    def test_async(self):
        async def main():
            rb = AsyncRbridge(workspace=self.d.workspace, env=self.d.env)
            a, b = await asyncio.gather(rb.run("a"), rb.run("b"))
            s = await rb.stream("s")
            chunks = [c async for c in s]
            got = []
            await rb.run("cb", stream=got.append)
            assert "".join(got) == "echo: cb"
            chat = rb.chat()
            one = await chat.send("one")
            two = await chat.send("two")
            await chat.close()
            await rb.close()
            return a.text, b.text, "".join(chunks), one.session_id == two.session_id

        self.assertEqual(asyncio.run(main()), ("echo: a", "echo: b", "echo: s", True))


class ExitClosesTest(unittest.TestCase):
    """A program that ends without closing must not leave sessions holding rbridge's slots."""

    @classmethod
    def setUpClass(cls):
        cls.d = Daemon("echo")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_conversations_close_when_the_program_exits(self):
        import subprocess
        import sys
        child = (
            "from rbridge import Rbridge\n"
            f"rb = Rbridge(workspace={self.d.workspace!r}, env={self.d.env!r}, chat=True)\n"
            "rb.run('one')\n"
            "other = rb.chat()\n"
            "other.send('two')\n"
        )
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        subprocess.run([sys.executable, "-c", child], check=True, timeout=60,
                       env=dict(os.environ, PYTHONPATH=src, **self.d.env))
        with rbridge.connect(env=self.d.env) as c:
            open_now = [s for s in c.sessions() if s.get("state") not in ("closed", "interrupted")]
        self.assertEqual(open_now, [])


class AsyncCrowdTest(unittest.TestCase):
    """Many conversations at once, more than rbridge has slots and the default
    thread pool has threads: each must still finish."""

    @classmethod
    def setUpClass(cls):
        cls.d = Daemon("echo", execution=["max_concurrent_sessions: 2"])

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_two_turn_chats_beyond_slots_and_threads_all_finish(self):
        from concurrent.futures import ThreadPoolExecutor

        async def main():
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(4))
            rb = AsyncRbridge(workspace=self.d.workspace, env=self.d.env)

            async def talk(i):
                async with rb.chat() as chat:
                    await chat.send(f"draft {i}")
                    return (await chat.send(f"check {i}")).text

            out = await asyncio.wait_for(asyncio.gather(*(talk(i) for i in range(40))), 120)
            await rb.close()
            return out

        self.assertEqual(asyncio.run(main()), [f"echo: check {i}" for i in range(40)])


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



class FilesAndSchemasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon("echo")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_files_go_to_the_runtime(self):
        import io
        src = os.path.join(self.d.home, "in")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "notes.md"), "w") as f:
            f.write("# Notes\n")

        class Upload:  # the shape of FastAPI's UploadFile
            filename = "report.txt"
            file = io.BytesIO(b"uploaded text")

        with Rbridge(env=self.d.env) as rb:
            r = rb.run("read them", files=[os.path.join(src, "notes.md"), ("data.csv", b"a,b\n1,2\n"), Upload()])
            self.assertIn("Files for this task, in your working folder: notes.md; data.csv; report.txt.", r.text)
            self.assertIn("read them", r.text)

    def test_pydantic_schema(self):
        try:
            import pydantic
        except ImportError:
            self.skipTest("pydantic not installed")

        class Answer(pydantic.BaseModel):
            echo: str

        with Rbridge(env=self.d.env, workspace=self.d.workspace) as rb:
            r = rb.run("x", schema=Answer)
            self.assertIsNotNone(r.raw)  # the fake runtime returns no JSON; nothing to parse
            self.assertIsNone(r.parsed)

    def test_skills_list(self):
        skill = os.path.join(self.d.home, "src2", "tidy")
        os.makedirs(skill)
        with open(os.path.join(skill, "SKILL.md"), "w") as f:
            f.write("---\nname: tidy\ndescription: Keep things tidy.\n---\nTidy.\n")
        with Rbridge(env=self.d.env) as rb:
            rb.add_skills(skill)
            self.assertIn({"id": "tidy", "description": "Keep things tidy.", "trust": "reviewed", "version": None, "scripts": False},
                          rb.skills())



if __name__ == "__main__":
    unittest.main()
