"""Test-only Python boundary; not an OS or production security sandbox."""

import argparse
import builtins
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = Path(__file__).resolve()
_ACTIVE = None


class TestBoundaryError(PermissionError):
    pass


def repository_roots(root=ROOT):
    roots = [Path(root).resolve()]
    marker = roots[0] / ".git"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = marker.parent / gitdir
            if gitdir.parent.name == "worktrees":
                roots.append(gitdir.resolve().parents[2])
    return list(dict.fromkeys(roots))


def protected_paths(root=ROOT, environment=None):
    environment = os.environ if environment is None else environment
    paths = [repo / name for repo in repository_roots(root)
             for name in ("workspace", "data", "assets", "images", "state", "drafts")]
    if environment.get("SELFMEDIA_DATA_DIR"):
        paths.append(Path(environment["SELFMEDIA_DATA_DIR"]).resolve())
    return list(dict.fromkeys(paths))


def snapshot(paths):
    result = {}
    for root in paths:
        root = Path(root).resolve()
        result[str(root)] = "directory" if root.is_dir() else "missing"
        entries = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in entries:
            if path.is_symlink():
                result[str(path)] = "symlink:" + os.readlink(path)
            elif path.is_file():
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                result[str(path)] = hasher.hexdigest()
            elif path.is_dir():
                result[str(path)] = "directory"
    return result


def new_run_directory(label="run", root=ROOT):
    base = Path(root).resolve()
    parent = base / "tmp" / "regression" if base == ROOT.resolve() else base
    parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        candidate = parent / (label + "-" + uuid.uuid4().hex[:8])
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise OSError("cannot create a unique test run directory")


