"""File workbench simulation tests. Fake judgments are not real model acceptance."""

import copy
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from harness_support import bootstrap
RUN_DIR = bootstrap(__file__)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.workbench.workspace import Workspace, Conflict, load_json, safe_path, digest, verify_delivery
from workbench_fixtures import FakeRunner, SETTINGS, TestJobs as Jobs, TestOnlyRuntimeBlocked as RuntimeBlocked

def loader(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestWorkbench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        builder = loader(ROOT / "tools/skill-packager/build.py", "_test_wb_build")
        cls.skills = RUN_DIR / "installed"
        builder.build(cls.skills)
        cls.common = loader(cls.skills / "content-engine-review/scripts/common.py", "_test_wb_common")

    def setUp(self):
        self.ws = Workspace(RUN_DIR / digest(self._testMethodName)[:12])
        self.ws.initialize(copy.deepcopy(SETTINGS))
        self.runner = FakeRunner(self.common)
        self.jobs = Jobs(self.ws, self.skills, self.runner)

    def topic(self):
        value = self.ws.add_input({"theme": "测试主题"})
        self.assertTrue(self.jobs.research(value["id"])["ok"])
        return self.ws.topics()[0]

    def article(self, platform="xhs"):
        topic = self.topic()
        return self.ws.add_content("2026-11-11", topic["id"], platform)

    def test_read_missing_does_not_initialize(self):
        ws = Workspace(RUN_DIR / "readonly-missing")
        self.assertEqual(ws.plan("2026-11-11")["items"], [])
        self.assertFalse(ws.root.exists())

    def test_input_accepts_fuzzy_theme(self):
        self.assertEqual(self.ws.add_input({"theme": "未来产品"})["status"], "queued")

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            self.ws.add_input({})

    def test_relative_path_escape_rejected(self):
        for path in ("../secret", "C:/secret", "/secret", "a\\b", "a//b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(self.ws.root, path)

    def test_duplicate_json_rejected(self):
        path = self.ws.path("bad.json")
        path.write_text('{"a":1,"a":2}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_json(path)

    def test_claim_exclusive(self):
        with self.ws.claim("test"):
            with self.assertRaises(Conflict):
                with self.ws.claim("test"):
                    pass

    def test_cleanup_failure_keeps_claim(self):
        with self.assertRaises(RuntimeBlocked):
            with self.ws.claim("test"):
                raise RuntimeBlocked("process_cleanup_unconfirmed")
        self.assertTrue(self.ws.path(".local/claims/test.json").exists())

    def test_config_cannot_enable_publish(self):
        current = self.ws.settings()
        with self.assertRaises(ValueError):
            self.ws.save_settings({**current, "publish_enabled": True}, current["revision"])

    def test_stale_settings_revision(self):
        current = self.ws.settings()
        self.ws.save_settings(current, current["revision"])
        with self.assertRaises(Conflict):
            self.ws.save_settings(current, current["revision"])

    def test_research_ready_requires_source_text(self):
        self.assertEqual(self.topic()["status"], "ready")

    def test_research_failure_does_not_repeat_every_tick(self):
        value = self.ws.add_input({"theme": "test"})
        self.runner.failure = "research"
        self.assertFalse(self.jobs.tick()["ok"])
        self.assertEqual(self.jobs.tick()["state"], "idle")
        self.assertEqual(load_json(self.ws.path(f"library/inbox/{value['id']}/input.json"))["status"], "needs_input")

    def test_generate_plan_preserves_manual_and_gaps(self):
        item = self.article()
        result = self.ws.generate_plan("2026-11-11")
        self.assertEqual(result["items"][0]["id"], item["id"])
        self.assertEqual(result["shortages"]["mp"], 1)
        self.assertEqual(len(self.ws.generate_plan("2026-11-11")["items"]), 1)

    def test_unstarted_edit_and_stale_request(self):
        item = self.article()
        self.jobs.request("2026-11-11", item["id"])
        self.ws.change_content("2026-11-11", item["id"], {"platform": "mp"}, 1)
        self.assertEqual(self.jobs.tick()["state"], "idle")

    def test_real_files_mock_chain_never_publishes(self):
        item = self.article()
        self.assertTrue(self.jobs.produce("2026-11-11", item["id"])["ok"])
        result = self.ws.detail("2026-11-11", item["id"])
        self.assertTrue(result["approval_current"])
        self.assertFalse(result["publish_enabled"])
        self.assertEqual(self.runner.calls, ["research", "write", "assets", "review"])

    def test_changed_image_invalidates_approval(self):
        item = self.article()
        self.jobs.produce("2026-11-11", item["id"])
        result = self.ws.detail("2026-11-11", item["id"])
        path = safe_path(self.ws.revision_dir("2026-11-11", item["id"], 1), result["artifacts"]["assets"]["items"][0]["path"])
        path.write_bytes(b"changed")
        self.assertFalse(self.ws.approval_current("2026-11-11", item))

    def test_started_edit_requires_revision(self):
        item = self.article()
        self.jobs.produce("2026-11-11", item["id"])
        with self.assertRaises(Conflict):
            self.ws.change_content("2026-11-11", item["id"], {"platform": "mp"}, 1)
        new = self.ws.revise("2026-11-11", item["id"], "改写", 1)
        self.assertEqual(new["revision"], 2)
        self.assertFalse(self.ws.approval_current("2026-11-11", new))

    def test_low_review_blocks_and_keeps_quality_budget(self):
        item = self.article()
        self.runner.score = 1
        result = self.jobs.produce("2026-11-11", item["id"])
        self.assertFalse(result["ok"])
        with self.assertRaises(Conflict):
            self.ws.revise("2026-11-11", item["id"], "补充", 1)
        self.assertEqual(self.ws.content("2026-11-11", item["id"])["revision"], 1)

    def test_retry_recovers_only_missing_stage(self):
        item = self.article()
        self.runner.failure = "assets"
        self.jobs.produce("2026-11-11", item["id"])
        self.runner.failure = None
        self.jobs.retry("2026-11-11", item["id"], 1)
        self.jobs.tick()
        self.assertEqual(self.runner.calls.count("write"), 1)

    def test_schedule_tick_does_not_duplicate_plans(self):
        self.topic()
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "auto_plan": True}, 1)
        now = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
        self.jobs.tick(now)
        count = len(self.ws.plan("2026-11-11")["items"])
        self.jobs.tick(now)
        self.assertEqual(len(self.ws.plan("2026-11-11")["items"]), count)

    def test_auto_produce_only_current_day_due(self):
        item = self.article()
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "auto_produce": True}, 1)
        self.jobs.tick(datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(self.ws.content("2026-11-11", item["id"])["status"], "ready")

    def test_delivery_moves_and_detects_tampering(self):
        item = self.article("mp")
        self.jobs.produce("2026-11-11", item["id"])
        target = RUN_DIR / "delivery" / self._testMethodName
        self.ws.export("2026-11-11", item["id"], target)
        self.assertEqual(verify_delivery(target)["content_id"], item["id"])
        (target / "draft.json").write_text("{}")
        with self.assertRaises(ValueError):
            verify_delivery(target)

    def test_research_claim_blocks_input_edit_before_running(self):
        value = self.ws.add_input({"theme": "one"})
        with self.ws.claim("research-" + value["id"]):
            with self.assertRaises(Conflict):
                self.ws.update_input(value["id"], {"theme": "two"}, 1)
        self.assertEqual(load_json(self.ws.path(f"library/inbox/{value['id']}/input.json"))["theme"], "one")

    def test_failed_writer_after_rejection_preserves_feedback(self):
        item = self.article()
        original = self.runner.run
        def run(stage, package, attempt, timeout):
            if stage == "review" and self.runner.calls.count("review") == 0:
                self.runner.score = 1
                result = original(stage, package, attempt, timeout)
                self.runner.score = 4
                self.runner.failure = "write"
                return result
            return original(stage, package, attempt, timeout)
        self.runner.run = run
        self.assertFalse(self.jobs.produce("2026-11-11", item["id"])["ok"])
        state = load_json(self.ws.revision_dir("2026-11-11", item["id"], 1) / "progress.json")
        self.assertEqual(state["phase"], "write")
        self.assertIn("feedback", state["package"])
        self.assertEqual(state["quality_used"], 1)
        self.runner.failure = None
        self.jobs.retry("2026-11-11", item["id"], 1)
        self.assertTrue(self.jobs.tick()["ok"])
        self.assertEqual(self.runner.calls.count("review"), 2)

    def test_finalization_retry_does_not_repeat_approved_model(self):
        item = self.article("mp")
        original = self.jobs.store("review")._finish
        with patch.object(self.jobs.store("review"), "_finish", side_effect=OSError("test I/O failure")):
            self.assertFalse(self.jobs.produce("2026-11-11", item["id"])["ok"])
        calls = len(self.runner.calls)
        self.jobs.retry("2026-11-11", item["id"], 1)
        self.assertTrue(self.jobs.tick()["ok"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertTrue(self.ws.approval_current("2026-11-11", item))

    def test_checkpoint_changed_draft_prevents_resume(self):
        item = self.article()
        self.runner.failure = "assets"
        self.jobs.produce("2026-11-11", item["id"])
        root = self.ws.revision_dir("2026-11-11", item["id"], 1)
        state = load_json(root / "progress.json")
        safe_path(root, state["draft_path"]).write_text("{}")
        self.runner.failure = None
        self.jobs.retry("2026-11-11", item["id"], 1)
        self.assertFalse(self.jobs.tick()["ok"])
        self.assertEqual(self.runner.calls.count("review"), 0)

    def test_last_quality_round_finalization_resumes_without_model(self):
        item = self.article("mp")
        topic = self.ws.topic(item["topic_id"])
        self.ws.set_quality_rounds("2026-11-11", item["id"], 1, self.jobs.policy()["scoring"]["max_iterations"] - 1)
        with patch.object(self.jobs.store("review"), "_finish", side_effect=OSError("test final save failure")):
            self.assertFalse(self.jobs.produce("2026-11-11", item["id"])["ok"])
        before = len(self.runner.calls)
        self.jobs.retry("2026-11-11", item["id"], 1)
        self.assertTrue(self.jobs.tick()["ok"])
        self.assertEqual(before, len(self.runner.calls))

if __name__ == "__main__":
    unittest.main()
