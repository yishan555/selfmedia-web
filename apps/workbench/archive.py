"""Cross-machine selfmedia-web archive and three-way content sync."""

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .workspace import Conflict, WorkspaceError, atomic_json, canonical, file_hash, load_json, safe_path

DEFAULT_REMOTE = "https://github.com/yishan555/selfmedia-web.git"
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLAN_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}/plan\.json$")
STABLE_ID = re.compile(r"^[a-f0-9]{32}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
SOURCE_DIRS = ("apps/workbench", "skills/content-engine", "tools/skill-packager", "tests", "references")
SOURCE_FILES = ("setup.py", "requirements.txt", "config/topics.yaml")
IGNORE_DIRS = {".git", ".local", ".venv", "venv", "node_modules", "__pycache__", "tmp", "portable-build"}
IGNORE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", ".content-engine-install.lock"}
HOSTS = ("codex", "claude", "hermes")
MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pous]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"(?i)\bauthorization\s*:\s*bearer\s+\S{16,}"),
)
MISSING = object()


class ArchiveError(WorkspaceError):
    def __init__(self, message, reason_code="archive_failed"):
        super().__init__(message)
        self.reason_code = reason_code


def canonical_remote(value):
    value = str(value).strip().lower()
    value = re.sub(r"^git@github\.com:", "https://github.com/", value)
    value = value.rstrip("/").removesuffix(".git")
    return value


def is_managed(relative):
    if relative == "config/settings.json":
        return True
    if relative.startswith("library/"):
        return True
    parts = relative.split("/")
    return len(parts) >= 2 and bool(DATE_DIR.fullmatch(parts[0]))


