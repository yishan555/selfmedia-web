"""Loopback-only web adapter. Requests enqueue work; never execute an agent."""

import hashlib
import html
import io
import json
import re
import secrets
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import markdown
from flask import Flask, jsonify, request, send_file, url_for, render_template_string
from PIL import Image
from werkzeug.exceptions import HTTPException

from .workspace import Conflict, WorkspaceError, day, identity, load_json, safe_path
from .automation import task_templates
from .archive import archive_status

SETTINGS_FIELDS = {"timezone", "planning_time", "production_time", "daily_counts", "auto_plan", "auto_produce"}
INPUT_FIELDS = {"theme", "url", "reference", "notes"}
CONTENT_FIELDS = {"topic_id", "platform", "production_time", "status"}
PAGE_LIMIT = 100
IMAGE_LIMIT = 20_000_000


class WebError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _object(value, allowed):
    if type(value) is not dict or set(value) - allowed:
        raise WebError("invalid_fields", "请求含未知字段或格式不正确。")
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        raise WebError("invalid_revision", "请提供当前版本号。")
    return value


def _body(allowed):
    if not request.is_json:
        raise WebError("json_required", "请使用 JSON 请求。", 415)
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def reject(value):
        raise ValueError("nonfinite JSON")
    try:
        value = json.loads(request.get_data(), object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeError):
        raise WebError("invalid_json", "JSON 格式不正确。") from None
    return _object(value, allowed)


def _local_host(value):
    match = re.fullmatch(r"(?:localhost|127\.0\.0\.1)(?::([0-9]{1,5}))?", value)
    return bool(match and (match[1] is None or 1 <= int(match[1]) <= 65535))


class SafeMarkdown(HTMLParser):
    """Rebuild generated Markdown with a small tag/attribute allowlist."""
    tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "ul", "ol", "li", "blockquote", "pre", "code", "br", "hr", "a", "img", "table", "thead", "tbody", "tr", "th", "td"}
    void = {"img", "br", "hr"}

    def __init__(self, image_url):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.image_url = image_url

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            return
        values = dict(attrs)
        attributes = ""
        if tag == "img":
            src = self.image_url(values.get("src", ""))
            if not src:
                self.parts.append('<span class="missing-image">[未登记或已变更的图片]</span>')
                return
            attributes = ' src="' + html.escape(src, quote=True) + '" alt="' + html.escape(values.get("alt", "正文配图"), quote=True) + '" loading="lazy"'
        elif tag == "a":
            # Keep link text, not external navigation/tracking or executable URLs.
            return
        self.parts.append("<" + tag + attributes + ">")

    def handle_endtag(self, tag):
        if tag in self.tags - self.void - {"a"}:
            self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(html.escape(data))

    def result(self):
        return "".join(self.parts)


