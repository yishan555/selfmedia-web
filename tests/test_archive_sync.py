"""Offline archive/sync tests. Simulated remote state only; no network mutation."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from harness_support import bootstrap
RUN_DIR = bootstrap(__file__)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.workbench.archive import ArchiveSync, canonical, canonical_remote, is_allowed_commit_path, is_managed, portable_settings_bytes, scan_secrets
from apps.workbench.workspace import Workspace, atomic_json, load_json

SETTINGS = {"timezone": "Asia/Shanghai", "planning_time": "16:50", "production_time": "07:00", "daily_counts": {"xhs": 1, "mp": 0}, "auto_plan": False, "auto_produce": False, "execution_mode": "external", "host": "codex", "publish_enabled": False}


def git(root, *args):
    result = subprocess.run([shutil.which("git"), *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8", env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


def commit(root, message="test"):
    git(root, "add", "-A")
    git(root, "commit", "-m", message)


def topic(ws, title="跨机选题"):
    value = {"id": "a" * 32, "status": "ready", "title": title, "created_at": "2026-11-10T00:00:00+00:00", "research": {"summary": "本地 fixture", "sources": [{"url": "https://example.org"}], "evidence": [{"id": "e1", "claim": "本地 fixture", "source_url": "https://example.org", "excerpt": "本地"}]}}
    atomic_json(ws.path(f"library/topics/{value['id']}/topic.json"), value)
    return value


class FakeArchive(ArchiveSync):
    """Exercise real file/git sync while replacing only remote transport."""

    def __init__(self, workspace, repo, *, remote_manifest=None, remote_blobs=None, base_manifest=None, remote_sha=None, base_sha=None, **kwargs):
        super().__init__(workspace, repo, remote="https://github.com/yishan555/selfmedia-web", project=repo, allow_local_remote=True, **kwargs)
        self.remote_manifest = dict(remote_manifest or {})
        self.remote_blobs = dict(remote_blobs or {})
        self.base_manifest = dict(base_manifest or {})
        self.remote_sha = remote_sha
        self.base_sha = base_sha

    def ensure_repo(self):
        return None

    def _fetch(self):
        return self.remote_sha

    def _merge_base(self, local, remote):
        return self.base_sha

    def _baseline(self, ref):
        return self.base_manifest if ref else {}

    def _remote_manifest(self, ref):
        return self.remote_manifest if ref else {}

    def _blob_bytes(self, ref, relative):
        return self.remote_blobs[relative]

    def _push(self, enabled):
        return True, "pushed", None


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.base = RUN_DIR / self._testMethodName
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.org")
        (self.repo / ".gitignore").write_text(".local/\n", encoding="utf-8")
        (self.repo / "README.md").write_text("seed\n", encoding="utf-8")
        commit(self.repo, "seed")
        self.seed_commit = git(self.repo, "rev-parse", "HEAD").strip()
        self.ws = Workspace(self.repo)
        self.ws.initialize(dict(SETTINGS))
        topic(self.ws)
        item = self.ws.add_content("2026-11-11", "a" * 32, "xhs")
        self.content_id = item["id"]
        root = self.ws.revision_dir("2026-11-11", item["id"], 1)
        atomic_json(root / "draft.json", {"title": "标题", "body": "正文"})
        atomic_json(root / "receipts" / "write-test.json", {"stage": "write", "outcome": "succeeded", "time": "2026-11-11T00:00:00+00:00"})
        atomic_json(self.ws.path(".local/attempts/" + "b" * 32 + "/ticket.json"), {"attempt": "b" * 32, "token": "private-token", "owner": "private-session", "role": "write", "date": "2026-11-11", "content_id": item["id"], "revision": 1, "state": "submitted", "created_at": "2026-11-11T00:00:00+00:00", "budget": 10})
        (self.ws.path(".local/attempts/" + "b" * 32) / "events.jsonl").write_text(canonical({"event": "submitted", "session": "private-session", "token": "private-token"}) + "\n", encoding="utf-8")

    def test_remote_paths_are_normalized_and_allowlisted(self):
        self.assertEqual(canonical_remote("git@github.com:yishan555/selfmedia-web.git"), "https://github.com/yishan555/selfmedia-web")
        self.assertEqual(canonical_remote("https://github.com/yishan555/selfmedia-web.git/"), "https://github.com/yishan555/selfmedia-web")
        self.assertTrue(is_managed("2026-11-11/contents/" + "a" * 32 + "/r0001/draft.json"))
        self.assertTrue(is_managed("config/settings.json"))
        self.assertFalse(is_managed(".local/attempts/x/ticket.json"))
        self.assertFalse(is_allowed_commit_path("private/credentials.json"))
        self.assertTrue(is_allowed_commit_path("_pipeline/attempts/x/summary.json"))

    def test_sync_archives_by_date_and_redacts_runtime(self):
        sync = FakeArchive(self.ws, self.repo)
        result = sync.sync(push=False, build_bundles=False)
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.repo / "2026-11-11/contents" / self.content_id / "r0001/draft.json").is_file())
        self.assertTrue((self.repo / "_sync/baseline.json").is_file())
        pipeline = (self.repo / "_pipeline/attempts" / ("b" * 32) / "summary.json").read_text(encoding="utf-8")
        self.assertNotIn("private-token", pipeline)
        self.assertNotIn("private-session", pipeline)
        events = (self.repo / "_pipeline/attempts" / ("b" * 32) / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private-token", events)
        self.assertNotIn("private-session", events)

    def test_second_sync_without_changes_is_noop(self):
        first = FakeArchive(self.ws, self.repo).sync(push=False, build_bundles=False)
        self.assertTrue(first["ok"], first)
        second = FakeArchive(self.ws, self.repo).sync(push=False, build_bundles=False)
        self.assertEqual(second["imported"], [], second)
        self.assertEqual(second["exported"], [], second)
        self.assertEqual(second["merged"], [], second)
        self.assertEqual(second["local_files"], first["local_files"])

    def test_remote_ahead_imports_new_date_and_plan_entry(self):
        local_plan = load_json(self.ws.path("2026-11-11/plan.json"))
        remote_plan = {"schema_version": 1, "date": "2026-11-11", "revision": 1, "items": [*local_plan["items"], {"id": "c" * 32, "topic_id": "a" * 32, "platform": "mp", "revision": 1, "status": "ready", "manual": True, "created_at": "2026-11-10T00:00:00+00:00", "production_time": "07:00"}], "shortages": {}}
        plan_bytes = canonical(remote_plan).encode("utf-8")
        article = b'{"article":true}'
        remote_manifest = {
            "2026-11-11/plan.json": hashlib.sha256(plan_bytes).hexdigest(),
            "2026-11-11/contents/" + "c" * 32 + "/r0001/article.md": hashlib.sha256(article).hexdigest(),
        }
        sync = FakeArchive(self.ws, self.repo, remote_sha="f" * 40, base_sha=self.seed_commit, base_manifest={}, remote_manifest=remote_manifest, remote_blobs={"2026-11-11/plan.json": plan_bytes, "2026-11-11/contents/" + "c" * 32 + "/r0001/article.md": article})
        result = sync.sync(push=True, build_bundles=False)
        self.assertTrue(result["ok"], result)
        self.assertIn("2026-11-11/plan.json", result["merged"])
        self.assertIn("2026-11-11/contents/" + "c" * 32 + "/r0001/article.md", result["imported"])
        plan = load_json(self.ws.path("2026-11-11/plan.json"))
        self.assertEqual({entry["id"] for entry in plan["items"]}, {local_plan["items"][0]["id"], "c" * 32})
        self.assertTrue((self.ws.path("2026-11-11/contents/" + "c" * 32 + "/r0001/article.md")).is_file())

    def test_conflicting_content_stops_without_overwrite(self):
        draft = canonical({"remote": True}).encode("utf-8")
        path = "2026-11-11/contents/" + self.content_id + "/r0001/draft.json"
        sync = FakeArchive(self.ws, self.repo, remote_sha="f" * 40, base_sha=self.seed_commit, base_manifest={}, remote_manifest={path: hashlib.sha256(draft).hexdigest()}, remote_blobs={path: draft})
        result = sync.sync(push=True, build_bundles=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "sync_conflict")
        self.assertEqual(load_json(self.ws.path(path)), {"title": "标题", "body": "正文"})

    def test_archived_settings_omit_sync_paths(self):
        settings = self.ws.settings()
        self.ws.save_settings({**settings, "archive_repo": "D:/private/machine/selfmedia-web", "archive_remote": "https://github.com/yishan555/selfmedia-web.git"}, settings["revision"])
        archived = load_json(self.ws.path("config/settings.json"))
        portable = json.loads(portable_settings_bytes(archived))
        self.assertNotIn("archive_repo", portable)
        self.assertNotIn("archive_remote", portable)
        self.assertIn("timezone", portable)
        self.assertEqual(self.ws.settings()["archive_repo"], "D:/private/machine/selfmedia-web")

    def test_active_claim_blocks_sync(self):
        atomic_json(self.ws.path(".local/claims/content-" + "a" * 32 + ".json"), {"attempt": "b" * 32})
        result = FakeArchive(self.ws, self.repo).sync(push=False, build_bundles=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "active_work")

    def test_plan_items_merge_and_revision_same_item_conflicts(self):
        base = {"schema_version": 1, "date": "2026-11-11", "revision": 1, "items": [{"id": "a" * 32, "status": "queued", "revision": 1}], "shortages": {"xhs": 0, "mp": 1}}
        local = {"schema_version": 1, "date": "2026-11-11", "revision": 2, "items": [{"id": "a" * 32, "status": "queued", "revision": 1}, {"id": "b" * 32, "status": "queued", "revision": 2}], "shortages": {"xhs": 0, "mp": 1}}
        remote = {"schema_version": 1, "date": "2026-11-11", "revision": 2, "items": [{"id": "a" * 32, "status": "queued", "revision": 1}, {"id": "c" * 32, "status": "queued", "revision": 2}], "shortages": {"xhs": 0, "mp": 1}}
        helper = ArchiveSync(self.ws, self.repo, remote="https://github.com/yishan555/selfmedia-web", allow_local_remote=True)
        merged, conflicts = helper._merge_plan_values(base, local, remote)
        self.assertEqual(conflicts, [])
        self.assertEqual([item["id"] for item in merged["items"]], ["a" * 32, "b" * 32, "c" * 32])
        local["items"][0]["status"] = "ready"
        remote["items"][0]["status"] = "cancelled"
        merged, conflicts = helper._merge_plan_values(base, local, remote)
        self.assertIsNone(merged)
        self.assertEqual(conflicts, ["a" * 32])

    def test_secret_scan_detects_bearer_and_ignores_images(self):
        (self.repo / "notes.txt").write_text("Authorization: " + "Bearer " + "x" * 32, encoding="utf-8")
        self.assertIn("notes.txt", scan_secrets(self.repo))
        (self.repo / "notes.txt").unlink()
        (self.repo / "image.png").write_bytes(b"-----BEGIN " + b"PRIVATE KEY-----")
        self.assertEqual(scan_secrets(self.repo), [])

    def test_status_reports_ahead_and_in_sync(self):
        ahead = FakeArchive(self.ws, self.repo).status()
        self.assertEqual(ahead["state"], "local_ahead")
        sync = FakeArchive(self.ws, self.repo)
        self.assertTrue(sync.sync(push=True, build_bundles=False)["ok"])
        manifest = sync._repo_manifest()
        commit_now = git(self.repo, "rev-parse", "HEAD").strip()
        in_sync = FakeArchive(self.ws, self.repo, remote_sha=commit_now, base_sha=commit_now, base_manifest=manifest, remote_manifest=manifest).status()
        self.assertEqual(in_sync["state"], "in_sync")

    def test_repo_must_exist_and_be_root(self):
        missing = ArchiveSync(self.ws, self.base / "missing", remote="https://github.com/yishan555/selfmedia-web")
        with self.assertRaises(ValueError):
            missing.ensure_repo()
        wrong = ArchiveSync(self.ws, self.repo, remote="https://github.com/yishan555/selfmedia-web.git")
        with self.assertRaises(ValueError):
            wrong.status()


if __name__ == "__main__":
    unittest.main()
