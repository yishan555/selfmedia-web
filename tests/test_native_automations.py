"""Offline templates and host-independent commands, never an active automation."""
import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness_support import bootstrap
RUN_DIR = bootstrap(__file__)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.workbench.workspace import Workspace
from apps.workbench.automation import task_templates
from apps.workbench.__main__ import main, skill_root, installed_report

SETTINGS = {"timezone": "Asia/Shanghai", "planning_time": "21:17", "production_time": "08:07", "daily_counts": {"xhs": 1, "mp": 1}, "auto_plan": False, "auto_produce": False, "execution_mode": "external", "host": "codex", "publish_enabled": False}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(RUN_DIR / self._testMethodName)
        self.ws.initialize(copy.deepcopy(SETTINGS))
        self.project = RUN_DIR / "project"
        self.project.mkdir(exist_ok=True)

    def test_templates_are_not_created_tasks(self):
        result = task_templates(self.ws, self.project, self.project / ".agents/skills")
        self.assertEqual(result["host"], "codex")
        self.assertFalse(result["created_automations"])
        self.assertFalse(result["publish_enabled"])
        self.assertEqual([item["id"] for item in result["tasks"]], ["research", "plan", "write", "assets", "review", "archive"])

    def test_host_specific_invocation_without_private_variables(self):
        for host in ("codex", "claude", "hermes"):
            result = task_templates(self.ws, self.project, self.project / "skills", host)
            for item in result["tasks"]:
                self.assertNotIn("CLAUDE_SKILL_DIR", item["prompt"])
                self.assertNotIn("dangerously", item["prompt"])
            self.assertIn(("$" if host == "codex" else "/") + "content-engine-research", result["tasks"][0]["prompt"])

    def test_review_is_independent_and_image_based(self):
        result = task_templates(self.ws, self.project, self.project / "skills")
        review = next(item for item in result["tasks"] if item["id"] == "review")
        self.assertEqual(review["max_items_per_run"], 1)
        self.assertIn("独立", review["prompt"])
        self.assertIn("view_image", review["prompt"])
        self.assertIn("submit", review["prompt"])

    def test_plan_uses_workspace_timezone_and_tomorrow(self):
        result = task_templates(self.ws, self.project, self.project / "skills")
        plan = next(item for item in result["tasks"] if item["id"] == "plan")
        self.assertIn("plan --tomorrow", plan["prompt"])
        self.assertIn("--automatic", plan["prompt"])
        self.assertIn("auto_plan", plan["prompt"])
        self.assertIn("21:17", plan["schedule_hint"])
        self.assertIn("Asia/Shanghai", plan["schedule_hint"])
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "planning_time": "16:50", "production_time": "07:00"}, settings["revision"])
        updated = task_templates(self.ws, self.project, self.project / "skills")
        updated_plan = next(item for item in updated["tasks"] if item["id"] == "plan")
        self.assertEqual(updated_plan["schedule_hint"], "16:50 (Asia/Shanghai)")
        self.assertEqual(updated["production_not_before"], "07:00")

    def test_archive_steps_are_pre_and_post_production(self):
        result = task_templates(self.ws, self.project, self.project / "skills")
        plan = next(item for item in result["tasks"] if item["id"] == "plan")
        archive = next(item for item in result["tasks"] if item["id"] == "archive")
        for prompt in (plan["prompt"], archive["prompt"]):
            self.assertIn("archive --repo", prompt)
            self.assertIn("--mode sync", prompt)
            self.assertIn("不覆盖、不 force push", prompt)
            self.assertIn("脱敏管线", prompt)
        self.assertIn("当天最后一个", archive["prompt"])
        self.assertIn("在途 claim", archive["prompt"])
        self.assertFalse(result["created_automations"])
        self.assertFalse(result["publish_enabled"])

    def test_missing_hermes_profile_not_guessed(self):
        with self.assertRaises(ValueError):
            skill_root(self.project, "hermes")
        self.assertEqual(skill_root(self.project, "hermes", str(self.project / "skills")), self.project / "skills")

    def test_project_discovery_is_not_claude_only(self):
        self.assertEqual(skill_root(self.project, "codex"), self.project / ".agents/skills")
        self.assertEqual(skill_root(self.project, "claude"), self.project / ".claude/skills")

    def test_doctor_is_offline_package_report(self):
        result = installed_report(self.project / "missing", "codex")
        self.assertFalse(result["ok"])
        self.assertFalse(result["native_automation_verified"])
        self.assertEqual(set(result["packages"]), {"research", "write", "assets", "review", "publish"})

    def test_template_export_contains_no_token_or_claim(self):
        result = task_templates(self.ws, self.project, self.project / "skills")
        self.assertNotIn("claims/", json.dumps(result))
        self.assertFalse(any("token" in task for task in result["tasks"]))

    def test_cli_does_not_require_a_claude_process(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--workspace", str(self.ws.root), "--skills", str(self.project / "skills"), "automation-templates", "--project", str(self.project)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["host"], "codex")

    def test_cli_arguments_and_help_are_single_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--workspace", str(self.ws.root), "prepare", "--role", "write"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_automatic_plan_disabled_does_not_write_plan(self):
        self.ws.clock = lambda: "2026-11-11T14:00:00+00:00"
        result = self.ws.generate_plan(automatic=True)
        self.assertEqual(result["reason_code"], "auto_plan_disabled")
        self.assertFalse(self.ws.path("2026-11-12/plan.json").exists())

    def test_automatic_plan_reads_time_and_runs_once(self):
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "auto_plan": True}, settings["revision"])
        self.ws.clock = lambda: "2026-11-11T13:16:59+00:00"
        self.assertEqual(self.ws.generate_plan(automatic=True)["reason_code"], "planning_not_due")
        self.assertFalse(self.ws.path("2026-11-12/plan.json").exists())
        self.ws.clock = lambda: "2026-11-11T13:17:00+00:00"
        result = self.ws.generate_plan(automatic=True)
        self.assertEqual(result["date"], "2026-11-12")
        self.assertEqual(result["shortages"], {"xhs": 1, "mp": 1})
        before = self.ws.path("2026-11-12/plan.json").read_bytes()
        self.assertEqual(self.ws.generate_plan(automatic=True)["reason_code"], "already_planned")
        self.assertEqual(before, self.ws.path("2026-11-12/plan.json").read_bytes())
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "auto_plan": False}, settings["revision"])
        self.assertEqual(self.ws.generate_plan(automatic=True)["reason_code"], "auto_plan_disabled")

    def test_automatic_plan_uses_workspace_date_across_midnight(self):
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "auto_plan": True, "planning_time": "00:01"}, settings["revision"])
        self.ws.clock = lambda: "2026-11-11T16:02:00+00:00"
        self.assertEqual(self.ws.generate_plan(automatic=True)["date"], "2026-11-13")

    def test_manual_plan_bypasses_automatic_switch(self):
        self.assertEqual(self.ws.generate_plan("2026-11-12")["date"], "2026-11-12")

    def test_automatic_cli_requires_tomorrow(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--workspace", str(self.ws.root), "plan", "--date", "2026-11-12", "--automatic"])
        self.assertEqual(code, 1)
        self.assertIn("requires --tomorrow", json.loads(output.getvalue())["message"])

    def test_automatic_cli_passes_gate(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--workspace", str(self.ws.root), "plan", "--tomorrow", "--automatic"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["plan"]["reason_code"], "auto_plan_disabled")


if __name__ == "__main__":
    unittest.main()
