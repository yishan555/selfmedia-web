"""Offline local web acceptance using real files, never model/content acceptance."""
import sys
import unittest
from pathlib import Path
from harness_support import bootstrap

RUN_DIR = bootstrap(__file__)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
import io
import json
import os
import re
import uuid
from unittest.mock import patch
from PIL import Image

from apps.workbench.app import create_app
from apps.workbench.workspace import Workspace, atomic_json

SETTINGS = {"timezone": "Asia/Shanghai", "planning_time": "21:17", "production_time": "08:07", "daily_counts": {"xhs": 1, "mp": 1}, "auto_plan": False, "auto_produce": False, "execution_mode": "external", "host": "codex", "publish_enabled": False}
DATE = "2026-11-10"


class FakeJobs:
    """Only records queue requests. No tick/runner or model exists in this fixture."""
    def __init__(self):
        self.calls = []
    def request(self, date, content_id):
        self.calls.append((date, content_id))
        return {"ok": True, "itemid": content_id}
    def retry(self, date, content_id, revision):
        self.calls.append((date, content_id, revision))
        return {"ok": True, "itemid": content_id}


class WorkbenchWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = RUN_DIR / uuid.uuid4().hex
        self.ws = Workspace(self.directory)
        self.ws.initialize(dict(SETTINGS))
        self.jobs = FakeJobs()
        self.app = create_app(self.ws, self.jobs)
        self.client = self.app.test_client()
        page = self.client.get("/").get_data(as_text=True)
        self.token = re.search(r'name="workbench-token" content="([^"]+)"', page)[1]
        self.headers = {"Origin": "http://localhost", "X-Workbench-Token": self.token}

    def call(self, path, method="POST", value=None, **kwargs):
        return self.client.open(path, method=method, json=value if value is not None else {}, headers=self.headers, **kwargs)

    def topic(self, title="离线模拟选题"):
        topic = {"id": uuid.uuid4().hex, "status": "ready", "title": title, "created_at": "2026-11-10T00:00:00Z", "research": {"summary": "simulation fixture, not real research", "sources": [{"url": "https://example.com/source"}], "evidence": [{"claim": "离线 fixture", "source_url": "https://example.com/source"}]}}
        atomic_json(self.ws.path(f"library/topics/{topic['id']}/topic.json"), topic)
        return topic

    def content(self, platform="xhs"):
        return self.ws.add_content(DATE, self.topic()["id"], platform)

    def artifacts(self, platform="xhs", body="离线模拟正文", title="离线模拟标题"):
        item = self.content(platform)
        root = self.ws.revision_dir(DATE, item["id"], item["revision"])
        atomic_json(root / "draft.json", {"title": title, "body": body, "simulation": True})
        image = io.BytesIO()
        Image.new("RGB", (30, 40), (120, 140, 100)).save(image, format="PNG")
        data = image.getvalue()
        path = root / "images" / "one.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        atomic_json(root / "assets.json", {"items": [{"path": "images/one.png", "sha256": hashlib.sha256(data).hexdigest(), "kind": "photo"}], "simulation": True})
        return item, root, data

    def asset_url(self, item, relative="images/one.png"):
        return f"/api/asset/{DATE}/{item['id']}/{item['revision']}/{relative}"

    def preview_url(self, item):
        return f"/preview/{DATE}/{item['id']}/{item['revision']}/{item['platform']}"

    def test_topic_search_archive_and_revision(self):
        topic = self.topic(title="可搜索的选题")
        self.assertEqual(len(self.client.get("/api/topics?q=可搜索").json["items"]), 1)
        self.assertEqual(self.client.get("/api/topics?q=不存在").json["items"], [])
        path = f"/api/topics/{topic['id']}"
        response = self.call(path, "PATCH", {"revision": 1, "fields": {"status": "archived"}})
        self.assertEqual(response.json["item"]["status"], "archived")
        self.assertEqual(self.call(path, "PATCH", {"revision": 1, "fields": {"status": "ready"}}).status_code, 409)

    def test_topic_cannot_edit_evidence_directly(self):
        topic = self.topic()
        response = self.call(f"/api/topics/{topic['id']}", "PATCH", {"revision": 1, "fields": {"research": {"fake": True}}})
        self.assertEqual(response.status_code, 400)

    def test_native_task_templates_are_not_active_automations(self):
        response = self.client.get("/api/automations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["host"], "codex")
        self.assertFalse(response.json["created_automations"])
        self.assertFalse(response.json["publish_enabled"])
        self.assertEqual([t["id"] for t in response.json["tasks"]], ["research", "plan", "write", "assets", "review", "archive"])
        self.assertNotIn("CLAUDE_SKILL_DIR", response.get_data(as_text=True))

    def test_archive_status_has_no_private_runtime_data(self):
        response = self.client.get("/api/archive")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["publish_enabled"])
        self.assertFalse(response.json["archive"]["available"])
        self.assertNotIn("token", response.get_data(as_text=True).lower())

    def test_runtime_view_does_not_expose_attempt_credentials(self):
        self.jobs.status = lambda: {"ok": True, "attempts": [{"role": "write", "status": "active", "token": "private-attempt", "owner": "private-owner", "package_path": "private-path"}]}
        response = self.client.get("/api/runtime")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["attempts"], [{"role": "write", "status": "active"}])
        self.assertNotIn("private-", response.get_data(as_text=True))

    def test_empty_get_does_not_initialize_workspace(self):
        ws = Workspace(RUN_DIR / uuid.uuid4().hex)
        client = create_app(ws).test_client()
        for path in ("/", "/api/settings", f"/api/day/{DATE}", f"/api/plan/{DATE}", "/api/topics", "/api/inbox"):
            response = client.get(path)
            self.assertIn(response.status_code, (200, 404))
        self.assertFalse(ws.root.exists())

    def test_initialized_gets_leave_all_files_unchanged(self):
        before = {str(path): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        for path in ("/", "/static/app.js", "/static/app.css", "/api/settings", f"/api/day/{DATE}", "/api/inbox", "/api/topics"):
            self.assertEqual(self.client.get(path).status_code, 200)
        after = {str(path): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_host_rebinding_and_bad_ports_rejected(self):
        for host in ("evil.example", "127.0.0.1.evil", "localhost.evil", "localhost:0", "localhost:65536", "localhost.", "[::1]"):
            response = self.client.get("/", headers={"Host": host})
            self.assertEqual(response.status_code, 403, host)
            self.assertEqual(response.json["reason_code"], "invalid_host")

    def test_loopback_hosts_and_port_supported(self):
        for host in ("localhost", "localhost:9091", "127.0.0.1", "127.0.0.1:9091"):
            self.assertEqual(self.client.get("/", headers={"Host": host}).status_code, 200)

    def test_cross_origin_get_and_write_rejected(self):
        for origin in ("http://evil.example", "http://localhost.evil", "http://localhost/", "https://localhost", "null"):
            response = self.client.post("/api/inbox", json={"theme": "素材"}, headers={"Origin": origin, "X-Workbench-Token": self.token})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.get("/api/inbox", headers={"Origin": origin}).status_code, 403)
        self.assertEqual(self.ws.records("library/inbox", "input.json"), [])

    def test_write_requires_origin_and_memory_token(self):
        for headers in ({}, {"Origin": "http://localhost"}, {"Origin": "http://localhost", "X-Workbench-Token": "wrong"}, {"Origin": "http://localhost", "X-Workbench-Token": "凭证"}):
            self.assertEqual(self.client.post("/api/inbox", json={"theme": "素材"}, headers=headers).status_code, 403)
        self.assertEqual(self.ws.records("library/inbox", "input.json"), [])

    def test_token_is_per_app_not_on_disk(self):
        other = create_app(self.ws).test_client().get("/").get_data(as_text=True)
        other_token = re.search(r'name="workbench-token" content="([^"]+)"', other)[1]
        self.assertNotEqual(self.token, other_token)
        for path in self.directory.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.token.encode(), path.read_bytes())

    def test_origin_with_explicit_port_must_match(self):
        headers={"Host": "127.0.0.1:9091", "Origin": "http://127.0.0.1:9091", "X-Workbench-Token": self.token}
        response=self.client.post("/api/inbox", json={"theme": "素材"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        headers["Origin"]="http://localhost:9091"
        self.assertEqual(self.client.post("/api/inbox", json={"theme": "素材"}, headers=headers).status_code,403)

    def test_single_theme_url_or_reference_saved_queued(self):
        for fields in ({"theme": "纯主题"}, {"url": "https://example.com"}, {"reference": "参考原文"}):
            response=self.call("/api/inbox", value=fields)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json["item"]["status"],"queued")
            self.assertEqual(response.json["item"]["revision"],1)
        self.assertEqual(len(self.client.get("/api/inbox").json["items"]),3)
        self.assertEqual(self.jobs.calls,[])

    def test_input_edit_uses_revision_and_requeues(self):
        item=self.call("/api/inbox",value={"theme":"旧主题"}).json["item"]
        response=self.call(f"/api/inbox/{item['id']}","PATCH",{"revision":1,"fields":{"theme":"新主题"}})
        self.assertEqual(response.json["item"]["revision"],2)
        self.assertEqual(response.json["item"]["status"],"queued")
        stale=self.call(f"/api/inbox/{item['id']}","PATCH",{"revision":1,"fields":{"theme":"覆盖"}})
        self.assertEqual(stale.status_code,409)
        self.assertEqual(stale.json["reason_code"],"revision_conflict")

    def test_input_whitelist_and_http_url_validation(self):
        for fields in ({"theme":"x","command":"erase"},{"theme":"x","status":"ready"},{"url":"javascript:alert(1)"},{"notes":"only notes"},{"theme":2}):
            self.assertEqual(self.call("/api/inbox",value=fields).status_code,400)
        self.assertEqual(self.ws.records("library/inbox","input.json"),[])

    def test_duplicate_json_and_nonfinite_rejected(self):
        for text in ('{"theme":"x","theme":"y"}','{"theme":NaN}', '[]', 'null'):
            response=self.client.post("/api/inbox",data=text,content_type="application/json",headers=self.headers)
            self.assertEqual(response.status_code,400)
            self.assertFalse(response.json["ok"])

    def test_large_payload_and_non_json_rejected(self):
        response=self.call("/api/inbox",value={"reference":"x"*1_000_001})
        self.assertEqual(response.status_code,413)
        self.assertEqual(response.json["reason_code"],"payload_too_large")
        self.assertEqual(self.client.post("/api/inbox",data="theme=x",headers=self.headers).status_code,415)

    def test_settings_show_only_editable_fields(self):
        response=self.client.get("/api/settings")
        self.assertEqual(response.json["settings"]["planning_time"],"21:17")
        self.assertEqual(response.json["settings"]["production_time"],"08:07")
        self.assertFalse(response.json["settings"]["auto_plan"])
        self.assertFalse(response.json["publish_enabled"])
        self.assertNotIn("runner_command",response.json["settings"])

    def test_settings_revision_and_automation_edit(self):
        response=self.call("/api/settings","PUT",{"revision":1,"production_time":"09:23","auto_plan":True})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json["settings"]["revision"],2)
        self.assertTrue(self.ws.settings()["auto_plan"])
        self.assertEqual(self.ws.settings()["execution_mode"], "external")
        self.assertEqual(self.ws.settings()["host"], "codex")
        self.assertEqual(self.call("/api/settings","PUT",{"revision":1,"production_time":"10:01"}).status_code,409)

    def test_settings_runner_and_publication_changes_rejected(self):
        for fields in ({"runner_command":["evil"]},{"publish_enabled":True},{"arbitrarycommand":"evil"},{"daily_counts":{"xhs":-1,"mp":1}},{"planning_time":"25:00"},{"timezone":"Not/AZone"}):
            self.assertEqual(self.call("/api/settings","PUT",{"revision":1,**fields}).status_code,400)
        self.assertEqual(self.ws.settings()["revision"],1)

    def test_bool_revision_is_invalid(self):
        self.assertEqual(self.call("/api/settings","PUT",{"revision":True,"auto_plan":True}).status_code,400)

    def test_generate_plan_shortages_and_idempotent_slots(self):
        self.topic()
        response=self.call(f"/api/plan/{DATE}")
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json["plan"]["items"]),1)
        self.assertEqual(sum(response.json["plan"]["shortages"].values()),1)
        self.assertEqual(len(self.call(f"/api/plan/{DATE}").json["plan"]["items"]),1)
        self.assertEqual(self.jobs.calls,[])

    def test_day_has_topic_title_and_current_approval(self):
        item=self.content()
        response=self.client.get(f"/api/day/{DATE}")
        self.assertEqual(response.json["items"][0]["topic_title"],"离线模拟选题")
        self.assertFalse(response.json["items"][0]["approval_current"])
        self.assertEqual(response.json["plan"]["items"][0]["id"],item["id"])

    def test_produce_creates_and_only_requests_job(self):
        topic=self.topic()
        response=self.call("/api/produce",value={"date":DATE,"topic_id":topic["id"],"platform":"mp"})
        self.assertEqual(response.status_code,200)
        item=response.json["item"]
        self.assertEqual(self.jobs.calls,[(DATE,item["id"])])
        self.assertEqual(item["status"],"queued")
        self.assertFalse(self.ws.revision_dir(DATE,item["id"],1).exists())
        self.assertEqual(self.call(f"/api/content/{DATE}/{item['id']}/produce").status_code,200)
        self.assertEqual(len(self.jobs.calls),2)

    def test_without_jobs_503_before_creating_content(self):
        topic=self.topic()
        app=create_app(self.ws)
        client=app.test_client()
        token=re.search(r'name="workbench-token" content="([^"]+)"',client.get("/").get_data(as_text=True))[1]
        response=client.post("/api/produce",json={"date":DATE,"topic_id":topic["id"],"platform":"xhs"},headers={"Origin":"http://localhost","X-Workbench-Token":token})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json["reason_code"],"worker_unavailable")
        self.assertEqual(self.ws.plan(DATE)["items"],[])

    def test_unstarted_content_edit_and_stale_revision(self):
        item=self.content()
        path=f"/api/content/{DATE}/{item['id']}"
        response=self.call(path,"PATCH",{"revision":1,"fields":{"platform":"mp","production_time":"11:23"}})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json["item"]["revision"],2)
        self.assertEqual(response.json["item"]["platform"],"mp")
        self.assertEqual(self.call(path,"PATCH",{"revision":1,"fields":{"production_time":"12:00"}}).status_code,409)
        self.assertEqual(self.call(path,"PATCH",{"revision":2,"fields":{"publish_enabled":True}}).status_code,400)

    def test_production_default_does_not_reschedule_existing_items(self):
        item = self.content()
        response = self.call('/api/settings', 'PUT', {'revision': 1, 'production_time': '10:30'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ws.content(DATE, item['id'])['production_time'], '08:07')
        later = self.content()
        self.assertEqual(later['production_time'], '10:30')
        response = self.call(f"/api/content/{DATE}/{item['id']}", 'PATCH', {'revision': 1, 'fields': {'production_time': '11:45'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['item']['production_time'], '11:45')

    def test_production_default_label_is_explicit(self):
        self.assertIn('新条目默认生产时间', self.client.get('/').get_data(as_text=True))

    def test_started_frozen_and_feedback_creates_new_revision(self):
        item,root,_=self.artifacts()
        self.ws.set_status(DATE,item["id"],1,"running")
        self.ws.set_status(DATE,item["id"],1,"completed")
        path=f"/api/content/{DATE}/{item['id']}"
        self.assertEqual(self.call(path,"PATCH",{"revision":1,"fields":{"platform":"mp"}}).status_code,409)
        response=self.call(path+"/revise",value={"revision":1,"feedback":"修改离线正文"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json["item"]["revision"],2)
        self.assertTrue((root/"draft.json").exists())
        detail=self.client.get(path).json["detail"]
        self.assertFalse(detail["approval_current"])
        self.assertNotIn("draft",detail["artifacts"])
        self.assertEqual(self.jobs.calls,[])

    def test_retry_revision_fence(self):
        item=self.content()
        path=f"/api/content/{DATE}/{item['id']}/retry"
        self.assertEqual(self.call(path,value={"revision":2}).status_code,409)
        self.assertEqual(self.jobs.calls,[])
        self.assertEqual(self.call(path,value={"revision":1}).status_code,200)
        self.assertEqual(self.jobs.calls,[(DATE,item["id"],1)])

    def test_detail_has_artifacts_topic_assets_and_publish_disabled(self):
        item,_,_=self.artifacts()
        detail=self.client.get(f"/api/content/{DATE}/{item['id']}").json["detail"]
        self.assertIn("draft",detail["artifacts"])
        self.assertIn("topic",detail)
        self.assertEqual(len(detail["asset_urls"]),1)
        self.assertEqual(detail["asset_urls"][0]["url"],self.asset_url(item))
        self.assertFalse(detail["publish_enabled"])

    def test_assets_hash_and_decoded_mime(self):
        item,root,data=self.artifacts()
        response=self.client.get(self.asset_url(item))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.mimetype,"image/png")
        self.assertEqual(response.data,data)
        (root/"images/one.png").write_bytes(b"changed")
        self.assertEqual(self.client.get(self.asset_url(item)).status_code,404)

    def test_workspace_image_cannot_replace_missing_revision_asset(self):
        item,root,data=self.artifacts()
        relative="images/"+uuid.uuid4().hex+"/photo.png"
        path=self.ws.path(relative)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        atomic_json(root/"assets.json",{"items":[{"path":relative,"sha256":hashlib.sha256(data).hexdigest()}]})
        self.assertEqual(self.client.get(self.asset_url(item,relative)).status_code,404)

    def test_unregistered_and_non_image_resources_404(self):
        item,root,_=self.artifacts()
        for relative in ("draft.json","approval.json","../../config/settings.json","images/missing.png","selfmedia.db"):
            self.assertEqual(self.client.get(self.asset_url(item,relative)).status_code,404)
        raw=(root/"draft.json").read_bytes()
        atomic_json(root/"assets.json",{"items":[{"path":"draft.json","sha256":hashlib.sha256(raw).hexdigest()}]})
        self.assertEqual(self.client.get(self.asset_url(item,"draft.json")).status_code,404)

    def test_encoded_traversal_and_absolute_paths_404(self):
        item,_,_=self.artifacts()
        for relative in ("%2e%2e/config/settings.json","%2fconfig/settings.json","C%3A/secrets.png","images%5Cone.png"):
            self.assertEqual(self.client.get(self.asset_url(item,relative)).status_code,404)

    def test_symlink_resource_rejected(self):
        item,root,data=self.artifacts()
        target=self.directory/"outside.png"
        target.write_bytes(data)
        path=root/"images/link.png"
        try:
            os.symlink(target,path)
        except OSError:
            self.skipTest("Windows symlink privilege unavailable; safe_path link checks remain covered statically")
        atomic_json(root/"assets.json",{"items":[{"path":"images/link.png","sha256":hashlib.sha256(data).hexdigest()}]})
        self.assertEqual(self.client.get(self.asset_url(item,"images/link.png")).status_code,404)

    def test_xhs_preview_escapes_title_body_and_uses_3_to_4(self):
        item,_,_=self.artifacts(title='<script>alert("title")</script>',body='<img src=x onerror="alert(1)">\n离线模拟')
        response=self.client.get(self.preview_url(item))
        text=response.get_data(as_text=True)
        self.assertEqual(response.status_code,200)
        self.assertNotIn("<script>",text)
        self.assertIn("&lt;script&gt;",text)
        self.assertNotIn('<img src=x',text)
        self.assertIn("aspect-ratio:3/4",text)
        self.assertIn(self.asset_url(item),text)
        self.assertIn("非平台像素复刻",text)

    def test_mp_preview_markdown_and_raw_html_safe(self):
        body='# 离线模拟\n\n**粗体**\n\n<script>alert(1)</script>\n\n<img src="https://tracker.example/x" onerror="alert(1)">\n\n[攻击](javascript:alert(1))'
        item,_,_=self.artifacts("mp",body=body)
        text=self.client.get(self.preview_url(item)).get_data(as_text=True)
        self.assertIn("<strong>粗体</strong>",text)
        self.assertNotIn("<script>",text)
        self.assertNotIn('<img src="https://tracker',text)
        self.assertNotIn('href="javascript:',text)
        self.assertIn("&lt;script&gt;",text)

    def test_mp_images_rewrite_only_registered_local_paths(self):
        body='![本地](images/one.png)\n\n![远程](https://tracker.example/pixel.png)\n\n![伪造](images/not-registered.png)'
        item,_,_=self.artifacts("mp",body=body)
        text=self.client.get(self.preview_url(item)).get_data(as_text=True)
        self.assertIn('src="'+self.asset_url(item)+'"',text)
        self.assertNotIn("https://tracker.example",text)
        self.assertNotIn('src="images/not-registered.png"',text)
        self.assertIn("未登记或已变更",text)

    def test_preview_version_platform_and_query_paths_rejected(self):
        item,_,_=self.artifacts("mp")
        path=self.preview_url(item)
        for url in (path+'?path=C:/secret',path.replace('/1/mp','/2/mp'),path.replace('/mp','/xhs'),path.replace('/mp','/html')):
            self.assertEqual(self.client.get(url).status_code,404)

    def test_security_headers_sandbox_and_frontend_sinks(self):
        page=self.client.get("/")
        text=page.get_data(as_text=True)
        self.assertIn('sandbox="allow-same-origin"',text)
        self.assertNotIn("allow-scripts",text)
        self.assertIn('title="公众号当前版本只读预览"',text)
        self.assertEqual(page.headers["X-Content-Type-Options"],"nosniff")
        self.assertEqual(page.headers["Cache-Control"],"no-store")
        js=self.client.get("/static/app.js").get_data(as_text=True)
        self.assertNotIn("innerHTML",js)
        self.assertNotIn("eval(",js)
        self.assertIn("textContent",js)
        self.assertIn("ArrowRight",js)
        item,_,_=self.artifacts("mp")
        self.assertIn("sandbox allow-same-origin",self.client.get(self.preview_url(item)).headers["Content-Security-Policy"])

    def test_static_unknown_paths_and_errors_single_json(self):
        for path in ("/static/../../workspace.py","/static/no.js","/oldpreview",f"/api/content/{DATE}/bad-id","/api/no-route"):
            response=self.client.get(path)
            self.assertEqual(response.status_code,404)
            self.assertFalse(response.json["ok"])
            self.assertEqual(set(response.json),{"ok","reason_code","message"})

    def test_internal_errors_hide_trace_and_private_paths(self):
        with patch.object(self.ws,"topics",side_effect=RuntimeError("SDK trace C:/Users/private/secret")):
            response=self.client.get("/api/topics")
        self.assertEqual(response.status_code,500)
        self.assertNotIn("private",response.get_data(as_text=True))
        self.assertEqual(response.json["reason_code"],"internal_error")

    def test_receipts_expose_outcomes_not_prompts_or_private_paths(self):
        item,root,_=self.artifacts()
        atomic_json(root/"receipts"/"one.json",{"stage":"write","outcome":"succeeded","time":"2026-11-10T00:00:00Z","revision":1,"prompt":"private task prompt","stdout":"C:/Users/private/secret","outputs":{"C:/private/file":"hash"},"usage":{"path":"private"}})
        response=self.client.get(f"/api/content/{DATE}/{item['id']}")
        receipt=response.json["detail"]["receipts"][0]
        self.assertEqual(receipt["outcome"],"succeeded")
        self.assertNotIn("private",json.dumps(receipt))
        self.assertNotIn("prompt",receipt)
        self.assertNotIn("outputs",receipt)

    def test_linked_template_and_static_assets_rejected(self):
        original=Path.is_symlink
        def linked(path):
            if path.name in {"app.js","index.html"}:
                return True
            return original(path)
        with patch.object(Path,"is_symlink",linked):
            self.assertIn(self.client.get("/").status_code,(400,404))
            self.assertEqual(self.client.get("/static/app.js").status_code,404)

    def test_linked_revision_asset_parent_rejected(self):
        item,root,_=self.artifacts()
        original=Path.is_symlink
        def linked(path):
            return path==root/"images" or original(path)
        with patch.object(Path,"is_symlink",linked):
            self.assertEqual(self.client.get(self.asset_url(item)).status_code,404)

    def test_jpeg_and_webp_use_verified_mime_not_filename(self):
        item,root,_=self.artifacts()
        for format,mime in (("JPEG","image/jpeg"),("WEBP","image/webp")):
            stream=io.BytesIO()
            Image.new("RGB",(30,40)).save(stream,format=format)
            data=stream.getvalue()
            (root/"images/one.png").write_bytes(data)
            atomic_json(root/"assets.json",{"items":[{"path":"images/one.png","sha256":hashlib.sha256(data).hexdigest()}]})
            response=self.client.get(self.asset_url(item))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.mimetype,mime)

    def test_bounded_inbox_and_topics(self):
        items=[{"id":str(index),"status":"queued","created_at":str(index)} for index in range(125)]
        with patch.object(self.ws,"records",return_value=items):
            response=self.client.get("/api/inbox")
        self.assertEqual(len(response.json["items"]),100)
        self.assertTrue(response.json["truncated"])
        with patch.object(self.ws,"topics",return_value=items):
            response=self.client.get("/api/topics")
        self.assertEqual(len(response.json["items"]),100)
        self.assertTrue(response.json["truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