def is_allowed_commit_path(relative):
    if relative in {".gitignore", ".gitattributes", "README.md", "open-workbench.ps1", "open-workbench.sh", "setup.py", "requirements.txt", "config/topics.yaml"}:
        return True
    if is_managed(relative):
        return True
    return any(relative.startswith(prefix) for prefix in ("apps/", "skills/", "tools/", "tests/", "references/", "bundles/", "_sync/", "_pipeline/", "_logs/"))


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex[:10])
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def json_object(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ArchiveError("duplicate JSON key", "invalid_json")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise ArchiveError("invalid JSON in archive", "invalid_json") from exc
    if type(value) is not dict:
        raise ArchiveError("archive JSON must be an object", "invalid_json")
    return value

def portable_settings_bytes(settings):
    value = dict(settings)
    value.pop("archive_repo", None)
    value.pop("archive_remote", None)
    return (canonical(value) + "\n").encode("utf-8")


class ArchiveSync:
    def __init__(self, workspace, repo, remote=DEFAULT_REMOTE, branch="main", project=None, allow_local_remote=False):
        self.workspace = workspace
        self.repo = Path(repo).absolute()
        self.remote = remote
        self.branch = branch
        self.project = Path(project).absolute() if project else Path(__file__).resolve().parents[2]
        self.allow_local_remote = allow_local_remote
        self._remote_manifest_cache = {}

    def _git_result(self, args, *, cwd=None, input=None):
        return subprocess.run(
            [shutil.which("git") or "git", *args], cwd=str(cwd or self.repo), input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False,
        )

    def _git(self, args, *, input=None):
        result = self._git_result(args, input=input)
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip() or "git command failed"
            raise ArchiveError(message[:2000], "git_failed")
        return result.stdout

    def ensure_repo(self):
        if not self.repo.exists() or not self.repo.is_dir():
            raise ArchiveError("selfmedia-web checkout missing; clone it before syncing", "repo_missing")
        if self.repo.is_symlink():
            raise ArchiveError("archive checkout cannot be a link", "unsafe_repo")
        self.repo = self.repo.resolve()
        top = self._git_result(["rev-parse", "--show-toplevel"])
        if top.returncode or Path(top.stdout.decode("utf-8", "replace").strip()).resolve() != self.repo:
            raise ArchiveError("--repo must be the repository root", "repo_root_required")
        origin = self._git_result(["remote", "get-url", "origin"])
        if origin.returncode:
            raise ArchiveError("archive repository has no origin remote", "remote_missing")
        origin_url = origin.stdout.decode("utf-8", "replace").strip()
        if not origin_url:
            raise ArchiveError("archive origin remote is empty", "remote_missing")
        if not self.allow_local_remote and canonical_remote(origin_url) != canonical_remote(self.remote):
            raise ArchiveError("archive origin does not match selfmedia-web", "unexpected_remote")

    @contextlib.contextmanager
    def _lock(self):
        path = self.workspace.path(".local/archive.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(canonical({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise ArchiveError("archive sync is already running", "archive_busy") from exc
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def _active(self):
        if self.workspace.path(".local/mutation.lock").exists():
            return "mutation"
        claims = self.workspace.path(".local/claims")
        if claims.exists() and any(claims.glob("*.json")):
            return "claims"
        return None

    def _local_sha(self):
        result = self._git_result(["rev-parse", "--verify", "HEAD"])
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else None

    def _remote_sha(self):
        result = self._git_result(["ls-remote", "--heads", "origin", self.branch])
        if result.returncode:
            raise ArchiveError(result.stderr.decode("utf-8", "replace").strip() or "cannot read remote", "git_failed")
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            fields = line.split()
            if len(fields) == 2:
                return fields[0]
        return None

    def _fetch(self):
        remote = self._remote_sha()
        if remote:
            result = self._git_result(["fetch", "--prune", "origin", self.branch])
            if result.returncode:
                raise ArchiveError(result.stderr.decode("utf-8", "replace").strip() or "git fetch failed", "git_failed")
        return remote

    def _merge_base(self, local, remote):
        if not local or not remote:
            return None
        result = self._git_result(["merge-base", local, remote])
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else None

    def _baseline(self, ref):
        if not ref:
            return {}
        result = self._git_result(["show", f"{ref}:_sync/baseline.json"])
        if result.returncode:
            return {}
        value = json_object(result.stdout)
        files = value.get("files")
        if type(files) is not dict:
            raise ArchiveError("invalid archive baseline", "invalid_baseline")
        return {path: digest for path, digest in files.items() if type(path) is str and is_managed(path) and type(digest) is str and HASH.fullmatch(digest)}

    def _iter_files(self, root):
        root = Path(root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS and not (current / name).is_symlink()]
            for name in filenames:
                if name in IGNORE_FILES or name.endswith((".pyc", ".pyo")):
                    continue
                path = current / name
                if path.is_symlink():
                    raise ArchiveError("archive source contains a link", "unsafe_source")
                yield path

    def _manifest_at(self, root):
        root = Path(root)
        result = {}
        settings = root / "config/settings.json"
        if settings.is_file():
            data = portable_settings_bytes(load_json(settings))
            result["config/settings.json"] = hashlib.sha256(data).hexdigest()
        library = root / "library"
        if library.is_dir():
            for path in self._iter_files(library):
                relative = path.relative_to(root).as_posix()
                if is_managed(relative):
                    result[relative] = file_hash(path)
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and not entry.is_symlink() and DATE_DIR.fullmatch(entry.name):
                    for path in self._iter_files(entry):
                        relative = path.relative_to(root).as_posix()
                        if is_managed(relative):
                            result[relative] = file_hash(path)
        return result

    def _workspace_manifest(self):
        return self._manifest_at(self.workspace.root)

    def _repo_manifest(self):
        return self._manifest_at(self.repo)

    def _remote_manifest(self, ref):
        if not ref:
            return {}
        if ref in self._remote_manifest_cache:
            return self._remote_manifest_cache[ref]
        listing = self._git(["ls-tree", "-r", "-z", "--full-tree", ref])
        blobs = {}
        for record in listing.split(b"\0"):
            if not record:
                continue
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, sha = meta.decode("ascii").split(" ")
            path = raw_path.decode("utf-8", "surrogateescape")
            if not is_managed(path):
                continue
            if kind != "blob" or mode not in ("100644", "100755"):
                raise ArchiveError("archive content cannot contain links or submodules", "unsafe_remote")
            blobs[path] = sha
        hashes = self._blob_hashes(blobs.values())
        result = {path: hashes[sha] for path, sha in blobs.items()}
        self._remote_manifest_cache[ref] = result
        return result

    def _blob_hashes(self, shas):
        unique = sorted(set(shas))
        if not unique:
            return {}
        process = self._git_result(["cat-file", "--batch"], input=("\n".join(unique) + "\n").encode("ascii"))
        if process.returncode:
            raise ArchiveError("cannot read archive blobs", "git_failed")
        data = process.stdout
        result = {}
        position = 0
        for sha in unique:
            newline = data.find(b"\n", position)
            if newline < 0:
                raise ArchiveError("invalid git cat-file output", "git_failed")
            header = data[position:newline].decode("ascii", "replace").split()
            if len(header) != 3 or header[1] != "blob":
                raise ArchiveError("archive blob missing", "git_failed")
            size = int(header[2])
            start = newline + 1
            blob = data[start:start + size]
            result[sha] = hashlib.sha256(blob).hexdigest()
            position = start + size + 1
        return result

    def _blob_bytes(self, ref, relative):
        return self._git(["show", f"{ref}:{relative}"])

    def _dirty_paths(self):
        output = self._git(["status", "--porcelain=v1", "-z"])
        records = output.split(b"\0")
        paths = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            status = record[:2].decode("ascii", "replace")
            path = record[3:].decode("utf-8", "surrogateescape")
            paths.append(path)
            if "R" in status or "C" in status:
                index += 1
        return paths

    def _classify(self, baseline, local, remote):
        paths = set(baseline) | set(local) | set(remote)
        imports, exports, merges, conflicts = [], [], [], []
        for path in sorted(paths):
            base = baseline.get(path)
            left = local.get(path, base)
            right = remote.get(path, base)
            if left == base and right == base:
                continue
            if left != base and right == base:
                exports.append(path)
            elif right != base and left == base:
                imports.append(path)
            elif left == right:
                continue
            elif PLAN_FILE.fullmatch(path):
                merges.append(path)
            else:
                conflicts.append(path)
        return imports, exports, merges, conflicts

    def _merge_plan_values(self, base, local, remote):
        if type(base.get("items", [])) is not list or type(local.get("items", [])) is not list or type(remote.get("items", [])) is not list:
            return None, ["items"]
        def items(value):
            result = {}
            for item in value.get("items", []):
                if type(item) is dict and type(item.get("id")) is str:
                    result[item["id"]] = item
            return result
        base_items, local_items, remote_items = items(base), items(local), items(remote)
        merged_items = []
        conflicts = []
        order = [item.get("id") for item in local.get("items", []) if type(item) is dict]
        order += [item_id for item_id in remote_items if item_id not in order]
        for item_id in order:
            if not type(item_id) is str:
                continue
            left, right, original = local_items.get(item_id), remote_items.get(item_id), base_items.get(item_id)
            if left is None:
                merged_items.append(right)
            elif right is None:
                merged_items.append(left)
            elif left == right:
                merged_items.append(left)
            elif left == original:
                merged_items.append(right)
            elif right == original:
                merged_items.append(left)
            else:
                conflicts.append(item_id)
        if conflicts:
            return None, conflicts
        merged = dict(local)
        merged["items"] = merged_items
        revisions = []
        for value in (base, local, remote):
            revision = value.get("revision", 0)
            if type(revision) is int and revision >= 0:
                revisions.append(revision)
        merged["revision"] = (max(revisions) if revisions else 0) + 1
        planned = [value.get("planned_at") for value in (base, local, remote) if type(value.get("planned_at")) is str]
        if planned:
            merged["planned_at"] = max(planned)
        shortages = {}
        for platform in set(base.get("shortages", {})) | set(local.get("shortages", {})) | set(remote.get("shortages", {})):
            original = base.get("shortages", {}).get(platform)
            left = local.get("shortages", {}).get(platform, original)
            right = remote.get("shortages", {}).get(platform, original)
            shortages[platform] = right if left == original else left
        if shortages:
            merged["shortages"] = shortages
        return merged, []

    def _merge_plan(self, path, base_ref, remote_ref):
        local = load_json(self.workspace.path(path))
        remote = json_object(self._blob_bytes(remote_ref, path))
        if base_ref:
            result = self._git_result(["show", f"{base_ref}:{path}"])
            base = json_object(result.stdout) if result.returncode == 0 else {"items": []}
        else:
            base = {"items": []}
        merged, conflicts = self._merge_plan_values(base, local, remote)
        return merged, [path + "#" + item for item in conflicts]

    def status(self):
        self.ensure_repo()
        remote = self._fetch()
        local = self._local_sha()
        base = self._merge_base(local, remote)
        baseline = self._baseline(base)
        left = self._workspace_manifest()
        right = self._remote_manifest(remote) if remote else {}
        imports, exports, merges, conflicts = self._classify(baseline, left, right)
        dirty = self._dirty_paths()
        if conflicts:
            state = "diverged"
        elif dirty and self.workspace.root.resolve() != self.repo:
            state = "repo_dirty"
        elif imports and exports:
            state = "both_changed"
        elif imports:
            state = "remote_ahead"
        elif exports:
            state = "local_ahead"
        elif not local and not remote:
            state = "empty"
        else:
            state = "in_sync"
        return {
            "ok": True, "state": state, "local_commit": local, "remote_commit": remote,
            "baseline_commit": base, "dirty": dirty[:100], "imports": imports[:200],
            "exports": exports[:200], "plan_merges": merges[:50], "conflicts": conflicts[:100],
            "local_files": len(left), "remote_files": len(right), "publish_enabled": False,
        }

    def sync(self, *, push=True, build_bundles=True, allow_active=False, message=None):
        self.ensure_repo()
        with self._lock():
            active = self._active()
            if active and not allow_active:
                return {"ok": False, "state": "busy", "reason_code": "active_work", "active": active, "publish_enabled": False}
            remote = self._fetch()
            local_before = self._local_sha()
            base = self._merge_base(local_before, remote)
            baseline = self._baseline(base)
            if self.workspace.root.resolve() != self.repo:
                dirty = self._dirty_paths()
                if dirty:
                    return {"ok": False, "state": "blocked", "reason_code": "repo_dirty", "paths": dirty[:50], "publish_enabled": False}
            try:
                with self.workspace.transaction():
                    left = self._workspace_manifest()
                    right = self._remote_manifest(remote) if remote else {}
                    imports, exports, merges, conflicts = self._classify(baseline, left, right)
                    if conflicts:
                        return {"ok": False, "state": "conflict", "reason_code": "sync_conflict", "conflicts": conflicts[:100], "publish_enabled": False}
                    merged_values = {}
                    for path in merges:
                        value, merged_conflicts = self._merge_plan(path, base, remote)
                        if merged_conflicts or value is None:
                            return {"ok": False, "state": "conflict", "reason_code": "sync_conflict", "conflicts": merged_conflicts[:100], "publish_enabled": False}
                        merged_values[path] = value
                    for path, value in merged_values.items():
                        atomic_json(self.workspace.path(path), value)
                        left[path] = file_hash(self.workspace.path(path))
                    imported = []
                    for path in imports:
                        current = file_hash(self.workspace.path(path)) if self.workspace.path(path).exists() else None
                        if current != left.get(path):
                            return {"ok": False, "state": "conflict", "reason_code": "sync_conflict", "conflicts": [path], "publish_enabled": False}
                        data = self._blob_bytes(remote, path)
                        if hashlib.sha256(data).hexdigest() != right[path]:
                            raise ArchiveError("remote content changed while syncing", "input_changed")
                        atomic_bytes(self.workspace.path(path), data)
                        left[path] = right[path]
                        imported.append(path)
                    exported = []
                    if self.workspace.root.resolve() != self.repo:
                        for path, digest in sorted(left.items()):
                            source = self.workspace.path(path)
                            target = safe_path(self.repo, path)
                            data = portable_settings_bytes(load_json(source)) if path == "config/settings.json" else source.read_bytes()
                            if target.exists() and target.read_bytes() == data:
                                continue
                            if hashlib.sha256(data).hexdigest() != digest:
                                raise ArchiveError("workspace changed while syncing", "input_changed")
                            atomic_bytes(target, data)
                            exported.append(path)
                    pipeline = self._pipeline_snapshot()
            except Conflict as exc:
                return {"ok": False, "state": "busy", "reason_code": "active_work", "active": str(exc), "publish_enabled": False}
            self._write_generated(build_bundles)
            self._write_pipeline(pipeline)
            self._write_dates(left)
            self._write_manifest(left, pipeline, remote, local_before, build_bundles)
            self._write_log(imported, exported, merges, left, right, remote, local_before)
            self._write_baseline()
            findings = scan_secrets(self.repo)
            if findings:
                return {"ok": False, "state": "blocked", "reason_code": "secret_scan_failed", "findings": findings[:20], "publish_enabled": False}
            commit = self._commit(message)
            if commit is None:
                return {"ok": True, "state": "no_changes", "imported": imported, "exported": exported, "merged": merges, "publish_enabled": False}
            pushed, push_state, push_detail = self._push(push)
            result = {
                "ok": pushed or not push, "state": "synced" if pushed or not push else "local_commit",
                "commit": commit, "pushed": pushed, "push_state": push_state,
                "imported": imported, "exported": exported, "merged": merges,
                "dates": self._date_summary(left), "pipeline_files": len(pipeline),
                "local_files": len(left), "publish_enabled": False,
            }
            if push_detail:
                result["detail"] = push_detail
            atomic_json(self.workspace.path(".local/archive/last-result.json"), result)
            return result

    def _write_generated(self, build_bundles):
        if self.project.resolve() != self.repo.resolve():
            for relative in SOURCE_DIRS:
                source = self.project / relative
                target = self.repo / relative
                if source.is_dir():
                    self._sync_tree(source, target)
            for relative in SOURCE_FILES:
                source = self.project / relative
                if source.is_file():
                    target = safe_path(self.repo, relative)
                    data = source.read_bytes()
                    if not target.exists() or target.read_bytes() != data:
                        atomic_bytes(target, data)
        for name, content in self._root_files().items():
            target = safe_path(self.repo, name)
            data = content.encode("utf-8")
            if not target.exists() or target.read_bytes() != data:
                atomic_bytes(target, data)
        if self.project.resolve() != self.repo.resolve() and build_bundles:
            self._build_bundles()

    def _sync_tree(self, source, target):
        expected = set()
        for path in self._iter_files(source):
            relative = path.relative_to(source).as_posix()
            expected.add(relative)
            destination = safe_path(target, relative)
            data = path.read_bytes()
            if not destination.exists() or destination.read_bytes() != data:
                atomic_bytes(destination, data)
        if target.exists():
            for path in sorted(target.rglob("*"), reverse=True):
                if path.is_file():
                    relative = path.relative_to(target).as_posix()
                    if relative not in expected:
                        path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()

    def _build_bundles(self):
        builder_path = self.project / "tools/skill-packager/build.py"
        if not builder_path.is_file():
            raise ArchiveError("skill builder missing", "bundle_failed")
        spec = importlib.util.spec_from_file_location("_archive_builder", builder_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for host in HOSTS:
            target = self.repo / "bundles" / host
            result = module.build(target, host=host)
            if not result.get("ok"):
                raise ArchiveError(result.get("message", "bundle build failed"), "bundle_failed")

    def _root_files(self):
        return {
            ".gitignore": "\n".join([".local/", "tmp/", ".venv/", "venv/", "node_modules/", "__pycache__/", "*.py[cod]", ".content-engine-install.lock", "Thumbs.db", ".DS_Store", ""]),
            ".gitattributes": "* -text\n",
            "README.md": "# selfmedia-web\n\n这是 hermes-selfmedia 的跨机内容归档与工作台镜像。\n\n```powershell\npwsh -File open-workbench.ps1 -Port 5051\n```\n\n浏览器访问 http://127.0.0.1:5051。服务只绑定回环地址；发布始终关闭。只有生产机写入归档；远端克隆用于查看、跟踪进度和提出修订，代码镜像不要手改。\n\n- `YYYY-MM-DD/`：按日计划、正文、配图、审校、回执和 revision。\n- `library/`：素材收件箱与证据就绪选题。\n- `apps/workbench/`、`skills/content-engine/`、`tools/skill-packager/`：源码镜像，由生产机生成，不在此直接修改。\n- `bundles/codex|claude|hermes/`：预构建的五角色技能包。\n- `_pipeline/`：脱敏后的任务/claim/事件/回执摘要，不含 token、owner 会话或原始工作包。\n- `_logs/`：归档同步日志。\n- `_sync/`：同步基线、日期索引和最近一次同步摘要。\n\n生产机每天写作前和最后一次审校后运行：\n\n```powershell\npy -3.12 -B -m apps.workbench --workspace \"<hermes-selfmedia/workspace>\" archive --repo \"<selfmedia-web>\" --mode sync\n```\n\n同步先拉取远端并做三方比对；远端有新内容则导入，本地有新内容则上传。冲突时停止，不覆盖，不 force push。\n",
            "open-workbench.ps1": "param([int]$Port = 5051)\n$ErrorActionPreference = \"Stop\"\n$root = Split-Path -Parent $MyInvocation.MyCommand.Path\nSet-Location -LiteralPath $root\n$skills = Join-Path $root \"bundles/codex\"\nif (Get-Command py -ErrorAction SilentlyContinue) {\n    & py -3.12 -B -m apps.workbench --workspace $root --host codex --skills $skills serve --port $Port\n} elseif (Get-Command python -ErrorAction SilentlyContinue) {\n    & python -B -m apps.workbench --workspace $root --host codex --skills $skills serve --port $Port\n} else {\n    throw \"Python 3.11+ is required. Install Python or add it to PATH.\"\n}\n",
            "open-workbench.sh": "#!/usr/bin/env bash\nset -euo pipefail\nroot=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\ncd \"$root\"\nexec python3 -B -m apps.workbench --workspace \"$root\" --host codex --skills \"$root/bundles/codex\" serve --port \"${1:-5051}\"\n",
        }
    def _project_commit(self):
        result = self._git_result(["rev-parse", "HEAD"], cwd=self.project)
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else None

    def _pipeline_snapshot(self):
        result = {}
        attempts = self.workspace.path(".local/attempts")
        if attempts.exists():
            for directory in sorted(attempts.iterdir()):
                if not directory.is_dir() or directory.is_symlink() or not STABLE_ID.fullmatch(directory.name):
                    continue
                relative = f"_pipeline/attempts/{directory.name}"
                ticket_path = directory / "ticket.json"
                if ticket_path.is_file():
                    ticket = load_json(ticket_path)
                    summary = {key: ticket[key] for key in ("attempt", "role", "date", "content_id", "input_id", "revision", "state", "created_at", "budget", "result") if key in ticket}
                    summary["schema_version"] = 1
                    summary["session_redacted"] = True
                    result[relative + "/summary.json"] = canonical(summary).encode("utf-8")
                events = directory / "events.jsonl"
                if events.is_file() and events.stat().st_size <= 200000:
                    lines = []
                    for line in events.read_text(encoding="utf-8", errors="replace").splitlines()[:200]:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if type(event) is dict:
                            event.pop("session", None)
                            event.pop("owner", None)
                            event.pop("token", None)
                            event["session_redacted"] = True
                            lines.append(canonical(event))
                    if lines:
                        result[relative + "/events.jsonl"] = ("\n".join(lines) + "\n").encode("utf-8")
                receipt = directory / "receipt.json"
                if receipt.is_file():
                    value = load_json(receipt)
                    allowed = {"schema_version", "stage", "role", "content_id", "input_id", "revision", "run_id", "host", "input_digest", "input_hash", "policy_hash", "outcome", "time", "outputs", "reason_code"}
                    result[relative + "/receipt.json"] = canonical({key: value[key] for key in allowed if key in value}).encode("utf-8")
                raw_result = directory / "result.json"
                if raw_result.is_file():
                    value = load_json(raw_result)
                    summary = {"schema_version": 1}
                    for key in ("outcome", "reason_code", "passed", "decision", "state"):
                        if key in value:
                            summary[key] = value[key]
                    result[relative + "/result-summary.json"] = canonical(summary).encode("utf-8")
                failure = directory / "failure.json"
                if failure.is_file():
                    value = load_json(failure)
                    allowed = {"schema_version", "reason_code", "message", "time", "role", "revision"}
                    result[relative + "/failure.json"] = canonical({key: value[key] for key in allowed if key in value}).encode("utf-8")
        claims = self.workspace.path(".local/claims")
        if claims.exists():
            for path in sorted(claims.glob("*.json")):
                if path.is_symlink() or not path.is_file():
                    continue
                value = load_json(path)
                summary = {key: value[key] for key in ("attempt", "role", "content_id", "input_id", "revision", "host", "binding") if key in value}
                summary["schema_version"] = 1
                summary["present"] = True
                result[f"_pipeline/claims/{path.name}"] = canonical(summary).encode("utf-8")
        requests = self.workspace.path(".local/requests")
        if requests.exists():
            for path in sorted(requests.glob("*.json")):
                if path.is_file() and not path.is_symlink():
                    result[f"_pipeline/requests/{path.name}"] = canonical(load_json(path)).encode("utf-8")
        checkpoints = self.workspace.path(".local/checkpoints")
        if checkpoints.exists():
            for path in sorted(checkpoints.glob("*.json")):
                if path.is_file() and not path.is_symlink():
                    result[f"_pipeline/checkpoints/{path.name}"] = canonical(load_json(path)).encode("utf-8")
        result["_pipeline/README.md"] = (
            "# Pipeline records\n\n"
            "这里只保存脱敏后的 attempt 摘要、事件、回执和 claim 状态。\n"
            "不保存 token、owner 会话、原始工作包、绝对路径或凭据；这些不能跨机交接。\n"
        ).encode("utf-8")
        return result

    def _write_pipeline(self, pipeline):
        for relative, data in pipeline.items():
            target = safe_path(self.repo, relative)
            if not target.exists() or target.read_bytes() != data:
                atomic_bytes(target, data)

    def _date_summary(self, local):
        dates = {}
        for relative in local:
            first = relative.split("/")[0]
            if not DATE_DIR.fullmatch(first):
                continue
            entry = dates.setdefault(first, {"date": first, "files": 0, "items": 0, "contents": 0, "revisions": 0, "statuses": {}})
            entry["files"] += 1
        for date, entry in dates.items():
            plan_path = self.workspace.path(f"{date}/plan.json")
            if plan_path.is_file():
                plan = load_json(plan_path)
                items = plan.get("items", [])
                entry["items"] = len(items) if type(items) is list else 0
                entry["plan_revision"] = plan.get("revision", 0)
                entry["planned_at"] = plan.get("planned_at")
                counts = {}
                if type(items) is list:
                    for item in items:
                        if type(item) is dict and type(item.get("status")) is str:
                            counts[item["status"]] = counts.get(item["status"], 0) + 1
                entry["statuses"] = counts
            contents = self.workspace.path(f"{date}/contents")
            if contents.is_dir():
                entry["contents"] = len([p for p in contents.iterdir() if p.is_dir() and not p.is_symlink()])
                entry["revisions"] = len([p for p in contents.rglob("r[0-9][0-9][0-9][0-9]") if p.is_dir()])
        return [dates[key] for key in sorted(dates)]

    def _write_dates(self, local):
        value = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "dates": self._date_summary(local)}
        atomic_json(safe_path(self.repo, "_sync/dates.json"), value)

    def _write_manifest(self, local, pipeline, remote, local_before, build_bundles):
        value = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repo": "selfmedia-web",
            "remote": self.remote,
            "branch": self.branch,
            "source_project": self.project.name,
            "source_commit": self._project_commit(),
            "local_commit_before": local_before,
            "remote_commit_before": remote,
            "workspace_files": len(local),
            "dates": self._date_summary(local),
            "pipeline_files": len(pipeline),
            "bundles": list(HOSTS) if (self.project.resolve() != self.repo.resolve() and build_bundles) else [],
            "redactions": ["attempt token", "owner session", "claim token", "absolute path"],
            "publish_enabled": False,
        }
        atomic_json(safe_path(self.repo, "_sync/manifest.json"), value)

    def _write_baseline(self):
        value = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "files": self._repo_manifest()}
        atomic_json(safe_path(self.repo, "_sync/baseline.json"), value)

    def _write_log(self, imported, exported, merged, local, remote, remote_before, local_before):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        value = {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "local_commit_before": local_before, "remote_commit_before": remote_before,
            "imported": imported[:200], "exported": exported[:200], "merged": merged[:100],
            "local_files": len(local), "remote_files": len(remote),
            "publish_enabled": False,
        }
        atomic_json(safe_path(self.repo, f"_logs/sync/{stamp}.json"), value)
        atomic_bytes(safe_path(self.repo, "_logs/README.md"), "# Archive sync logs\n\n每次归档记录导入、导出、计划合并和远端提交；不保存凭据或绝对路径。\n".encode("utf-8"))

    def _commit(self, message):
        self._git(["add", "-A"])
        staged = self._git(["diff", "--cached", "--name-only", "-z"]).split(b"\0")
        names = [name.decode("utf-8", "surrogateescape") for name in staged if name]
        for name in names:
            if not is_allowed_commit_path(name):
                self._git_result(["reset"])
                raise ArchiveError("unexpected path staged for archive commit: " + name, "unexpected_commit_path")
        if not names:
            return None
        if (self.repo / "open-workbench.sh").exists():
            self._git_result(["update-index", "--chmod=+x", "open-workbench.sh"])
        if message is None:
            date = datetime.now(timezone.utc).date().isoformat()
            message = f"sync: archive selfmedia-web content {date}"
        identity = self._identity_args()
        result = self._git_result(["commit", *identity, "-m", message])
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise ArchiveError(detail[:2000] or "archive commit failed", "commit_failed")
        return self._local_sha()

    def _identity_args(self):
        name = self._git_result(["config", "user.name"]).stdout.decode("utf-8", "replace").strip()
        email = self._git_result(["config", "user.email"]).stdout.decode("utf-8", "replace").strip()
        if name and email:
            return []
        return ["-c", "user.name=Content Workbench", "-c", "user.email=content-workbench@localhost"]

    def _push(self, enabled):
        if not enabled:
            return True, "disabled", None
        remote = self._fetch()
        local = self._local_sha()
        if not local:
            return False, "no_commit", None
        if remote and self._git_result(["merge-base", "--is-ancestor", remote, local]).returncode != 0:
            return False, "push_rejected", "remote moved; next sync will merge"
        result = self._git_result(["push", "origin", f"HEAD:{self.branch}"])
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            return False, "push_failed", detail[:1000]
        return True, "pushed", None