PREVIEW_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#fff;color:#252824;font:16px/1.8 -apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;overflow-wrap:anywhere}article{padding:24px 20px}h1{font-size:24px;line-height:1.45;margin:0 0 18px}h2{font-size:20px}h3{font-size:18px}img{display:block;width:100%;height:auto;margin:18px 0}pre{white-space:pre-wrap;background:#f3f4ef;padding:12px}blockquote{margin:20px 0;border-left:3px solid #66816a;padding-left:14px;color:#5b625c}table{width:100%;border-collapse:collapse;font-size:13px}td,th{border:1px solid #ddd;padding:6px}.notice{font-size:11px;letter-spacing:.04em;color:#71776f;border-bottom:1px solid #e7e9e3;padding:10px 20px}.body{white-space:pre-wrap}.carousel{display:flex;overflow-x:auto;scroll-snap-type:x mandatory}.slide{min-width:100%;scroll-snap-align:start;aspect-ratio:3/4;background:#f2f3ee;display:flex;align-items:center}.slide img{margin:0;height:100%;object-fit:contain}.arrows{display:flex;justify-content:center;gap:14px;padding:8px}.arrows a{color:#386345;padding:8px;text-decoration:none}.missing-image{color:#84613d;font-size:13px}small{color:#6b746c}
"""


def create_app(workspace, jobs=None):
    app = Flask(__name__, static_folder=None)
    app.url_map.merge_slashes = False
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    token = secrets.token_urlsafe(32)

    @app.before_request
    def protect_request():
        if not _local_host(request.host):
            raise WebError("invalid_host", "仅允许本机访问。", 403)
        origin = request.headers.get("Origin")
        expected = request.host_url.rstrip("/")
        if origin is not None and origin != expected:
            raise WebError("invalid_origin", "请求来源与本机页面不一致。", 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin != expected:
                raise WebError("invalid_origin", "写入须来自当前页面。", 403)
            supplied = request.headers.get("X-Workbench-Token", "")
            if not supplied.isascii() or not secrets.compare_digest(supplied, token):
                raise WebError("invalid_token", "页面凭证失效，请刷新后重试。", 403)

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
        if request.path.startswith("/preview/"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox allow-same-origin"
        return response

    @app.errorhandler(Exception)
    def failure(error):
        if isinstance(error, WebError):
            code, message, status = error.code, error.message, error.status
        elif isinstance(error, Conflict):
            code, message, status = "revision_conflict", "内容已变更或正在处理，请刷新后重试。", 409
        elif isinstance(error, FileNotFoundError):
            code, message, status = "not_found", "内容或配置尚不存在。", 404
        elif isinstance(error, WorkspaceError):
            code, message, status = "invalid_request", "字段、状态或资源路径不符合要求。", 400
        elif isinstance(error, HTTPException):
            status = error.code
            code = "payload_too_large" if status == 413 else "not_found" if status == 404 else "http_error"
            message = "请求超过 1 MB。" if status == 413 else "请求地址或格式不正确。"
        elif isinstance(error, (ValueError, KeyError, TypeError)):
            code, message, status = "invalid_request", "字段格式不正确。", 400
        else:
            code, message, status = "internal_error", "操作未完成，请稍后重试。", 500
        return jsonify(ok=False, reason_code=code, message=message), status

    def success(**values):
        return jsonify(ok=True, **values)

    def require_jobs():
        if jobs is None:
            raise WebError("worker_unavailable", "生产队列尚未连接，未启动任何任务。", 503)

    def get_item(date, content_id):
        try:
            day(date)
            identity(content_id)
            return workspace.content(date, content_id)
        except (WorkspaceError, ValueError, FileNotFoundError):
            raise WebError("not_found", "未找到该内容。", 404) from None

    def listed_plan(date):
        plan = workspace.plan(day(date))
        items = []
        for original in plan["items"]:
            item = dict(original)
            try:
                item["topic_title"] = workspace.topic(item["topic_id"]).get("title", "未命名选题")
            except (OSError, WorkspaceError):
                item["topic_title"] = "选题不可用"
            item["approval_current"] = workspace.approval_current(date, item)
            items.append(item)
        return {**plan, "items": items}

    def registered_assets(date, content_id, revision):
        get_item(date, content_id)
        root = workspace.revision_dir(date, content_id, revision)
        manifest = load_json(safe_path(root, "assets.json"))
        items = manifest.get("items", [])
        if type(items) is not list:
            raise WebError("not_found", "图片清单不可用。", 404)
        return root, items

    def asset_bytes(date, content_id, revision, relative):
        try:
            root, items = registered_assets(date, content_id, revision)
            entry = next((entry for entry in items if type(entry) is dict and entry.get("path") == relative), None)
            if entry is None or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", ""))):
                raise ValueError("unregistered")
            # Only the manifest-owning revision can supply an asset.
            path = safe_path(root, relative)
            if not path.is_file() or path.stat().st_size > IMAGE_LIMIT:
                raise ValueError("not a bounded file")
            data = path.read_bytes()
            if len(data) > IMAGE_LIMIT or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("hash mismatch")
            with Image.open(io.BytesIO(data)) as image:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
                if mime is None or image.width * image.height > 40_000_000:
                    raise ValueError("unsupported image")
                image.verify()
            return data, mime
        except (OSError, ValueError, KeyError, TypeError, Image.DecompressionBombError):
            raise WebError("not_found", "图片未登记、已变更或路径不安全。", 404) from None

    def image_links(date, item):
        links = []
        try:
            _, items = registered_assets(date, item["id"], item["revision"])
        except (OSError, ValueError, WebError):
            return links
        for asset in items[:30]:
            if type(asset) is not dict:
                continue
            relative = asset.get("path", "")
            try:
                asset_bytes(date, item["id"], item["revision"], relative)
            except WebError:
                continue
            links.append({"path": relative, "url": url_for("asset", date=date, content_id=item["id"], revision=item["revision"], relative=relative), "kind": asset.get("kind", "photo")})
        return links

    @app.get("/")
    def index():
        template = safe_path(Path(app.root_path), "templates/index.html").read_text(encoding="utf-8")
        return render_template_string(template, token=token)

    @app.get("/static/<path:relative>")
    def static_asset(relative):
        if relative not in {"app.js", "app.css"}:
            raise WebError("not_found", "资源不存在。", 404)
        try:
            path = safe_path(Path(app.root_path) / "static", relative)
            data = path.read_bytes()
        except (OSError, WorkspaceError):
            raise WebError("not_found", "资源不存在。", 404) from None
        return send_file(io.BytesIO(data), mimetype="text/javascript" if relative.endswith(".js") else "text/css")

    @app.get("/api/settings")
    def get_settings():
        value = workspace.settings()
        return success(settings={key: value[key] for key in SETTINGS_FIELDS | {"revision"}}, publish_enabled=False, runtime={"mode": "external", "host": value.get("host", "codex"), "task_creation": "operator", "native_automation_verified": False})

    @app.get("/api/automations")
    def automations():
        settings = workspace.settings()
        host = settings.get("host", "codex")
        project = Path(__file__).resolve().parents[2]
        explicit = getattr(jobs, "skills_root", None)
        if host == "hermes" and explicit is None:
            raise WebError("profile_required", "Hermes须指定对应profile的技能目录。")
        skills = explicit or project / (".agents" if host == "codex" else ".claude") / "skills"
        return jsonify(task_templates(workspace, project, skills, host))

    @app.get("/api/archive")
    def archive_view():
        settings = workspace.settings()
        return success(archive=archive_status(workspace), config={"repo": settings.get("archive_repo", ""), "remote": settings.get("archive_remote", ""), "read_only_replica": True}, publish_enabled=False)

    @app.get("/api/runtime")
    def runtime_status():
        require_jobs()
        if not callable(getattr(jobs, "status", None)):
            return success(mode="external", attempts=[], native_automation_verified=False)
        result = jobs.status()
        allowed = {"role", "content_id", "input_id", "date", "revision", "status", "state", "reason_code", "created_at", "completed_at", "deadline"}
        attempts = [{key: row[key] for key in allowed if key in row} for row in result.get("attempts", [])[:100]]
        return success(mode="external", attempts=attempts, native_automation_verified=False)

    @app.put("/api/settings")
    def put_settings():
        value = _body(SETTINGS_FIELDS | {"revision"})
        revision = _revision(value.pop("revision", None))
        settings = workspace.save_settings({**workspace.settings(), **value}, revision)
        return success(settings={key: settings[key] for key in SETTINGS_FIELDS | {"revision"}}, publish_enabled=False)

    @app.get("/api/day/<date>")
    def get_day(date):
        plan = listed_plan(date)
        return success(plan=plan, items=plan["items"], publish_enabled=False)

    @app.get("/api/inbox")
    def get_inbox():
        items = workspace.records("library/inbox", "input.json")
        items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return success(items=items[:PAGE_LIMIT], truncated=len(items) > PAGE_LIMIT)

    @app.post("/api/inbox")
    def add_input():
        return success(item=workspace.add_input(_body(INPUT_FIELDS)))

    @app.patch("/api/inbox/<input_id>")
    def edit_input(input_id):
        identity(input_id)
        value = _body({"revision", "fields"})
        fields = _object(value.get("fields"), INPUT_FIELDS | {"status"})
        return success(item=workspace.update_input(input_id, dict(fields), _revision(value.get("revision"))))

    @app.get("/api/topics")
    def get_topics():
        items = workspace.topics()
        query = request.args.get("q", "").strip().casefold()
        status = request.args.get("status", "")
        if query:
            items = [item for item in items if query in (item.get("title", "") + item.get("notes", "") + item.get("research", {}).get("summary", "")).casefold()]
        if status:
            items = [item for item in items if item.get("status") == status]
        items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return success(items=items[:PAGE_LIMIT], truncated=len(items) > PAGE_LIMIT)

    @app.patch("/api/topics/<topic_id>")
    def edit_topic(topic_id):
        identity(topic_id)
        value = _body({"revision", "fields"})
        fields = _object(value.get("fields"), {"title", "notes", "status"})
        return success(item=workspace.update_topic(topic_id, fields, _revision(value.get("revision"))))

    @app.post("/api/produce")
    def produce():
        value = _body({"date", "topic_id", "platform"})
        require_jobs()
        date = day(value.get("date"))
        item = workspace.add_content(date, value.get("topic_id"), value.get("platform"))
        queued = jobs.request(date, item["id"])
        return success(item=item, request=queued)

    @app.post("/api/content/<date>/<content_id>/produce")
    def produce_existing(date, content_id):
        _body(set())
        require_jobs()
        item = get_item(date, content_id)
        return success(item=item, request=jobs.request(date, content_id))

    @app.post("/api/content/<date>/<content_id>/retry")
    def retry(date, content_id):
        value = _body({"revision"})
        require_jobs()
        item = get_item(date, content_id)
        revision = _revision(value.get("revision"))
        if item["revision"] != revision:
            raise Conflict("stale retry")
        return success(item=item, request=jobs.retry(date, content_id, revision))

    @app.patch("/api/content/<date>/<content_id>")
    def edit_content(date, content_id):
        get_item(date, content_id)
        value = _body({"revision", "fields"})
        fields = _object(value.get("fields"), CONTENT_FIELDS)
        if "production_time" in fields and type(fields["production_time"]) is not str:
            raise WebError("invalid_fields", "生产时间须为 HH:MM。")
        return success(item=workspace.change_content(date, content_id, fields, _revision(value.get("revision"))))

    @app.post("/api/content/<date>/<content_id>/revise")
    def revise_content(date, content_id):
        get_item(date, content_id)
        value = _body({"revision", "feedback"})
        return success(item=workspace.revise(date, content_id, value.get("feedback"), _revision(value.get("revision"))))

    @app.get("/api/plan/<date>")
    def get_plan(date):
        return success(plan=listed_plan(date))

    @app.post("/api/plan/<date>")
    def plan_day(date):
        _body(set())
        workspace.generate_plan(day(date))
        return success(plan=listed_plan(date))

    @app.get("/api/content/<date>/<content_id>")
    def detail(date, content_id):
        item = get_item(date, content_id)
        value = workspace.detail(date, content_id)
        # Receipts expose outcome summaries, never prompts, stdout or private paths.
        receipt_fields = {"stage", "role", "ok", "reason_code", "revision", "duration_seconds", "started_at", "completed_at", "outcome", "time", "simulation"}
        value["receipts"] = [{key: entry[key] for key in receipt_fields if key in entry} for entry in value.get("receipts", [])]
        value["asset_urls"] = image_links(date, item)
        value["topic"] = workspace.topic(item["topic_id"])
        return success(detail=value)

    @app.get("/api/asset/<date>/<content_id>/<int:revision>/<path:relative>")
    def asset(date, content_id, revision, relative):
        data, mime = asset_bytes(date, content_id, revision, relative)
        return send_file(io.BytesIO(data), mimetype=mime)

    @app.get("/preview/<date>/<content_id>/<int:revision>/<platform>")
    def preview(date, content_id, revision, platform):
        if platform not in {"xhs", "mp"} or request.args:
            raise WebError("not_found", "预览地址不正确。", 404)
        item = get_item(date, content_id)
        if item["revision"] != revision or item["platform"] != platform:
            raise WebError("not_found", "预览版本或平台不匹配。", 404)
        root = workspace.revision_dir(date, content_id, revision)
        draft = load_json(safe_path(root, "draft.json"))
        title = html.escape(str(draft.get("title", "未命名内容")))
        body = str(draft.get("body", draft.get("markdown", "")))
        links = image_links(date, item)
        if platform == "mp":
            lookup = {link["path"]: link["url"] for link in links}
            def image_url(source):
                source = unquote(html.unescape(source))
                if urlsplit(source).scheme or source.startswith(("/", "\\")):
                    return None
                return lookup.get(source)
            parser = SafeMarkdown(image_url)
            # Raw HTML is escaped before Markdown; generated HTML is allowlisted again.
            parser.feed(markdown.markdown(html.escape(body, quote=False), extensions=["tables", "fenced_code"]))
            article = '<article><h1>' + title + '</h1>' + parser.result() + '</article>'
        else:
            slides = ''.join('<div class="slide" id="image-' + str(i) + '"><img src="' + html.escape(link["url"], quote=True) + '" alt="配图 ' + str(i + 1) + '"></div>' for i, link in enumerate(links))
            arrows = ''.join('<a href="#image-' + str(i) + '" aria-label="查看第 ' + str(i + 1) + ' 张图片">' + str(i + 1) + '</a>' for i in range(len(links)))
            article = ('<div class="carousel" aria-label="3:4 配图轮播">' + slides + '</div><nav class="arrows">' + arrows + '</nav><article><h1>' + title + '</h1><div class="body">' + html.escape(body) + '</div></article>')
        label = "小红书图文" if platform == "xhs" else "公众号文章"
        return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + label + ' · 只读预览</title><style>' + PREVIEW_STYLE + '</style></head><body><div class="notice">' + label + ' · 内容排版预览，非平台像素复刻 · 未发布</div>' + article + '</body></html>'

    return app
