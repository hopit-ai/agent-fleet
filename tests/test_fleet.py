"""Unit tests for fleet. No network, no auth, no backends required."""

import argparse
import importlib.util
import json
import os
import re
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_fleet():
    """bin/fleet has no .py extension, so give importlib an explicit source loader."""
    path = str(ROOT / "bin" / "fleet")
    spec = importlib.util.spec_from_loader("fleet", SourceFileLoader("fleet", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fleet = load_fleet()
ROSTER = json.loads((ROOT / "roster.json").read_text())


def with_backends(*names):
    """Pretend exactly these backends are installed."""
    return lambda roster, backend: ("/fake/bin/%s" % backend) if backend in names else None


class RosterIntegrity(unittest.TestCase):
    """The roster is user-editable config, so its invariants are worth enforcing."""

    def test_tier_defaults_reference_real_models(self):
        for tier, mapping in ROSTER["tier_defaults"].items():
            self.assertIn(tier, ROSTER["tier_order"], "unknown tier %s" % tier)
            for backend, key in mapping.items():
                self.assertIn(key, ROSTER["models"], "%s/%s -> missing model %s" % (tier, backend, key))
                self.assertEqual(ROSTER["models"][key]["backend"], backend)
                self.assertEqual(ROSTER["models"][key]["tier"], tier)

    def test_every_model_has_a_known_backend_and_tier(self):
        for key, m in ROSTER["models"].items():
            self.assertIn(m["backend"], ROSTER["backends"], "%s: bad backend" % key)
            self.assertIn(m["tier"], ROSTER["tier_order"], "%s: bad tier" % key)
            self.assertTrue(m["id"], "%s: empty model id" % key)

    def test_routing_table_is_total(self):
        for kind, row in ROSTER["routing"].items():
            if kind.startswith("_"):
                continue
            for c in fleet.COMPLEXITIES:
                self.assertIn(c, row, "routing[%s] missing complexity %s" % (kind, c))
                self.assertIn(row[c], ROSTER["tier_order"], "routing[%s][%s] bad tier" % (kind, c))

    def test_every_tier_has_an_effort(self):
        for tier in ROSTER["tier_order"]:
            self.assertIn(tier, ROSTER["effort"])

    def test_complexity_never_routes_downward(self):
        """Harder work must never land on a strictly weaker tier than easier work."""
        order = ROSTER["tier_order"]
        for kind, row in ROSTER["routing"].items():
            if kind.startswith("_"):
                continue
            ranks = [order.index(row[c]) for c in fleet.COMPLEXITIES]
            self.assertEqual(ranks, sorted(ranks), "routing[%s] regresses: %s" % (kind, ranks))


class Routing(unittest.TestCase):
    def setUp(self):
        self._orig = fleet.backend_bin

    def tearDown(self):
        fleet.backend_bin = self._orig

    def test_kind_and_complexity_select_expected_tier(self):
        fleet.backend_bin = with_backends("claude", "codex")
        for kind, complexity, tier in [
            ("boilerplate", "trivial", "light"),
            ("implement", "high", "heavy"),
            ("debug", "extreme", "orchestrator"),
            ("plan", "high", "orchestrator"),
        ]:
            _, got = fleet.route(ROSTER, kind, complexity)
            self.assertEqual(got, tier, "%s/%s" % (kind, complexity))

    def test_prefer_backend_is_honoured(self):
        fleet.backend_bin = with_backends("claude", "codex")
        claude_pick, _ = fleet.route(ROSTER, "implement", "high", prefer="claude")
        codex_pick, _ = fleet.route(ROSTER, "implement", "high", prefer="codex")
        self.assertEqual(ROSTER["models"][claude_pick]["backend"], "claude")
        self.assertEqual(ROSTER["models"][codex_pick]["backend"], "codex")
        self.assertNotEqual(claude_pick, codex_pick)

    def test_avoid_backend_crosses_families(self):
        fleet.backend_bin = with_backends("claude", "codex")
        pick, _ = fleet.route(ROSTER, "review", "high", prefer="claude", avoid_backend="claude")
        self.assertEqual(ROSTER["models"][pick]["backend"], "codex")

    def test_falls_back_when_preferred_backend_absent(self):
        """Asking for claude with only codex installed must still return a codex model."""
        fleet.backend_bin = with_backends("codex")
        pick, _ = fleet.route(ROSTER, "implement", "high", prefer="claude")
        self.assertEqual(ROSTER["models"][pick]["backend"], "codex")

    def test_unknown_kind_degrades_to_implement(self):
        fleet.backend_bin = with_backends("claude", "codex")
        a, _ = fleet.route(ROSTER, "no-such-kind", "high")
        b, _ = fleet.route(ROSTER, "implement", "high")
        self.assertEqual(a, b)

    def test_no_backends_still_answers_policy_questions(self):
        """`fleet route` must work before anything is installed; dispatch still fails."""
        fleet.backend_bin = with_backends()
        pick, tier = fleet.route(ROSTER, "implement", "high")
        self.assertEqual(tier, "heavy")
        self.assertIn(pick, ROSTER["models"])
        cmd, err = fleet.build_cmd(ROSTER, ROSTER["models"][pick], "/tmp", "high",
                                   False, False, "/tmp/o")
        self.assertIsNone(cmd)
        self.assertIn("not installed", err)


class Waves(unittest.TestCase):
    def test_independent_tasks_share_one_wave(self):
        waves = fleet.plan_waves([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        self.assertEqual(len(waves), 1)
        self.assertEqual(len(waves[0]), 3)

    def test_deps_create_ordered_waves(self):
        waves = fleet.plan_waves([
            {"id": "a"}, {"id": "b"},
            {"id": "c", "deps": ["a", "b"]},
            {"id": "d", "deps": ["c"]},
        ])
        self.assertEqual([sorted(t["id"] for t in w) for w in waves], [["a", "b"], ["c"], ["d"]])

    def test_cycle_exits(self):
        with self.assertRaises(SystemExit):
            fleet.plan_waves([{"id": "a", "deps": ["b"]}, {"id": "b", "deps": ["a"]}])

    def test_unknown_dep_exits(self):
        with self.assertRaises(SystemExit):
            fleet.plan_waves([{"id": "a", "deps": ["ghost"]}])


class TaskIdValidation(unittest.TestCase):
    def test_accepts_safe_ids(self):
        fleet.validate_task_ids([
            {"id": "survey"},
            {"id": "review-2.final"},
            {"id": "task_003"},
        ])

    def test_rejects_path_traversal_and_separators(self):
        for tid in ("../escaped", "a/b", r"a\b", ".", "..", "/absolute"):
            with self.subTest(tid=tid), self.assertRaises(ValueError):
                fleet.validate_task_ids([{"id": tid}])

    def test_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate task id"):
            fleet.validate_task_ids([{"id": "same"}, {"id": "same"}])

    def test_rejects_non_string_and_oversized_ids(self):
        for tid in (None, 1, "x" * 129):
            with self.subTest(tid=tid), self.assertRaises(ValueError):
                fleet.validate_task_ids([{"id": tid}])

    def test_artifact_path_stays_in_run_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = fleet.task_artifact_path(td, "safe-id", ".json")
            self.assertEqual(path.parent, Path(td).resolve())

    def test_run_task_rejects_traversal_without_writing_or_raising(self):
        """A bad id yields the usual failed-result shape, never a traceback."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run"
            run_dir.mkdir()
            res = fleet.run_task(
                ROSTER,
                {"id": "../escaped", "prompt": "hi", "model": "spark"},
                run_dir, td, 10, False, "auto",
            )
            self.assertFalse(res["ok"])
            self.assertIn("invalid task id", res["stderr"])
            for key in ("id", "ok", "model", "backend", "tier", "seconds", "output"):
                self.assertIn(key, res, "missing %r - callers will KeyError" % key)
            self.assertFalse((root / "escaped.json").exists())
            self.assertEqual(list(run_dir.iterdir()), [], "no artifact for an untrusted id")

    def test_rejects_ids_differing_only_in_case(self):
        """Case-insensitive filesystems collapse these to one artifact file."""
        with self.assertRaisesRegex(ValueError, "duplicate task id"):
            fleet.validate_task_ids([{"id": "Survey"}, {"id": "survey"}])


class RunDirectoryContainment(unittest.TestCase):
    """The plan's `label` is untrusted and names a directory."""

    def setUp(self):
        self._home = fleet.FLEET_HOME

    def tearDown(self):
        fleet.FLEET_HOME = self._home

    def test_hostile_label_cannot_escape(self):
        import tempfile
        for label in ("../../escaped", "../..", "/abs/path", "a/b", "..", "."):
            with tempfile.TemporaryDirectory() as td:
                fleet.FLEET_HOME = Path(td)
                with self.subTest(label=label):
                    d = fleet.new_run_dir(label)
                    self.assertEqual(d.parent, (Path(td) / "runs").resolve(),
                                     "run dir escaped for label %r" % label)
                    self.assertTrue(d.is_dir())

    def test_label_is_scrubbed_not_rejected(self):
        self.assertEqual(fleet.sanitize_label("my batch/v2"), "my-batch-v2")
        self.assertEqual(fleet.sanitize_label(".."), "run")
        self.assertEqual(fleet.sanitize_label(""), "run")
        self.assertEqual(fleet.sanitize_label(None), "None")
        self.assertLessEqual(len(fleet.sanitize_label("x" * 500)), 48)

    def test_ordinary_label_survives(self):
        self.assertEqual(fleet.sanitize_label("ratelimit"), "ratelimit")


class PlanValidation(unittest.TestCase):
    """Malformed plans must produce messages, not tracebacks."""

    def good(self):
        return {"tasks": [{"prompt": "do a thing"}]}

    def test_accepts_a_minimal_plan_and_fills_ids(self):
        tasks = fleet.validate_plan(self.good())
        self.assertEqual(tasks[0]["id"], "t1")

    def test_rejects_non_object_plan(self):
        for plan in ([], "text", 3, None):
            with self.subTest(plan=plan), self.assertRaisesRegex(ValueError, "JSON object"):
                fleet.validate_plan(plan)

    def test_rejects_missing_or_empty_tasks(self):
        for plan in ({}, {"goal": "x"}, {"tasks": []}, {"tasks": "nope"}):
            with self.subTest(plan=plan), self.assertRaisesRegex(ValueError, "tasks"):
                fleet.validate_plan(plan)

    def test_rejects_non_object_task(self):
        with self.assertRaisesRegex(ValueError, "must be an object"):
            fleet.validate_plan({"tasks": ["just a string"]})

    def test_rejects_missing_or_blank_prompt(self):
        for task in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 5}):
            with self.subTest(task=task), self.assertRaisesRegex(ValueError, "prompt"):
                fleet.validate_plan({"tasks": [task]})

    def test_propagates_duplicate_id_errors(self):
        with self.assertRaisesRegex(ValueError, "duplicate task id"):
            fleet.validate_plan({"tasks": [{"id": "a", "prompt": "x"},
                                           {"id": "a", "prompt": "y"}]})


class CommandBuilding(unittest.TestCase):
    def setUp(self):
        self._orig = fleet.backend_bin
        fleet.backend_bin = with_backends("claude", "codex")

    def tearDown(self):
        fleet.backend_bin = self._orig

    def build(self, key, **kw):
        opts = {"cwd": "/tmp/work", "effort": "medium", "readonly": False,
                "yolo": False, "outfile": "/tmp/out.txt"}
        opts.update(kw)
        cmd, err = fleet.build_cmd(ROSTER, ROSTER["models"][key], **opts)
        self.assertIsNone(err)
        return cmd

    def test_claude_defaults_to_sandboxed_edits(self):
        cmd = self.build("sonnet")
        self.assertIn("-p", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertIn("/tmp/work", cmd)

    def test_claude_readonly_blocks_write_tools(self):
        cmd = self.build("sonnet", readonly=True)
        joined = " ".join(cmd)
        for tool in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(tool, joined)
        self.assertNotIn("acceptEdits", cmd)

    def test_codex_defaults_to_workspace_write(self):
        cmd = self.build("terra")
        self.assertIn("exec", cmd)
        self.assertIn("workspace-write", cmd)
        self.assertIn("--skip-git-repo-check", cmd)

    def test_codex_readonly_uses_readonly_sandbox(self):
        cmd = self.build("terra", readonly=True)
        self.assertIn("read-only", cmd)
        self.assertNotIn("workspace-write", cmd)

    def test_yolo_is_opt_in_only(self):
        self.assertIn("--dangerously-skip-permissions", self.build("sonnet", yolo=True))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.build("terra", yolo=True))

    def test_readonly_beats_yolo(self):
        """A readonly task must stay readonly even if --yolo was passed globally."""
        self.assertNotIn("--dangerously-skip-permissions", self.build("sonnet", readonly=True, yolo=True))
        self.assertIn("read-only", self.build("terra", readonly=True, yolo=True))

    def test_effort_is_passed_through(self):
        self.assertIn("--effort", self.build("sonnet", effort="high"))
        self.assertIn("model_reasoning_effort=high", " ".join(self.build("terra", effort="high")))


class MissingBackendIsGraceful(unittest.TestCase):
    """A first run before installing a backend must not traceback."""

    def setUp(self):
        self._orig = fleet.backend_bin
        fleet.backend_bin = with_backends()

    def tearDown(self):
        fleet.backend_bin = self._orig

    def test_result_is_fully_shaped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            res = fleet.run_task(ROSTER, {"id": "t1", "prompt": "hi", "kind": "triage",
                                          "complexity": "low"}, td, td, 10, False, "auto")
        for key in ("id", "ok", "model", "model_id", "backend", "tier", "cwd",
                    "seconds", "exit_code", "timed_out", "output"):
            self.assertIn(key, res, "missing %r - callers will KeyError" % key)
        self.assertFalse(res["ok"])
        self.assertIn("not installed", res["stderr"])


class Sessions(unittest.TestCase):
    """Named sessions keep continuity across separate CLI processes."""

    def setUp(self):
        self._orig = fleet.backend_bin
        fleet.backend_bin = with_backends("claude", "codex")

    def tearDown(self):
        fleet.backend_bin = self._orig

    def build(self, key, session, **kw):
        opts = {"cwd": "/tmp/work", "effort": "medium", "readonly": False,
                "yolo": False, "outfile": "/tmp/out.txt", "session": session}
        opts.update(kw)
        cmd, err = fleet.build_cmd(ROSTER, ROSTER["models"][key], **opts)
        self.assertIsNone(err)
        return cmd

    def test_no_session_adds_no_session_flags(self):
        cmd = self.build("sonnet", None)
        self.assertNotIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("resume", self.build("terra", None))

    def test_claude_new_session_pins_a_uuid(self):
        import uuid as _uuid
        sess = {}
        cmd = self.build("sonnet", sess)
        self.assertIn("--session-id", cmd)
        native = cmd[cmd.index("--session-id") + 1]
        _uuid.UUID(native)  # must be a valid UUID or claude rejects it
        self.assertEqual(sess["native_id"], native, "id must be recorded for later resume")

    def test_claude_resume_uses_existing_id(self):
        cmd = self.build("sonnet", {"native_id": "abc-123"})
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc-123")
        self.assertNotIn("--session-id", cmd)

    def test_codex_resume_uses_resume_subcommand(self):
        cmd = self.build("terra", {"native_id": "thread-9"})
        self.assertEqual(cmd[1:4], ["exec", "resume", "thread-9"])

    def test_codex_resume_avoids_unsupported_flags(self):
        """`codex exec resume` rejects -s and -C, so they must not appear."""
        cmd = self.build("terra", {"native_id": "thread-9"})
        self.assertNotIn("-s", cmd)
        self.assertNotIn("-C", cmd)

    def test_codex_resume_preserves_sandbox_via_config(self):
        ro = self.build("terra", {"native_id": "t"}, readonly=True)
        self.assertIn('sandbox_mode="read-only"', ro)
        rw = self.build("terra", {"native_id": "t"}, readonly=False)
        self.assertIn('sandbox_mode="workspace-write"', rw)

    def test_codex_resume_readonly_still_beats_yolo(self):
        cmd = self.build("terra", {"native_id": "t"}, readonly=True, yolo=True)
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_thread_id_parsed_from_event_stream(self):
        stream = "\n".join([
            "not json",
            json.dumps({"type": "thread.started", "thread_id": "01a0-abc"}),
            json.dumps({"type": "turn.started"}),
        ])
        self.assertEqual(fleet.parse_codex_thread_id(stream), "01a0-abc")

    def test_thread_id_absent_is_none(self):
        self.assertIsNone(fleet.parse_codex_thread_id("garbage\n{}"))

    def test_text_recovered_from_event_stream(self):
        stream = "\n".join([
            json.dumps({"type": "item.completed",
                        "item": {"item_type": "assistant_message", "text": "first"}}),
            json.dumps({"type": "item.completed",
                        "item": {"item_type": "assistant_message", "text": "last"}}),
        ])
        self.assertEqual(fleet.codex_text_from_events(stream), "last")
        self.assertEqual(fleet.extract_text("codex", stream, "/nonexistent"), "last")

    def test_store_round_trips(self):
        import tempfile
        orig = fleet.FLEET_HOME
        try:
            with tempfile.TemporaryDirectory() as td:
                fleet.FLEET_HOME = Path(td)
                self.assertEqual(fleet.load_sessions(), {})
                fleet.save_sessions({"work": {"model": "sonnet", "native_id": "x", "turns": 1}})
                self.assertEqual(fleet.load_sessions()["work"]["native_id"], "x")
        finally:
            fleet.FLEET_HOME = orig

    def test_corrupt_store_does_not_crash(self):
        import tempfile
        orig = fleet.FLEET_HOME
        try:
            with tempfile.TemporaryDirectory() as td:
                fleet.FLEET_HOME = Path(td)
                (Path(td) / "sessions.json").write_text("{not json")
                self.assertEqual(fleet.load_sessions(), {})
        finally:
            fleet.FLEET_HOME = orig


class OutputParsing(unittest.TestCase):
    def test_claude_json_result_is_extracted(self):
        blob = json.dumps({"result": "the answer", "total_cost_usd": 0.01})
        self.assertEqual(fleet.extract_text("claude", blob, "/nonexistent"), "the answer")

    def test_claude_non_json_falls_back_to_raw(self):
        self.assertEqual(fleet.extract_text("claude", "plain text", "/nonexistent"), "plain text")

    def test_claude_meta_survives_garbage(self):
        self.assertEqual(fleet.claude_meta("not json"), {})




class SessionDiscovery(unittest.TestCase):
    """Picking up an existing project conversation from either CLI."""

    def test_path_normalisation_matches_claude_dir_encoding(self):
        # Claude Code maps every non-alphanumeric char to '-', so '/' and '_'
        # collide; the encoding is lossy and must be verified, not decoded.
        self.assertEqual(fleet._norm_path("/Users/a/dev/continual_learning"),
                         "-Users-a-dev-continual-learning")
        self.assertEqual(fleet._norm_path("/a/b.c"), "-a-b-c")

    def test_claude_transcript_digest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.jsonl"
            f.write_text("\n".join([
                json.dumps({"type": "user",
                            "message": {"content": [{"type": "text", "text": "add retries"}]}}),
                json.dumps({"type": "assistant",
                            "message": {"content": [{"type": "text", "text": "done"}]}}),
                json.dumps({"type": "system", "message": {"content": "ignored"}}),
            ]))
            out = fleet.transcript_digest({"backend": "claude", "path": str(f)})
            self.assertIn("USER: add retries", out)
            self.assertIn("ASSISTANT: done", out)
            self.assertNotIn("ignored", out)

    def test_codex_transcript_digest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "r.jsonl"
            f.write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {"id": "t1", "cwd": td}}),
                json.dumps({"type": "response_item",
                            "payload": {"type": "message", "role": "user",
                                        "content": [{"text": "ship it"}]}}),
            ]))
            out = fleet.transcript_digest({"backend": "codex", "path": str(f)})
            self.assertIn("USER: ship it", out)

    def test_harness_scaffolding_is_stripped(self):
        """Plugin lists and context blocks would otherwise eat the handoff budget."""
        for junk in ("<recommended_plugins>\nAirtable", "<app-context>\nhi",
                     "Caveat: the messages below were generated by",
                     "This session is being continued from a previous conversation"):
            self.assertTrue(fleet._is_scaffolding(junk), junk[:30])
        self.assertFalse(fleet._is_scaffolding("Please add rate limiting"))

    def test_digest_is_budget_capped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.jsonl"
            f.write_text("\n".join(
                json.dumps({"type": "user", "message": {"content": "x" * 500}})
                for _ in range(50)))
            out = fleet.transcript_digest({"backend": "claude", "path": str(f)}, max_chars=2000)
            self.assertLessEqual(len(out), 2600)
            self.assertIn("omitted", out)


