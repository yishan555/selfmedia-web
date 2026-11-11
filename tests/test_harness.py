"""Exercise the test boundary with real local fixtures; never production writes."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from harness_support import (ROOT, TestBoundaryError,
                             bootstrap, isolated_environment, new_run_directory,
                             protected_paths, repository_roots, snapshot)

RUN_DIR = bootstrap(__file__)
sys.path.insert(0, str(ROOT))


class HarnessTests(unittest.TestCase):
    def test_isolation_before_application_import(self):
        self.assertNotIn("apps.workbench.workspace", sys.modules)
        from apps.workbench.workspace import Workspace
        home = Path(os.environ["HERMES_HOME"])
        data = Path(os.environ["SELFMEDIA_DATA_DIR"])
        self.assertTrue(home.is_relative_to(RUN_DIR))
        self.assertTrue(data.is_relative_to(RUN_DIR))
        workspace = Workspace(RUN_DIR / "application-import-fixture")
        self.assertTrue(workspace.root.is_relative_to(RUN_DIR))
        self.assertFalse(workspace.root.exists())
        self.assertTrue(Path(tempfile.gettempdir()).is_relative_to(RUN_DIR))
        self.assertTrue((RUN_DIR / "boundary.json").is_file())

    def test_unique_run_and_suite_directories(self):
        fake_repo = RUN_DIR / "fake-repository"
        first = new_run_directory(root=fake_repo)
        second = new_run_directory(root=fake_repo)
        self.assertNotEqual(first, second)
        suite_a = Path(tempfile.mkdtemp(prefix="suite-", dir=first))
        suite_b = Path(tempfile.mkdtemp(prefix="suite-", dir=first))
        self.assertNotEqual(suite_a, suite_b)

    def test_snapshot_covers_db_wal_shm_state_assets_membership(self):
        fixture = RUN_DIR / "snapshot-fixture" / "data"
        fixture.mkdir(parents=True)
        paths = [fixture]
        for name in ("selfmedia.db", "selfmedia.db-wal", "selfmedia.db-shm",
                     "2026-10-15/pipeline_state.json", "2026-10-15/images/photo.png"):
            path = fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture before")
            before = snapshot(paths)
            path.write_bytes(b"fixture after")
            self.assertNotEqual(before, snapshot(paths), name)
            path.unlink()
            self.assertNotEqual(before, snapshot(paths), name)
        before = snapshot(paths)
        (fixture / "new-asset.png").write_bytes(b"new")
        self.assertNotEqual(before, snapshot(paths))
        workspace = RUN_DIR / "snapshot-fixture" / "workspace"
        workspace.mkdir()
        before = snapshot([workspace])
        (workspace / "plan.json").write_text('{"items": []}', encoding="utf-8")
        self.assertNotEqual(before, snapshot([workspace]))

    def test_original_worktree_is_in_protected_inventory(self):
        roots = repository_roots()
        protected = protected_paths(environment={})
        for repo in roots:
            self.assertIn(repo / "data", protected)
            self.assertIn(repo / "workspace", protected)
        self.assertIn(ROOT, roots)

    def test_allowed_isolated_file_sqlite_and_temp_writes(self):
        path = RUN_DIR / "allowed.json"
        path.write_text('{"fixture":true}', encoding="utf-8")
        self.assertEqual(json.loads(path.read_text())["fixture"], True)
        with sqlite3.connect(RUN_DIR / "fixture.db") as conn:
            conn.execute("CREATE TABLE fixture(value TEXT)")
            conn.execute("INSERT INTO fixture VALUES ('local only')")
        with tempfile.TemporaryDirectory() as name:
            self.assertTrue(Path(name).is_relative_to(RUN_DIR))
            (Path(name) / "allowed.txt").write_text("fixture")

    def test_real_fixture_write_delete_rename_sqlite_and_credentials_denied(self):
        code = '''
import hashlib, os, sqlite3, sys
from pathlib import Path
from harness_support import Boundary, TestBoundaryError
root = Path(os.environ["SELFMEDIA_TEST_RUN_DIR"])
fixture = root / "sealed-fixture"
fixture.mkdir(exist_ok=True)
files = []
for name in ("selfmedia.db", "selfmedia.db-wal", "selfmedia.db-shm", "pipeline_state.json", "asset.png", ".env"):
    path = fixture / name
    path.write_bytes(b"sealed fixture")
    files.append(path)
allowed = root / "nested-allowed"
allowed.mkdir(exist_ok=True)
sys.addaudithook(Boundary(allowed).audit)
for path in files:
    before = hashlib.sha256(path.read_bytes()).hexdigest() if path.name != ".env" else None
    operations = [lambda: path.write_bytes(b"forbidden"), lambda: os.open(path, os.O_WRONLY),
                  lambda: path.unlink(), lambda: path.rename(allowed / "stolen"),
                  lambda: sqlite3.connect(path), lambda: sqlite3.connect(path.as_uri(), uri=True)]
    if path.name == ".env":
        operations.append(lambda: path.read_text())
    for operation in operations:
        try:
            operation()
        except TestBoundaryError:
            pass
        else:
            raise AssertionError("unguarded fixture operation: " + str(path))
    if before:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
(allowed / "ok.txt").write_text("allowed fixture")
print("fixture guard passed")
'''
        completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("fixture guard passed", completed.stdout)

    def test_real_network_attempts_including_local_platform_are_denied(self):
        for host, port in (("127.0.0.1", 18060), ("192.0.2.1", 443)):
            with socket.socket() as sock, self.assertRaises(TestBoundaryError):
                sock.connect((host, port))
        with self.assertRaises(TestBoundaryError):
            socket.getaddrinfo("example.invalid", 443)
        with self.assertRaises(TestBoundaryError):
            urlopen("https://example.invalid", timeout=1)

    def test_platform_shell_and_uncontrolled_processes_denied(self):
        for command in (["hermes", "cron", "list"], ["curl", "https://example.invalid"],
                        ["xiaohongshu-mcp", "publish"], ["docker", "ps"]):
            with self.assertRaises(TestBoundaryError):
                subprocess.run(command)
        with self.assertRaises(TestBoundaryError):
            subprocess.run("hermes cron list", shell=True)
        with self.assertRaises(TestBoundaryError):
            os.system("hermes cron list")

    def test_child_python_reenters_guard_and_cannot_read_credentials(self):
        code = '''
import os, socket, tempfile
from pathlib import Path
from harness_support import TestBoundaryError
root = Path(os.environ["SELFMEDIA_TEST_RUN_DIR"])
assert Path(os.environ["SELFMEDIA_DATA_DIR"]).is_relative_to(root)
assert Path(os.environ["HERMES_HOME"]).is_relative_to(root)
assert Path(tempfile.gettempdir()).is_relative_to(Path(os.environ["SELFMEDIA_TEST_RUN_DIR"]))
try:
    socket.getaddrinfo("example.invalid", 443)
except TestBoundaryError:
    print("child boundary loaded")
else:
    raise AssertionError("network escaped")
'''
        completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("child boundary loaded", completed.stdout)
        with self.assertRaises(TestBoundaryError):
            (ROOT / ".env").read_bytes()

    def test_child_preserves_nested_isolation_before_import(self):
        env = dict(os.environ)
        env["HERMES_HOME"] = str(RUN_DIR / "nested-fixture" / "home")
        env["SELFMEDIA_DATA_DIR"] = str(RUN_DIR / "nested-fixture" / "data")
        code = "import os; from pathlib import Path; root = Path(os.environ['SELFMEDIA_TEST_RUN_DIR']); data = Path(os.environ['SELFMEDIA_DATA_DIR']); assert data.is_relative_to(root); assert Path(os.environ['HERMES_HOME']).is_relative_to(root); print(data)"
        completed = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), env["SELFMEDIA_DATA_DIR"])

    def test_guard_rejects_environment_and_symlink_escape(self):
        with self.assertRaises(TestBoundaryError):
            os.environ["SELFMEDIA_DATA_DIR"] = str(ROOT / "data")
        with self.assertRaises(TestBoundaryError):
            (ROOT / "workspace" / "guard-never-created.json").write_text("never written")
        with self.assertRaises(TestBoundaryError):
            os.unsetenv("HERMES_HOME")
        with self.assertRaises(TestBoundaryError):
            os.symlink(ROOT / "data", RUN_DIR / "outside-link", target_is_directory=True)

    def test_source_assets_readable_without_source_writes(self):
        path = ROOT / "apps" / "workbench" / "workspace.py"
        self.assertTrue(path.read_bytes())
        with self.assertRaises(TestBoundaryError):
            path.write_text("never written")
        from harness_support import select_source_root
        with self.assertRaises(TestBoundaryError):
            select_source_root(RUN_DIR)
        code = "from apps.workbench.workspace import Workspace; from pathlib import Path; import os; assert Path(__import__('apps').__file__).parent == Path(os.environ['SELFMEDIA_TEST_SOURCE_ROOT']) / 'apps'; print('read-only source selected')"
        env = dict(os.environ, SELFMEDIA_TEST_SOURCE_ROOT=str(ROOT))
        completed = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("read-only source selected", completed.stdout)

    def test_interpreter_prefix_readable_but_credentials_still_denied(self):
        """The runtime's own stdlib/packages are not production data.

        A deployment can bundle the only Python under the protected home, so
        every import would trip the guard and whole suites would die before
        running (2d shadow §4: 18 suites import-blocked, 7 more with noise).
        Exempt the interpreter prefix, but never a real secret.
        """
        from harness_support import Boundary
        boundary = Boundary(RUN_DIR)
        self.assertTrue(boundary.interpreter_roots, "no interpreter prefix recorded")
        stdlib = Path(sys.prefix) / "Lib" / "uuid.py"
        if stdlib.is_file():
            boundary.readable(str(stdlib))          # must not raise
        for name in (".env", ".env.local"):
            with self.subTest(secret=name):
                with self.assertRaises(TestBoundaryError):
                    boundary.readable(str(Path(sys.prefix) / name))

    def test_browser_local_rendering_and_remote_route_rejection(self):
        from playwright.sync_api import sync_playwright
        html = RUN_DIR / "local-browser-fixture.html"
        html.write_text("<html><body><h1>Local fixture</h1></body></html>", encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 320, "height": 200})
                page.goto(html.as_uri())
                self.assertEqual(page.locator("h1").inner_text(), "Local fixture")
                output = RUN_DIR / "local-browser-fixture.png"
                page.screenshot(path=str(output))
                self.assertTrue(output.is_file())
                rejected = page.evaluate("fetch('https://example.invalid/fixture').then(() => false).catch(() => true)")
                self.assertTrue(rejected)
                import harness_support
                self.assertIn("https://example.invalid/fixture", harness_support._ACTIVE.blocked_urls)
            finally:
                browser.close()

    def test_socketpair_ipc_allowed_but_arbitrary_loopback_bind_denied(self):
        first, second = socket.socketpair()
        try:
            first.send(b"local IPC")
            self.assertEqual(second.recv(32), b"local IPC")
        finally:
            first.close()
            second.close()
        with socket.socket() as sock, self.assertRaises(TestBoundaryError):
            sock.bind(("127.0.0.1", 0))

    @unittest.skipUnless(os.name == "nt", "Windows extended paths")
    def test_extended_paths_share_boundary_identity_without_escape(self):
        from harness_support import Boundary
        from concurrent.futures import ThreadPoolExecutor
        boundary = Boundary(RUN_DIR)
        target = RUN_DIR / "extended-path-fixture" / "nested"
        extended = "\\\\?\\" + str(target)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: Path(extended).mkdir(parents=True, exist_ok=True), range(40)))
        self.assertEqual(boundary.path(extended), target.resolve())
        Path(extended, "local.txt").write_text("isolated fixture", encoding="utf-8")
        self.assertTrue((target / "local.txt").is_file())
        for outside in (ROOT / "data", RUN_DIR / ".." / "outside-suite"):
            with self.assertRaises(TestBoundaryError):
                boundary.writable("\\\\?\\" + str(outside))
        with self.assertRaises(TestBoundaryError):
            boundary.writable("\\\\?\\UNC\\server\\share\\outside")

    def test_credential_environment_stripped(self):
        env = isolated_environment(RUN_DIR, {"PATH": os.environ["PATH"],
                                           "WX_APPSECRET": "fake-fixture", "OPENAI_API_KEY": "fake-fixture",
                                           "ACCESS_TOKEN": "fake-fixture", "COOKIE": "fake-fixture"})
        for name in ("WX_APPSECRET", "OPENAI_API_KEY", "ACCESS_TOKEN", "COOKIE"):
            self.assertNotIn(name, env)
        self.assertIn("PATH", env)
        env = isolated_environment(RUN_DIR, {"HERMES_HOME": str(ROOT / "fixture-home-outside-suite")})
        self.assertIn(str(ROOT / "fixture-home-outside-suite"), json.loads(env["SELFMEDIA_TEST_PROTECTED_HOMES"]))


if __name__ == "__main__":
    unittest.main()
