"""Shared simulated host fixtures; never a production runtime or acceptance claim."""
import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apps.workbench.jobs import Jobs
from apps.workbench.workspace import atomic_json, load_json, safe_path, file_hash

URL = "https://example.org/product"
TOPIC = {"title": "功能说明", "summary": "公开资料提到本地运行。", "sources": [{"url": URL, "title": "来源"}], "evidence": [{"id": "e1", "claim": "支持本地运行", "source_url": URL, "excerpt": "支持本地运行"}]}
DRAFT = {"title": "功能观察", "body": "支持本地运行", "source_urls": [URL], "claims": [{"text": "支持本地运行", "evidence_ids": ["e1"]}], "humanizer_notes": "删除无证据保证"}
SETTINGS = {"timezone": "Asia/Shanghai", "planning_time": "21:17", "production_time": "08:07", "daily_counts": {"xhs": 1, "mp": 1}, "auto_plan": False, "auto_produce": False, "execution_mode": "external", "host": "codex", "publish_enabled": False}


class TestOnlyRuntimeBlocked(ValueError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


class FakeRunner:
    def __init__(self, common):
        self.common = common
        self.calls = []
        self.failure = None
        self.score = 4

    def run(self, stage, package, attempt, timeout):
        self.calls.append(stage)
        if self.failure == stage:
            raise TestOnlyRuntimeBlocked("permission_blocked")
        if stage == "research":
            atomic_json(attempt / "sources.json", {"materials": [{"url": URL, "title": "source", "text": "支持本地运行"}]})
            return copy.deepcopy(TOPIC)
        if stage == "write":
            return copy.deepcopy(DRAFT)
        if stage == "assets":
            from PIL import Image, ImageOps
            path = attempt / "images/photo.png"
            path.parent.mkdir()
            with Image.new("RGB", (640, 480), "blue") as generated:
                generated.save(path)
            with Image.open(path) as image, ImageOps.exif_transpose(image) as oriented, oriented.convert("L") as gray, gray.resize((9, 8), Image.Resampling.LANCZOS) as small:
                pixels = list(small.getdata())
            value = 0
            for row in range(8):
                for col in range(8):
                    value = (value << 1) | (pixels[row*9+col] > pixels[row*9+col+1])
            return {"items": [{"id": "a"*32, "kind": "photo", "path": "images/photo.png", "source_url": URL, "rights_note": "测试夹具，不是生产版权", "sha256": file_hash(path), "width": 640, "height": 480, "format": "PNG", "dhash": f"{value:016x}", "template": None}]}
        return {"decision": "approved", "feedback": "模拟审核非实际视觉", "scores": {key: self.score for key in self.common.DIMENSIONS}, "facts": {"supported": True, "notes": "模拟"}, "images": [{"id": item["id"], "sha256": item["sha256"], "approved": True, "observation": "模拟读图非真实视觉"} for item in package["assets"]["items"]]}


class TestJobs(Jobs):
    """Drive fake outputs through inherited real prepare/submit; no alternate business logic."""
    def __init__(self, workspace, skills_root, runner):
        super().__init__(workspace, skills_root)
        self.runner = runner
        self._tick_lock = threading.Lock()

    def _fake(self, role, date=None, content_id=None, input_id=None):
        handoff = self.prepare(role, date, 'fake-' + role + '-' + uuid.uuid4().hex, 'test', content_id, input_id)
        if handoff.get('state') != 'ready':
            return handoff
        directory = Path(handoff['output_dir'])
        package = load_json(Path(handoff['package_path']))
        try:
            result = self.runner.run(role, package, directory, package['policy']['runtime'][role])
        except (ValueError, OSError) as exc:
            if getattr(exc, 'reason_code', '') == 'process_cleanup_unconfirmed':
                raise
            result = {'outcome': 'needs_input', 'reason_code': getattr(exc, 'reason_code', 'runner_failed'), 'message': 'Fake host could not complete this attempt'}
        atomic_json(directory / 'result.json', result)
        return self.submit(role, handoff['attempt'], handoff['token'])

    def research(self, input_id):
        return self._fake('research', input_id=input_id)

    def produce(self, date, content_id):
        if self.workspace.content(date, content_id)['status'] != 'queued':
            self.workspace.path(f'.local/requests/{content_id}.json').unlink(missing_ok=True)
            return {'ok': True, 'state': 'idle'}
        self.request(date, content_id)
        while True:
            item = self.workspace.content(date, content_id)
            if item['status'] == 'ready':
                return {'ok': True, 'content_id': content_id, 'publish_enabled': False}
            root = self.workspace.revision_dir(date, content_id, item['revision'])
            state_path = root / 'progress.json'
            phase = load_json(state_path)['phase'] if state_path.exists() else 'write'
            try:
                result = self._fake('review' if phase == 'finalize' else phase, date, content_id)
            except (ValueError, OSError) as exc:
                self.workspace.set_status(date, content_id, item['revision'], 'needs_input', getattr(exc, 'reason_code', 'packet_validation_failed'))
                return {'ok': False, 'reason_code': getattr(exc, 'reason_code', 'packet_validation_failed')}
            if not result['ok'] or result.get('state') == 'busy':
                return result

    def tick(self, now=None, manual=False):
        if not self._tick_lock.acquire(blocking=False):
            return {'ok': True, 'state': 'busy'}
        try:
            settings = self.workspace.settings()
            now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(settings['timezone']))
            date = now.date().isoformat()
            tomorrow = (now.date() + timedelta(days=1)).isoformat()
            if settings['auto_plan'] and now.strftime('%H:%M') >= settings['planning_time'] and not self.workspace.plan(tomorrow).get('planned_at'):
                self.workspace.generate_plan(tomorrow)
            requests = self.workspace.path('.local/requests')
            if requests.exists():
                for path in sorted(requests.glob('*.json')):
                    request = load_json(safe_path(requests, path.name))
                    item = self.workspace.content(request['date'], request['content_id'])
                    if item['revision'] != request['revision'] or item['status'] != 'queued':
                        path.unlink()
                        continue
                    return self.produce(request['date'], request['content_id'])
            if settings['auto_produce']:
                for item in self.workspace.plan(date)['items']:
                    if item['status'] == 'queued' and now.strftime('%H:%M') >= item['production_time']:
                        return self.produce(date, item['id'])
            for value in self.workspace.records('library/inbox', 'input.json'):
                if value['status'] == 'queued':
                    return self.research(value['id'])
            return {'ok': True, 'state': 'idle'}
        finally:
            self._tick_lock.release()