class Receipts(unittest.TestCase):
    """The notebook receipt is the evidence a task actually happened."""

    def summary(self):
        return {"goal": "add a guard", "cwd": "/tmp/x", "run_dir": "/tmp/x/run-1",
                "results": [{"id": "impl", "ok": True, "model": "sonnet", "model_id": "sonnet",
                             "backend": "claude", "tier": "mid", "kind": "implement",
                             "complexity": "high", "readonly": False, "seconds": 3.2,
                             "exit_code": 0, "output": "done", "prompt": "do the thing"}]}

    def test_is_valid_nbformat_4(self):
        nb = fleet.build_receipt(self.summary())
        self.assertEqual(nb["nbformat"], 4)
        self.assertGreaterEqual(nb["nbformat_minor"], 5)
        for cell in nb["cells"]:
            self.assertIn("id", cell, "nbformat >=4.5 requires a cell id")
            self.assertIsInstance(cell["source"], list)
            self.assertIn(cell["cell_type"], ("markdown", "code"))
            if cell["cell_type"] == "code":
                self.assertIn("outputs", cell)
                self.assertIn("execution_count", cell)

    def test_cell_ids_are_unique(self):
        nb = fleet.build_receipt(self.summary())
        ids = [c["id"] for c in nb["cells"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_records_prompt_response_and_outcome(self):
        text = json.dumps(fleet.build_receipt(self.summary()))
        for expected in ("add a guard", "do the thing", "done", "sonnet", "Verification"):
            self.assertIn(expected, text)

    def test_failed_task_is_not_reported_as_success(self):
        s = self.summary()
        s["results"][0].update(ok=False, exit_code=2, output="", stderr="boom")
        text = json.dumps(fleet.build_receipt(s))
        self.assertIn("FAILED", text)
        self.assertIn("0 of 1", text)

    def test_fences_in_output_cannot_break_the_notebook(self):
        s = self.summary()
        s["results"][0]["output"] = "```python\nprint(1)\n```"
        nb = fleet.build_receipt(s)
        json.loads(json.dumps(nb))  # must stay serialisable
        self.assertTrue(any("``" in "".join(c["source"]) for c in nb["cells"]))


class TaskGates(unittest.TestCase):
    """branch -> receipt -> review -> understanding, each independently checked."""

    def setUp(self):
        import tempfile
        self.td = tempfile.mkdtemp()
        self.root = Path(self.td)
        (self.root / fleet.TASK_ROOT / "t").mkdir(parents=True)
        self._save({"slug": "t", "branch": "fleet/t", "base": "main"})

    def _save(self, meta):
        (self.root / fleet.TASK_ROOT / "t" / "task.json").write_text(json.dumps(meta))

    def gates(self):
        return {g["gate"]: g["ok"] for g in fleet.task_gates(self.root, "t")}

    def test_everything_starts_closed(self):
        g = self.gates()
        for gate in ("receipt", "review", "understanding"):
            self.assertFalse(g[gate], gate)

    def test_receipt_gate_needs_a_real_notebook(self):
        d = self.root / fleet.TASK_ROOT / "t"
        (d / "receipt.ipynb").write_text("not json")
        self.assertFalse(self.gates()["receipt"])
        (d / "receipt.ipynb").write_text(json.dumps(
            fleet.build_receipt({"results": [], "run_dir": "r"})))
        self.assertTrue(self.gates()["receipt"])

    def test_review_gate_opens_only_on_pass(self):
        self._save({"slug": "t", "branch": "fleet/t", "review": {"verdict": "changes"}})
        self.assertFalse(self.gates()["review"])
        self._save({"slug": "t", "branch": "fleet/t", "review": {"verdict": "pass", "by": "fable"}})
        self.assertTrue(self.gates()["review"])

    def test_understanding_gate_rejects_the_unfilled_template(self):
        d = self.root / fleet.TASK_ROOT / "t"
        (d / "understanding.md").write_text(fleet.UNDERSTANDING_TEMPLATE)
        self.assertFalse(self.gates()["understanding"],
                         "an untouched template must not count as understanding")
        (d / "understanding.md").write_text(
            fleet.UNDERSTANDING_TEMPLATE + "\n" + ("We changed how errors surface. " * 12))
        self.assertTrue(self.gates()["understanding"])


class ReviewRegressions(unittest.TestCase):
    """Each of these pins a defect found in review of the original change."""

    def test_git_errors_are_not_swallowed(self):
        """git reports failures on stderr; returning stdout alone lost them."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out, rc = fleet._git(td, "rev-parse", "--abbrev-ref", "HEAD")
            self.assertNotEqual(rc, 0)
            self.assertTrue(out.strip(), "an error with no message is unactionable")

    def test_fence_never_edits_the_content_it_records(self):
        """A receipt is evidence, so no invisible characters may be injected."""
        body = "text\n```python\nx = 1\n```\nmore"
        out = fleet._fence(body)
        self.assertNotIn("​", out, "zero-width space injected into recorded output")
        self.assertIn("```python\nx = 1\n```", out, "original content must survive verbatim")
        self.assertTrue(out.startswith("````"), "fence must outgrow the content's backticks")

    def test_fence_handles_longer_backtick_runs(self):
        out = fleet._fence("a ````` b")
        self.assertTrue(out.startswith("`" * 6))
        self.assertIn("`````", out)

    def test_understanding_gate_ignores_comment_blocks(self):
        """The template's guidance comment must not count as the author's prose."""
        stripped = re.sub(r"<!--.*?-->", "", fleet.UNDERSTANDING_TEMPLATE, flags=re.S)
        body = "\n".join(l for l in stripped.splitlines()
                         if l.strip() and not l.strip().startswith(("#", "_")))
        self.assertEqual(len(body), 0,
                         "an untouched template must contribute nothing toward the gate")

    def test_turn_counting_is_the_same_on_both_backends(self):
        """Claude files tool results under the `user` type; those are not turns."""
        claude_tool_result = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "output"}]}}
        self.assertIsNone(fleet._message_text(claude_tool_result, "claude"))
        claude_real = {"type": "user", "message": {"content": [
            {"type": "text", "text": "hello"}]}}
        self.assertEqual(fleet._message_text(claude_real, "claude"), ("user", "hello"))
        codex_real = {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"text": "hello"}]}}
        self.assertEqual(fleet._message_text(codex_real, "codex"), ("user", "hello"))

    def test_empty_messages_are_not_counted(self):
        blank = {"type": "assistant", "message": {"content": [{"type": "text", "text": "  "}]}}
        self.assertIsNone(fleet._message_text(blank, "claude"))

    def test_scan_reports_turns_title_and_opening_ask_in_one_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.jsonl"
            f.write_text("\n".join(
                [json.dumps({"type": "ai-title", "aiTitle": "Fix the parser"}),
                 json.dumps({"type": "user", "cwd": td,
                             "message": {"content": [{"type": "text", "text": "<app-context>x"}]}}),
                 json.dumps({"type": "user",
                             "message": {"content": [{"type": "text", "text": "fix the parser"}]}}),
                 json.dumps({"type": "assistant",
                             "message": {"content": [{"type": "text", "text": "ok"}]}})]))
            info = fleet._scan_transcript(f, "claude")
            self.assertEqual(info["title"], "Fix the parser")
            self.assertEqual(info["cwd"], td)
            self.assertEqual(info["turns"], 3)
            self.assertEqual(info["first_ask"], "fix the parser",
                             "scaffolding must not be mistaken for the opening ask")

    def test_transcripts_are_read_as_utf8(self):
        """Both CLIs write UTF-8; locale-dependent decoding corrupts it silently."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.jsonl"
            f.write_bytes(json.dumps(
                {"type": "user", "message": {"content": [{"type": "text",
                                                          "text": "café — naïve"}]}}
            ).encode("utf-8") + b"\n")
            out = fleet.transcript_digest({"backend": "claude", "path": str(f)})
            self.assertIn("café — naïve", out)

    def test_runs_are_scoped_to_the_repository(self):
        """A run from another project must not be able to satisfy a task gate."""
        import tempfile
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            orig = fleet.FLEET_HOME
            try:
                fleet.FLEET_HOME = Path(home)
                runs = Path(home) / "runs"
                (runs / "a-inside").mkdir(parents=True)
                (runs / "a-inside" / "run.json").write_text(
                    json.dumps({"cwd": str(Path(repo) / "sub"), "results": []}))
                (runs / "b-outside").mkdir(parents=True)
                (runs / "b-outside" / "run.json").write_text(
                    json.dumps({"cwd": tempfile.gettempdir(), "results": []}))
                names = [r.name for r, _ in fleet._runs_under(repo)]
                self.assertIn("a-inside", names)
                self.assertNotIn("b-outside", names,
                                 "a run from another project leaked into this repo's evidence")
            finally:
                fleet.FLEET_HOME = orig



class LongPathDiscovery(unittest.TestCase):
    """Claude Code truncates long cwd encodings and appends a hash."""

    def setUp(self):
        self._orig = fleet.CLAUDE_PROJECTS

    def tearDown(self):
        fleet.CLAUDE_PROJECTS = self._orig

    def _project(self, root, dirname, cwd):
        d = Path(root) / dirname
        d.mkdir(parents=True)
        (d / "sess.jsonl").write_text(json.dumps(
            {"type": "user", "cwd": cwd,
             "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")
        return d

    def test_finds_truncated_and_hashed_directory(self):
        import tempfile
        cwd = "/" + "/".join("segment%02d" % i for i in range(40))  # well over 200 chars
        full = fleet._norm_path(cwd)
        self.assertGreater(len(full), fleet.CLAUDE_DIR_TRUNCATE)
        with tempfile.TemporaryDirectory() as td:
            fleet.CLAUDE_PROJECTS = Path(td)
            self._project(td, full[:fleet.CLAUDE_DIR_TRUNCATE] + "-ab12cd", cwd)
            dirs = fleet._claude_project_dirs(cwd)
            self.assertEqual(len(dirs), 1, "truncated directory was not matched")

    def test_exact_match_still_preferred(self):
        import tempfile
        cwd = "/tmp/short"
        with tempfile.TemporaryDirectory() as td:
            fleet.CLAUDE_PROJECTS = Path(td)
            self._project(td, fleet._norm_path(cwd), cwd)
            self.assertEqual(len(fleet._claude_project_dirs(cwd)), 1)

    def test_falls_back_to_the_recorded_cwd(self):
        """If the encoding changes again, the transcripts still identify themselves."""
        import tempfile
        cwd = "/tmp/whatever"
        with tempfile.TemporaryDirectory() as td:
            fleet.CLAUDE_PROJECTS = Path(td)
            self._project(td, "an-entirely-unrelated-name", cwd)
            dirs = fleet._claude_project_dirs(cwd)
            self.assertEqual([d.name for d in dirs], ["an-entirely-unrelated-name"])

    def test_unrelated_directories_are_not_matched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fleet.CLAUDE_PROJECTS = Path(td)
            self._project(td, "-tmp-other", "/tmp/other")
            self.assertEqual(fleet._claude_project_dirs("/tmp/mine"), [])


class ResumeHints(unittest.TestCase):
    """Delegations are headless, but they leave openable sessions behind."""

    def test_per_backend_commands(self):
        self.assertEqual(fleet.resume_hint("claude", "abc"), "claude --resume abc")
        self.assertEqual(fleet.resume_hint("codex", "xyz"), "codex resume xyz")

    def test_no_id_means_no_hint(self):
        self.assertIsNone(fleet.resume_hint("claude", None))
        self.assertIsNone(fleet.resume_hint("codex", ""))

    def test_receipt_exposes_the_open_command(self):
        nb = fleet.build_receipt({"goal": "g", "run_dir": "r", "results": [
            {"id": "t1", "ok": True, "model": "sonnet", "backend": "claude",
             "resume_with": "claude --resume abc", "seconds": 1}]})
        self.assertIn("claude --resume abc", json.dumps(nb))


class DecisionsCarry(unittest.TestCase):
    """Workers get the orchestrator's reasoning, not only its conclusions."""

    def test_context_and_decisions_are_both_rendered(self):
        out = fleet.decisions_block({"context": "why we are here",
                                     "decisions": ["use tabs", "no new deps"]})
        self.assertIn("why we are here", out)
        self.assertIn("- use tabs", out)
        self.assertIn("- no new deps", out)
        self.assertTrue(out.rstrip().endswith("# Your task"),
                        "the task must come last, after the context")

    def test_a_single_string_is_accepted(self):
        self.assertIn("- only one thing", fleet.decisions_block({"decisions": "only one thing"}))

    def test_empty_plan_adds_nothing(self):
        for plan in ({}, {"decisions": []}, {"context": "  "}, {"decisions": ["", "  "]}):
            self.assertEqual(fleet.decisions_block(plan), "", repr(plan))

    def test_decisions_are_marked_as_settled(self):
        out = fleet.decisions_block({"decisions": ["x"]})
        self.assertIn("settled", out.lower())
        self.assertIn("stop and say so", out.lower(),
                      "a delegate must escalate rather than silently deviate")

    def test_malformed_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "decisions"):
            fleet.validate_plan({"tasks": [{"prompt": "x"}], "decisions": {"a": 1}})
        with self.assertRaisesRegex(ValueError, "context"):
            fleet.validate_plan({"tasks": [{"prompt": "x"}], "context": ["not a string"]})

    def test_well_formed_fields_pass(self):
        fleet.validate_plan({"tasks": [{"prompt": "x"}],
                             "decisions": ["a"], "context": "b"})

    def test_receipt_records_what_was_carried(self):
        nb = fleet.build_receipt({"goal": "g", "run_dir": "r", "results": [],
                                  "context": "the background",
                                  "decisions": ["the constraint"]})
        text = json.dumps(nb)
        self.assertIn("Decisions carried into this work", text)
        self.assertIn("the background", text)
        self.assertIn("the constraint", text)



class ForegroundQueue(unittest.TestCase):
    """An orchestrator enqueues; one attached session drains, exactly once."""

    def setUp(self):
        import tempfile
        self._orig = fleet.FLEET_HOME
        self._td = tempfile.mkdtemp()
        fleet.FLEET_HOME = Path(self._td)
        # cmd_enqueue resolves the cwd, and on macOS a temp dir resolves
        # through /private - so the test must compare the resolved form.
        self.cwd = str((Path(self._td) / "proj").resolve())
        os.makedirs(self.cwd, exist_ok=True)
        self.cwd = str(Path(self.cwd).resolve())

    def tearDown(self):
        fleet.FLEET_HOME = self._orig

    def _add(self, prompt="do a thing", **kw):
        import contextlib, io
        args = argparse.Namespace(prompt=prompt, cwd=self.cwd, key=kw.get("key"),
                                  kind="implement", complexity="medium",
                                  priority=kw.get("priority", 0), label=kw.get("label"))
        with contextlib.redirect_stdout(io.StringIO()):
            fleet.cmd_enqueue(args)
        return fleet.all_queue_tasks(cwd=self.cwd)

    def test_enqueue_then_claim(self):
        self._add()
        pending = fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))
        self.assertEqual(len(pending), 1)
        got = fleet._claim(pending[0], "sessionA", 600)
        self.assertIsNotNone(got)
        self.assertEqual(got["state"], "claimed")
        self.assertEqual(got["attempts"], 1)

    def test_a_task_cannot_be_claimed_twice(self):
        self._add()
        t = fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))[0]
        self.assertIsNotNone(fleet._claim(t, "A", 600))
        self.assertIsNone(fleet._claim(t, "B", 600), "two sessions took the same task")

    def test_identical_work_is_not_queued_twice(self):
        self._add("same work")
        self._add("same work")
        self.assertEqual(len(fleet.all_queue_tasks(cwd=self.cwd)), 1)

    def test_an_explicit_key_controls_identity(self):
        self._add("wording one", key="shared")
        self._add("wording two", key="shared")
        self.assertEqual(len(fleet.all_queue_tasks(cwd=self.cwd)), 1)

    def test_higher_priority_is_offered_first(self):
        self._add("low", label="low")
        self._add("high", label="high", priority=5)
        self.assertEqual(fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))[0]["label"], "high")

    def test_in_flight_work_survives_a_reap(self):
        """The claiming process exits at once; that must not read as a crash."""
        self._add()
        t = fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))[0]
        fleet._claim(t, "A", 600)
        self.assertEqual(fleet.reap_stale(cwd=self.cwd), [],
                         "reclaimed work that was still being done - it would run twice")

    def test_an_expired_lease_returns_to_the_queue(self):
        self._add()
        t = fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))[0]
        fleet._claim(t, "A", 0)
        self.assertEqual(len(fleet.reap_stale(cwd=self.cwd)), 1)
        self.assertEqual(fleet.all_queue_tasks(cwd=self.cwd)[0]["state"], "pending")

    def test_a_dead_declared_owner_releases_immediately(self):
        self._add()
        t = fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))[0]
        fleet._claim(t, "A", 9999, owner_pid=999999)  # a pid that cannot exist
        self.assertEqual(len(fleet.reap_stale(cwd=self.cwd)), 1)

    def test_a_reclaimed_task_records_why(self):
        self._add()
        t = fleet.all_queue_tasks(cwd=self.cwd, states=("pending",))[0]
        fleet._claim(t, "A", 0)
        fleet.reap_stale(cwd=self.cwd)
        hist = fleet.all_queue_tasks(cwd=self.cwd)[0]["history"]
        self.assertEqual(hist[-1]["event"], "reclaimed")
        self.assertIn("expired", hist[-1]["why"])

    def test_tasks_are_scoped_per_project(self):
        self._add()
        other = str(Path(self._td) / "elsewhere")
        os.makedirs(other, exist_ok=True)
        self.assertEqual(fleet.all_queue_tasks(cwd=other), [],
                         "another project's work leaked into this queue")

    def test_worker_slot_is_held_by_reattaching(self):
        fresh = {"session": "A", "attached_at": fleet._iso(), "poll_seconds": 300}
        self.assertTrue(fleet._worker_is_live(fresh))
        stale = {"session": "A", "attached_at": "2020-01-01T00:00:00+00:00", "poll_seconds": 300}
        self.assertFalse(fleet._worker_is_live(stale), "a stale slot must be takeable")

    def test_a_declared_dead_owner_frees_the_slot_at_once(self):
        rec = {"session": "A", "attached_at": fleet._iso(),
               "poll_seconds": 300, "owner_pid": 999999}
        self.assertFalse(fleet._worker_is_live(rec))

    def test_a_slot_with_no_timestamp_is_not_trusted(self):
        self.assertFalse(fleet._worker_is_live({"session": "A"}))



if __name__ == "__main__":
    unittest.main(verbosity=2)