def scan_secrets(root):
    root = Path(root)
    total = 0
    findings = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS and not (current / name).is_symlink()]
        for name in filenames:
            if name in IGNORE_FILES or name.endswith((".pyc", ".pyo")):
                continue
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ArchiveError("archive contains a link: " + relative, "unsafe_archive")
            size = path.stat().st_size
            if size > MAX_FILE:
                raise ArchiveError("archive file exceeds 25 MB: " + relative, "file_too_large")
            total += size
            if total > MAX_TOTAL:
                raise ArchiveError("archive exceeds 200 MB", "archive_too_large")
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            data = path.read_bytes()
            for pattern in SECRET_PATTERNS:
                if pattern.search(data):
                    findings.append(relative)
                    break
            if re.search(rb"(?i)\b(password|passwd|api[_-]?key|secret)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/=_.-]{16,}", data):
                findings.append(relative)
    return sorted(set(findings))


def archive_status(workspace):
    try:
        settings = workspace.settings()
    except (OSError, ValueError):
        settings = {}
    configured = settings.get("archive_repo")
    root = Path(configured) if type(configured) is str and configured.strip() else workspace.root
    if root != workspace.root and root.is_dir() and not root.is_symlink():
        manifest_path = root / "_sync/manifest.json"
        dates_path = root / "_sync/dates.json"
    else:
        manifest_path = workspace.path("_sync/manifest.json")
        dates_path = workspace.path("_sync/dates.json")
    if not manifest_path.is_file():
        return {"available": False, "publish_enabled": False, "message": "本工作区尚未生成归档快照。"}
    try:
        manifest = load_json(manifest_path)
        dates = load_json(dates_path) if dates_path.is_file() else {"dates": []}
        return {"available": True, "manifest": manifest, "dates": dates.get("dates", []), "publish_enabled": False}
    except (OSError, ValueError):
        return {"available": False, "publish_enabled": False, "message": "归档摘要不可读取。"}


def run_archive(workspace, *, repo, mode="sync", remote=DEFAULT_REMOTE, branch="main", project=None, push=True,
                pull=True, build_bundles=True, allow_active=False, allow_local_remote=False, message=None):
    if mode not in {"status", "sync"}:
        raise ArchiveError("archive mode must be status or sync", "invalid_mode")
    if workspace.root.resolve() == Path(repo).resolve():
        allow_local_remote = True
        build_bundles = False
    sync = ArchiveSync(workspace, repo, remote=remote, branch=branch, project=project, allow_local_remote=allow_local_remote)
    if mode == "status":
        return sync.status()
    if not push and mode != "sync":
        raise ArchiveError("archive requires sync mode", "invalid_mode")
    if not pull:
        return {"ok": False, "state": "blocked", "reason_code": "pull_required", "publish_enabled": False}
    return sync.sync(push=push, build_bundles=build_bundles, allow_active=allow_active, message=message)
