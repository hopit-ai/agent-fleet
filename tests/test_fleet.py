"""Unit tests for fleet. No network, no auth, no backends required."""

import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