def isolated_environment(run_dir, environment=None):
    env = dict(os.environ if environment is None else environment)
    for key in list(env):
        upper = key.upper()
        if any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "COOKIE", "CREDENTIAL")) or upper in {"WX_APPID", "WX_APPSECRET", "WECHAT_APPID", "MP_APPID"}:
            env.pop(key)
    original_home = env.get("HERMES_HOME")
    protected_homes = json.loads(env.get("SELFMEDIA_TEST_PROTECTED_HOMES", "[]"))
    if original_home and not Path(original_home).resolve().is_relative_to(Path(run_dir).resolve()):
        protected_homes.append(original_home)
    env["SELFMEDIA_TEST_PROTECTED_HOMES"] = json.dumps(list(dict.fromkeys(protected_homes)))
    run_dir = Path(run_dir).resolve()
    env.update({"HERMES_HOME": str(run_dir / "home"),
                "SELFMEDIA_DATA_DIR": str(run_dir / "data"),
                "TEMP": str(run_dir / "temp"), "TMP": str(run_dir / "temp"),
                "TMPDIR": str(run_dir / "temp"), "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "SELFMEDIA_TEST_RUN_DIR": str(run_dir)})
    return env


class Boundary:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir).resolve()
        self.importing = False
        self.blocked_urls = []
        self.real_popen = subprocess.Popen
        self.real_import = builtins.__import__
        self.hermes_roots = [Path.home() / ".hermes"]
        if os.environ.get("LOCALAPPDATA"):
            self.hermes_roots.append(Path(os.environ["LOCALAPPDATA"]) / "hermes")
        if os.environ.get("HERMES_HOME"):
            self.hermes_roots.append(Path(os.environ["HERMES_HOME"]))
        self.hermes_roots.extend(Path(home) for home in json.loads(
            os.environ.get("SELFMEDIA_TEST_PROTECTED_HOMES", "[]")))
        # The interpreter and its installed packages are not "production data",
        # even when the deployment bundles them inside the protected home. On the
        # production box the only Python lives under `.hermes-runtime`, so every
        # stdlib/third-party import tripped the guard and suites died at import
        # (2d shadow §4). Exempt the interpreter's own prefixes for module reads
        # only; everything else under a protected home stays blocked.
        self.interpreter_roots = []
        for prefix in (getattr(sys, "prefix", None), getattr(sys, "base_prefix", None),
                       str(Path(sys.executable).resolve().parent.parent)):
            if prefix:
                try:
                    self.interpreter_roots.append(Path(prefix).resolve())
                except OSError:
                    pass

    def path(self, value, dir_fd=None):
        if isinstance(value, int) or value is None:
            return None
        path = Path(os.fsdecode(value))
        if dir_fd not in (None, -1) and not path.is_absolute():
            raise TestBoundaryError("test boundary: unresolved dir_fd operation")
        resolved = str(path.resolve())
        if os.name == "nt":
            if resolved.startswith("\\\\?\\UNC\\"):
                resolved = "\\\\" + resolved[8:]
            elif resolved.startswith("\\\\?\\") and len(resolved) > 6 and resolved[5:7] == ":\\":
                resolved = resolved[4:]
        return Path(resolved)

    def writable(self, value, dir_fd=None):
        path = self.path(value, dir_fd)
        if path is not None and not path.is_relative_to(self.run_dir):
            raise TestBoundaryError(f"test boundary: write outside suite directory: {path}")

    def readable(self, value):
        path = self.path(value)
        if path is None or path.is_relative_to(self.run_dir):
            return
        name = path.name.lower()
        credential = (name == ".env" or (name.startswith(".env.") and name != ".env.example")
                      or (path.suffix.lower() not in {".py", ".pyc", ".pyd"} and
                          any(word in name for word in ("credential", "cookies", "access_token", "app_secret"))))
        if credential:
            raise TestBoundaryError(f"test boundary: production credential read: {path}")
        # Explicitly protected homes stay blocked — except interpreter module
        # reads (stdlib/site-packages under the interpreter's own prefixes),
        # which are not production data. The module exemption is checked FIRST;
        # the previous order raised on the hermes-root match before the
        # exemption could apply, which made the exemption dead code whenever the
        # interpreter itself lives under the protected home.
        under_home = any(path.is_relative_to(home.resolve()) for home in self.hermes_roots)
        if under_home:
            lowered = {part.lower() for part in path.parts}
            interpreter_owned = (
                any(path.is_relative_to(root) for root in self.interpreter_roots)
                and lowered & {"lib", "site-packages", "dlls", "libs"}
            )
            if not interpreter_owned:
                raise TestBoundaryError(f"test boundary: production home read: {path}")
            return

    def audit(self, event, args):
        if event == "open":
            path, mode, flags = args
            if (mode and any(char in mode for char in "wax+")) or (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                self.writable(path)
            else:
                self.readable(path)
        elif event == "sqlite3.connect":
            database = os.fsdecode(args[0])
            if database != ":memory:":
                if database.startswith("file:"):
                    parsed = urlsplit(database)
                    if parsed.netloc:
                        raise TestBoundaryError("test boundary: remote SQLite URI forbidden")
                    database = url2pathname(unquote(parsed.path))
                self.writable(database)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            dir_fd = args[1] if event in {"os.remove", "os.rmdir"} else args[-1]
            self.writable(args[0], dir_fd)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            self.writable(args[0], args[2] if len(args) > 2 and event != "os.symlink" else None)
            self.writable(args[1], args[3] if len(args) > 3 else None)
        elif event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "urllib.Request", "http.client.connect", "http.client.send"}:
            socket_module = sys.modules.get("socket")
            fallback = getattr(socket_module, "_fallback_socketpair", None)
            frame = sys._getframe(1)
            ipc = False
            while frame:
                if fallback and frame.f_code is fallback.__code__:
                    ipc = event in {"socket.bind", "socket.connect"} and args[1][0] in {"127.0.0.1", "::1"}
                    break
                frame = frame.f_back
            if not ipc:
                raise TestBoundaryError(f"test boundary: real network forbidden ({event})")
        elif event == "os.putenv":
            key = os.fsdecode(args[0])
            if key in {"HERMES_HOME", "SELFMEDIA_DATA_DIR", "TEMP", "TMP", "TMPDIR", "SELFMEDIA_TEST_RUN_DIR"}:
                self.writable(os.fsdecode(args[1]))
        elif event == "os.unsetenv" and os.fsdecode(args[0]) in {"HERMES_HOME", "SELFMEDIA_DATA_DIR", "TEMP", "TMP", "TMPDIR", "SELFMEDIA_TEST_RUN_DIR"}:
            raise TestBoundaryError("test boundary: isolation environment cannot be removed")
        elif event == "subprocess.Popen":
            executable, argv, cwd, env = args
            self.writable(cwd or Path.cwd())
            command = list(argv) if not isinstance(argv, str) else []
            python = executable is not None and self.path(executable) == Path(sys.executable).resolve()
            prefix = subprocess.list2cmdline([sys.executable, str(BOOTSTRAP)])
            guarded = python and ((len(command) > 1 and self.path(command[1]) == BOOTSTRAP)
                                  or (isinstance(argv, str) and argv.startswith(prefix + " ")))
            driver = self.local_driver(command)
            if isinstance(argv, str) and not driver:
                # asyncio's transport passes argv already joined into one
                # command line and may leave ``executable`` empty; take the
                # first token as the effective executable before deciding.
                package = sys.modules.get("playwright")
                if package and package.__file__:
                    driver_root = Path(package.__file__).resolve().parent / "driver"
                    expected = [str(driver_root / "node.exe"), str(driver_root / "package" / "cli.js"), "run-driver"]
                    expected_line = subprocess.list2cmdline(expected)
                    first = argv.split(" ", 1)[0].strip('"')
                    driver = (argv == expected_line
                              or (self.path(first) == self.path(expected[0]) and argv.startswith(expected_line)))
            if not guarded and not driver and not self.local_git_args(argv, executable):
                raise TestBoundaryError("test boundary: external/platform process forbidden")
            if not env or env.get("SELFMEDIA_TEST_RUN_DIR") != str(self.run_dir):
                raise TestBoundaryError("test boundary: child lost isolation environment")
            for key in ("HERMES_HOME", "SELFMEDIA_DATA_DIR", "TEMP", "TMP", "TMPDIR"):
                if not env.get(key):
                    raise TestBoundaryError("test boundary: child lost isolation environment")
                self.writable(env[key])
        elif event in {"os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty", "pty.spawn", "webbrowser.open"}:
            raise TestBoundaryError(f"test boundary: uncontrolled process forbidden ({event})")

    def local_git_path(self, command):
        if not command or len(command) > 16:
            return None
        name = Path(command[0]).name.lower()
        if name not in {"git", "git.exe"}:
            return None
        installed = shutil.which(command[0])
        if not installed:
            return None
        forbidden = ("http://", "https://", "ssh://", "git://", "--upload-pack", "--receive-pack")
        if any(len(argument) > 512 or any(token in argument.lower() for token in forbidden) for argument in command[1:]):
            return None
        return installed

    def local_git_args(self, argv, executable=None):
        candidate = executable or (argv[0] if isinstance(argv, (list, tuple)) and argv else None)
        if not candidate or Path(str(candidate)).name.lower() not in {"git", "git.exe"}:
            return False
        text = argv if isinstance(argv, str) else " ".join(str(value) for value in argv)
        return len(text) <= 4096 and not any(token in text.lower() for token in ("http://", "https://", "ssh://", "git://", "--upload-pack", "--receive-pack"))

    def local_driver(self, command):
        package = sys.modules.get("playwright")
        if not package or not package.__file__ or len(command) != 3:
            return False
        root = Path(package.__file__).resolve().parent / "driver"
        return (self.path(command[0]) == root / ("node.exe" if os.name == "nt" else "node")
                and self.path(command[1]) == root / "package" / "cli.js"
                and command[2] == "run-driver")

    def popen(self, args, *positional, **kwargs):
        if positional:
            raise TestBoundaryError("test boundary: use keyword Popen options")
        if kwargs.get("shell"):
            raise TestBoundaryError("test boundary: shell execution forbidden")
        command = list(args) if not isinstance(args, (str, bytes)) else []
        executable = kwargs.get("executable") or (command[0] if command else "")
        if executable and self.path(executable) == Path(sys.executable).resolve():
            command = command[1:]
            while command and command[0] in {"-I", "-B", "-u", "-E", "-s"}:
                command.pop(0)
            if not command:
                raise TestBoundaryError("test boundary: interactive Python forbidden")
            kind = "file"
            if command[0] in {"-c", "-m"}:
                kind = {"-c": "code", "-m": "module"}[command.pop(0)]
            elif command[0].startswith("-"):
                raise TestBoundaryError("test boundary: unsupported Python flags")
            target, *arguments = command
            command = [sys.executable, str(BOOTSTRAP), "--run-dir", str(self.run_dir),
                       "--kind", kind, "--target", target, "--", *arguments]
        if not command:
            raise TestBoundaryError("test boundary: explicit command list required")
        if not (self.path(command[0]) == Path(sys.executable).resolve() or self.local_driver(command) or self.local_git_path(command)):
            raise TestBoundaryError("test boundary: external/platform process forbidden")
        kwargs["executable"] = command[0]
        env = isolated_environment(self.run_dir, kwargs.get("env") or os.environ)
        for key in ("HERMES_HOME", "SELFMEDIA_DATA_DIR"):
            candidate = (kwargs.get("env") or os.environ).get(key)
            if candidate:
                self.writable(candidate)
                env[key] = candidate
        kwargs["env"] = env
        kwargs.setdefault("cwd", str(self.run_dir))
        return command, kwargs

    def import_module(self, *args, **kwargs):
        module = self.real_import(*args, **kwargs)
        api = sys.modules.get("playwright.sync_api")
        if api and hasattr(api, "Browser") and hasattr(api, "BrowserType") and not self.importing:
            self.importing = True
            browser = api.Browser
            original = browser.new_context
            if not getattr(original, "_test_boundary", False):
                def block_remote(context):
                    def route_request(route):
                        url = route.request.url
                        if url.startswith("file:"):
                            parsed = urlsplit(url)
                            path = Path(url2pathname(unquote(parsed.path))).resolve()
                            try:
                                self.readable(path)
                            except TestBoundaryError:
                                return route.abort()
                            permitted = [*repository_roots(), self.run_dir]
                            if parsed.netloc not in {"", "localhost"} or not any(path.is_relative_to(root) for root in permitted):
                                return route.abort()
                        elif not url.startswith(("data:", "about:", "blob:")):
                            self.blocked_urls.append(url)
                            return route.abort()
                        return route.continue_()
                    context.route("**/*", route_request)

                def new_context(instance, *a, **kw):
                    kw["service_workers"] = "block"
                    context = original(instance, *a, **kw)
                    block_remote(context)
                    return context

                original_page = browser.new_page

                def new_page(instance, *a, **kw):
                    kw["service_workers"] = "block"
                    page = original_page(instance, *a, **kw)
                    block_remote(page.context)
                    return page

                original_launch = api.BrowserType.launch

                def launch(instance, *a, **kw):
                    if instance.name != "chromium":
                        raise TestBoundaryError("test boundary: only local Chromium rendering is allowed")
                    flags = ["--disable-background-networking", "--disable-component-update",
                             "--disable-sync", "--no-first-run", "--proxy-server=http://127.0.0.1:9",
                             "--proxy-bypass-list=<-loopback>"]
                    kw["args"] = [*kw.get("args", []), *flags]
                    return original_launch(instance, *a, **kw)

                new_context._test_boundary = True
                browser.new_context = new_context
                browser.new_page = new_page
                api.BrowserType.launch = launch
            self.importing = False
        return module


def bootstrap(suite=None, run_dir=None):
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE.run_dir
    run_dir = Path(run_dir) if run_dir else new_run_directory(Path(suite).stem if suite else "standalone")
    run_dir = run_dir.resolve()
    if not run_dir.is_relative_to(ROOT / "tmp"):
        raise TestBoundaryError("test boundary: run directory must be under repository tmp/")
    guard = Boundary(run_dir)
    inherited = {}
    if os.environ.get("SELFMEDIA_TEST_RUN_DIR") == str(run_dir):
        for key in ("HERMES_HOME", "SELFMEDIA_DATA_DIR"):
            candidate = os.environ.get(key)
            if candidate:
                guard.writable(candidate)
                inherited[key] = candidate
    for name in ("home", "data", "temp"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    environment = isolated_environment(run_dir)
    environment.update(inherited)
    for key in set(os.environ) - set(environment):
        os.environ.pop(key, None)
    os.environ.update(environment)
    tempfile.tempdir = str(run_dir / "temp")
    sys.dont_write_bytecode = True
    os.chdir(run_dir)
    sys.addaudithook(guard.audit)

    class GuardedPopen(guard.real_popen):
        def __init__(self, args, *positional, **kwargs):
            command, options = guard.popen(args, *positional, **kwargs)
            super().__init__(command, **options)

    subprocess.Popen = GuardedPopen
    builtins.__import__ = guard.import_module
    _ACTIVE = guard
    (run_dir / "boundary.json").write_text(json.dumps({
        "loaded": True, "pid": os.getpid(), "run_dir": str(run_dir),
        "scope": "test-only Python audit/controlled subprocess boundary, not an OS sandbox"
    }), encoding="utf-8")
    return run_dir


def select_source_root(source_root):
    source_root = Path(source_root).resolve()
    if source_root not in repository_roots() or not (source_root / "apps" / "__init__.py").is_file():
        raise TestBoundaryError("test boundary: source root must be this repository or its main worktree")
    if any(name == "apps" or name.startswith("apps.") for name in sys.modules):
        raise TestBoundaryError("test boundary: select source root before application imports")
    import types
    package = types.ModuleType("apps")
    package.__path__ = [str(source_root / "apps")]
    package.__file__ = str(source_root / "apps" / "__init__.py")
    package.__package__ = "apps"
    sys.modules["apps"] = package
    os.environ["SELFMEDIA_TEST_SOURCE_ROOT"] = str(source_root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--kind", choices=("file", "code", "module"), default="file")
    parser.add_argument("--target", required=True)
    parser.add_argument("--source-root", help="Explicit read-only source repository")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    sys.modules["harness_support"] = sys.modules[__name__]
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    bootstrap(run_dir=args.run_dir)
    source_root = args.source_root or os.environ.get("SELFMEDIA_TEST_SOURCE_ROOT")
    if source_root:
        select_source_root(source_root)
    extra = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    sys.argv = [args.target, *extra]
    if args.kind == "file":
        runpy.run_path(args.target, run_name="__main__")
    elif args.kind == "module":
        runpy.run_module(args.target, run_name="__main__", alter_sys=True)
    else:
        exec(compile(args.target, "<guarded-test-child>", "exec"), {"__name__": "__main__"})


if __name__ == "__main__":
    main()
