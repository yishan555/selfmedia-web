"""File repository: versioned inputs, final receipts, and single-owner work."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


class WorkspaceError(ValueError):
    pass


class Conflict(WorkspaceError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceError("duplicate JSON key")
        result[key] = value
    return result


def load_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 1_000_000:
        raise WorkspaceError("JSON file exceeds byte limit")
    def reject(value):
        raise WorkspaceError("nonfinite JSON")
    result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject)
    if type(result) is not dict:
        raise WorkspaceError("JSON must be an object")
    return result


def safe_path(root, relative):
    root = Path(root).resolve()
    if type(relative) is not str or not relative or "\\" in relative or ":" in relative:
        raise WorkspaceError("expected package-relative POSIX path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or Path(relative).is_absolute():
        raise WorkspaceError("unsafe relative path")
    path = root.joinpath(*parts)
    if path.resolve() != path or not path.resolve().is_relative_to(root):
        raise WorkspaceError("linked or escaping path")
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise WorkspaceError("linked path")
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex[:10])
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical(value).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def identity(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise WorkspaceError("invalid stable ID")
    return value


def day(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise WorkspaceError("expected explicit YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Workspace:
    def __init__(self, root, clock=timestamp):
        original = Path(root).absolute()
        if original != original.resolve():
            raise WorkspaceError("workspace cannot contain links")
        self.root = original
        self.clock = clock
        self._mutex = threading.RLock()

    def path(self, relative):
        return safe_path(self.root, relative)

    def initialize(self, settings):
        if self.root.exists() and not self.root.is_dir():
            raise WorkspaceError("workspace must be a directory")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.transaction():
            path = self.path("config/settings.json")
            if not path.exists():
                self.validate_settings(settings)
                atomic_json(path, {"schema_version": 1, "revision": 1, **settings})
        return self.settings()

    @contextlib.contextmanager
    def transaction(self):
        """Short mutation lock, never held during an Agent session."""
        with self._mutex:
            lock = self.path(".local/mutation.lock")
            lock.parent.mkdir(parents=True, exist_ok=True)
            try:
                with lock.open("x", encoding="utf-8") as stream:
                    stream.write(canonical({"pid": os.getpid(), "created_at": self.clock()}))
            except FileExistsError as exc:
                raise Conflict("mutation in progress or interrupted; inspect lock") from exc
            try:
                yield
            finally:
                lock.unlink()

    @contextlib.contextmanager
    def claim(self, name):
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise WorkspaceError("invalid claim name")
        path = self.path(".local/claims/" + name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(canonical({"token": token, "pid": os.getpid(), "created_at": self.clock()}))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise Conflict("running work or interrupted recovery required") from exc
        release = True
        try:
            yield token
        except BaseException as exc:
            if getattr(exc, "reason_code", None) == "process_cleanup_unconfirmed":
                release = False
            raise
        finally:
            if release and path.exists() and load_json(path).get("token") == token:
                path.unlink()

    def validate_settings(self, settings):
        allowed = {"timezone", "planning_time", "production_time", "daily_counts", "auto_plan", "auto_produce", "publish_enabled", "schema_version", "revision", "execution_mode", "host", "archive_repo", "archive_remote"}
        if type(settings) is not dict or set(settings) - allowed or settings.get("publish_enabled") is not False:
            raise WorkspaceError("publication disabled; invalid settings")
        from zoneinfo import ZoneInfo
        ZoneInfo(settings["timezone"])
        for key in ("planning_time", "production_time"):
            if type(settings[key]) is not str or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", settings[key]):
                raise WorkspaceError("invalid configured time")
        counts = settings["daily_counts"]
        if type(counts) is not dict or set(counts) != {"xhs", "mp"} or any(type(value) is not int or not 0 <= value <= 20 for value in counts.values()):
            raise WorkspaceError("invalid daily counts")
        if any(type(settings[key]) is not bool for key in ("auto_plan", "auto_produce")):
            raise WorkspaceError("invalid automation switches")
        if settings.get("execution_mode", "external") != "external" or settings.get("host", "codex") not in ("codex", "claude", "hermes"):
            raise WorkspaceError("only external native execution is supported")
        if "archive_repo" in settings and (type(settings["archive_repo"]) is not str or not settings["archive_repo"].strip() or len(settings["archive_repo"]) > 1000):
            raise WorkspaceError("invalid archive repository path")
        if "archive_remote" in settings and (type(settings["archive_remote"]) is not str or not settings["archive_remote"].startswith(("https://", "http://")) or len(settings["archive_remote"]) > 1000):
            raise WorkspaceError("invalid archive remote")

    def settings(self):
        settings = load_json(self.path("config/settings.json"))
        self.validate_settings(settings)
        return {"execution_mode": "external", "host": "codex", **settings}

    def save_settings(self, settings, revision):
        with self.transaction():
            current = self.settings()
            if type(revision) is not int or current["revision"] != revision:
                raise Conflict("settings changed")
            self.validate_settings(settings)
            result = {**settings, "schema_version": 1, "revision": revision + 1}
            atomic_json(self.path("config/settings.json"), result)
            return result

    def records(self, relative, filename):
        directory = self.path(relative)
        if not directory.exists():
            return []
        result = []
        for path in sorted(directory.glob("*/" + filename)):
            try:
                result.append(load_json(self.path(path.relative_to(self.root).as_posix())))
            except (OSError, ValueError):
                continue
        return result

    def _input_fields(self, value):
        if set(value) - {"theme", "url", "reference", "notes"}:
            raise WorkspaceError("unknown input fields")
        fields = {key: value.get(key, "").strip() if type(value.get(key, "")) is str else None for key in ("theme", "url", "reference", "notes")}
        if any(text is None or len(text) > 100_000 for text in fields.values()) or not any(fields[key] for key in ("theme", "url", "reference")):
            raise WorkspaceError("provide theme, URL, or reference text")
        if fields["url"]:
            from urllib.parse import urlsplit
            if urlsplit(fields["url"]).scheme not in ("https", "http"):
                raise WorkspaceError("expected HTTP(S) source")
        return fields

    def add_input(self, value):
        fields = self._input_fields(value)
        result = {"schema_version": 1, "id": uuid.uuid4().hex, "revision": 1, "status": "queued", "created_at": self.clock(), **fields}
        with self.transaction():
            atomic_json(self.path(f"library/inbox/{result['id']}/input.json"), result)
        return result

    def update_input(self, input_id, value, revision):
        path = self.path(f"library/inbox/{identity(input_id)}/input.json")
        with self.transaction():
            current = load_json(path)
            if current["revision"] != revision or current["status"] == "running" or self.path(f".local/claims/research-{input_id}.json").exists():
                raise Conflict("input changed or already running")
            status = value.pop("status", "queued")
            if status not in ("queued", "archived"):
                raise WorkspaceError("invalid input status")
            fields = self._input_fields({key: value.get(key, current[key]) for key in ("theme", "url", "reference", "notes")})
            if set(value) - set(fields):
                raise WorkspaceError("unknown input fields")
            result = {**current, **fields, "revision": revision + 1, "status": status}
            atomic_json(path, result)
            return result

    def topics(self):
        return self.records("library/topics", "topic.json")

    def topic(self, topic_id):
        return load_json(self.path(f"library/topics/{identity(topic_id)}/topic.json"))

    def update_topic(self, topic_id, fields, revision):
        if set(fields) - {"title", "notes", "status"}:
            raise WorkspaceError("only topic label/notes/archive can be edited; evidence requires research")
        if any(type(value) is not str or len(value) > 10000 for value in fields.values()):
            raise WorkspaceError("invalid topic label")
        if fields.get("status", "ready") not in ("ready", "archived"):
            raise WorkspaceError("invalid topic status")
        with self.transaction():
            topic = self.topic(topic_id)
            if type(revision) is not int or revision != topic.get("revision", 1):
                raise Conflict("topic changed")
            topic.update(fields)
            topic["revision"] = revision + 1
            atomic_json(self.path(f"library/topics/{topic_id}/topic.json"), topic)
            return topic

    def plan(self, date):
        path = self.path(f"{day(date)}/plan.json")
        return load_json(path) if path.exists() else {"schema_version": 1, "date": date, "revision": 0, "items": [], "shortages": {}}

    def add_content(self, date, topic_id, platform, *, manual=True):
        if platform not in ("xhs", "mp"):
            raise WorkspaceError("invalid platform")
        if self.topic(topic_id).get("status") != "ready":
            raise WorkspaceError("topic is not evidence-ready")
        with self.transaction():
            plan = self.plan(date)
            item = {"id": uuid.uuid4().hex, "topic_id": topic_id, "platform": platform, "revision": 1, "status": "queued", "manual": manual, "created_at": self.clock(), "production_time": self.settings()["production_time"]}
            plan["items"].append(item)
            plan["revision"] += 1
            atomic_json(self.path(f"{date}/plan.json"), plan)
        return item

    def generate_plan(self, date=None, *, automatic=False):
        """Fill only missing positions from evidence-ready inventory."""
        ready = sorted([topic for topic in self.topics() if topic.get("status") == "ready"], key=lambda topic: topic.get("created_at", ""), reverse=True)
        with self.transaction():
            settings = self.settings()
            if automatic:
                from zoneinfo import ZoneInfo
                now = datetime.fromisoformat(self.clock()).astimezone(ZoneInfo(settings["timezone"]))
                target = (now.date() + timedelta(days=1)).isoformat()
                if date is not None and date != target:
                    raise WorkspaceError("automatic planning only targets workspace tomorrow")
                date = target
                if not settings["auto_plan"]:
                    return {"state": "idle", "reason_code": "auto_plan_disabled", "date": date}
                if now.strftime("%H:%M") < settings["planning_time"]:
                    return {"state": "idle", "reason_code": "planning_not_due", "date": date}
            plan = self.plan(date)
            if automatic and plan.get("planned_at"):
                return {"state": "idle", "reason_code": "already_planned", "date": date}
            used = {item["topic_id"] for item in plan["items"]}
            for platform, target in settings["daily_counts"].items():
                count = sum(item["platform"] == platform and item["status"] != "cancelled" for item in plan["items"])
                for topic in ready:
                    if count >= target:
                        break
                    if topic["id"] in used:
                        continue
                    item = {"id": uuid.uuid4().hex, "topic_id": topic["id"], "platform": platform, "revision": 1, "status": "queued", "manual": False, "created_at": self.clock(), "production_time": settings["production_time"], "reason": "最新证据就绪素材；保留人工条目"}
                    plan["items"].append(item)
                    used.add(topic["id"])
                    count += 1
                plan["shortages"][platform] = max(0, target - count)
            plan["revision"] += 1
            plan["planned_at"] = self.clock()
            atomic_json(self.path(f"{date}/plan.json"), plan)
            return plan

    def content(self, date, content_id):
        for item in self.plan(date)["items"]:
            if item["id"] == identity(content_id):
                return item
        raise WorkspaceError("content not found")

    def change_content(self, date, content_id, fields, revision):
        if set(fields) - {"topic_id", "platform", "production_time", "status"}:
            raise WorkspaceError("unknown content fields")
        with self.transaction():
            plan = self.plan(date)
            item = next((item for item in plan["items"] if item["id"] == identity(content_id)), None)
            if item is None or item["revision"] != revision:
                raise Conflict("content revision changed")
            if item.get("started_at") or self.path(f".local/claims/content-{content_id}.json").exists():
                raise Conflict("started input frozen; create a revision")
            if "topic_id" in fields and self.topic(fields["topic_id"]).get("status") != "ready":
                raise WorkspaceError("topic not ready")
            if "platform" in fields and fields["platform"] not in ("xhs", "mp"):
                raise WorkspaceError("invalid platform")
            if "production_time" in fields and (type(fields["production_time"]) is not str or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", fields["production_time"])):
                raise WorkspaceError("invalid production time")
            if "status" in fields and fields["status"] not in ("queued", "cancelled"):
                raise WorkspaceError("invalid status")
            item.update(fields)
            item["revision"] += 1
            item["manual"] = True
            plan["revision"] += 1
            atomic_json(self.path(f"{date}/plan.json"), plan)
            return item

    def revise(self, date, content_id, feedback, revision):
        if type(feedback) is not str or not feedback.strip() or len(feedback) > 10000:
            raise WorkspaceError("provide bounded revision feedback")
        with self.transaction():
            plan = self.plan(date)
            item = next((item for item in plan["items"] if item["id"] == identity(content_id)), None)
            if item is None or item["revision"] != revision or item["status"] == "running" or self.path(f".local/claims/content-{content_id}.json").exists():
                raise Conflict("content changed or running")
            brief = self.revision_dir(date, content_id, revision) / "brief.json"
            if brief.exists():
                limit = load_json(brief)["policy"]["scoring"]["max_iterations"]
                if item.get("quality_rounds", 0) >= limit:
                    raise Conflict("quality budget exhausted; create separate content explicitly")
            item["revision"] += 1
            item["status"] = "queued"
            item["previous_revision"] = item["revision"] - 1
            item["feedback"] = feedback
            item["technical_retries"] = 0
            item.pop("started_at", None)
            plan["revision"] += 1
            atomic_json(self.path(f"{date}/plan.json"), plan)
            return item

    def set_status(self, date, content_id, revision, status, reason=None):
        with self.transaction():
            plan = self.plan(date)
            item = next((item for item in plan["items"] if item["id"] == content_id), None)
            if item is None or item["revision"] != revision:
                raise Conflict("stale attempt cannot commit")
            item["status"] = status
            if status == "running":
                item.setdefault("started_at", self.clock())
            item["reason_code"] = reason
            atomic_json(self.path(f"{date}/plan.json"), plan)

    def set_quality_rounds(self, date, content_id, revision, used):
        with self.transaction():
            plan = self.plan(date)
            item = next(i for i in plan["items"] if i["id"] == content_id)
            if item["revision"] != revision:
                raise Conflict("stale quality attempt")
            item["quality_rounds"] = used
            atomic_json(self.path(f"{date}/plan.json"), plan)

    def revision_dir(self, date, content_id, revision):
        if type(revision) is not int or revision < 1:
            raise WorkspaceError("invalid revision")
        return self.path(f"{day(date)}/contents/{identity(content_id)}/r{revision:04d}")

    def receipts(self, date, item):
        root = self.revision_dir(date, item["id"], item["revision"])
        directory = safe_path(root, "receipts")
        return [load_json(safe_path(directory, path.name)) for path in sorted(directory.glob("*.json"))] if directory.exists() else []

    def approval_current(self, date, item):
        root = self.revision_dir(date, item["id"], item["revision"])
        try:
            current = self.content(date, item["id"])
            if current["revision"] != item["revision"] or current["status"] != "ready":
                return False
            approval = load_json(safe_path(root, "approval.json"))
            if approval.get("content_id") != item["id"] or approval.get("revision") != item["revision"]:
                return False
            required = {"brief.json", "sources.json", "draft.json", "assets.json", "review.json", "article.md"}
            if not required.issubset(approval["files"]):
                return False
            for relative, expected in approval["files"].items():
                if file_hash(safe_path(root, relative)) != expected:
                    return False
            return approval.get("passed") is True and bool(approval["files"])
        except (OSError, ValueError, KeyError):
            return False

    def detail(self, date, content_id):
        item = self.content(date, content_id)
        root = self.revision_dir(date, item["id"], item["revision"])
        artifacts = {}
        for name in ("draft", "assets", "review", "brief"):
            path = safe_path(root, name + ".json")
            if path.exists():
                artifacts[name] = load_json(path)
        return {"item": item, "artifacts": artifacts, "receipts": self.receipts(date, item), "approval_current": self.approval_current(date, item), "publish_enabled": False}

    def export(self, date, content_id, target):
        item = self.content(date, content_id)
        if not self.approval_current(date, item):
            raise WorkspaceError("current revision is not approved")
        target = Path(target).absolute()
        if target != target.resolve() or target.exists():
            raise WorkspaceError("export target must be new and unlinked")
        root = self.revision_dir(date, item["id"], item["revision"])
        approval = load_json(safe_path(root, "approval.json"))
        target.mkdir(parents=True)
        files = {**approval["files"], "approval.json": file_hash(root / "approval.json")}
        for relative, expected in files.items():
            source = safe_path(root, relative)
            if file_hash(source) != expected:
                raise WorkspaceError("file changed during export")
            destination = safe_path(target, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        manifest = {"schema_version": 1, "content_id": item["id"], "revision": item["revision"], "platform": item["platform"], "date": date, "files": files, "publish_enabled": False}
        atomic_json(target / "delivery.json", manifest)
        verify_delivery(target)
        return manifest


def verify_delivery(root):
    root = Path(root).resolve()
    manifest = load_json(safe_path(root, "delivery.json"))
    if manifest.get("schema_version") != 1 or manifest.get("publish_enabled") is not False:
        raise WorkspaceError("invalid delivery version")
    for relative, expected in manifest["files"].items():
        if file_hash(safe_path(root, relative)) != expected:
            raise WorkspaceError("delivery hash mismatch")
    approval = load_json(safe_path(root, "approval.json"))
    if approval.get("passed") is not True or approval.get("content_id") != manifest["content_id"] or approval.get("revision") != manifest["revision"]:
        raise WorkspaceError("delivery approval mismatch")
    if not set(approval["files"]).issubset(manifest["files"]):
        raise WorkspaceError("incomplete delivery")
    for relative, expected in approval["files"].items():
        if manifest["files"][relative] != expected:
            raise WorkspaceError("approval hash mismatch")
    return manifest
