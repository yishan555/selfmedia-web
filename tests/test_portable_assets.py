"""Portable asset contract, real three-template PNGs; no network/models/approval."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from harness_support import bootstrap
RUN_DIR = bootstrap(__file__)
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/content-engine/assets"
from PIL import Image


def load_tools(path=SOURCE):
    spec = importlib.util.spec_from_file_location("portable_assets_test", path / "scripts/asset_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

TOOLS = load_tools()
URL = "https://example.test/evidence"
BODY = "本段验证便携卡片的完整正文覆盖。图片仅用于隔离机械测试，不代表视觉审查已通过。"
CONTEXT = {"event_id": "content-1", "topic_id": "topic-1", "angle_id": "r1:" + hashlib.sha256(BODY.encode()).hexdigest()}


def content(template):
    slots = {"cover": {"title": "便携证据卡", "subtitle": "测试既有设计与真实渲染"},
             "body": {"title": "证据边界", "paragraphs": [BODY]},
             "list": {"title": "验收步骤", "items": [{"term": "来源", "note": "明确出处"}, {"term": "审查", "note": "逐图读取"}]}}
    spec = {"context": copy.deepcopy(CONTEXT), "sources": [{"id": "s1", "url": URL, "title": "Fixture evidence",
            "event_id": CONTEXT["event_id"], "context": copy.deepcopy(CONTEXT)}],
            "theme": "cream-light", "slots": slots[template], "rights_note": "Synthetic fixture only, not publication rights"}
    if template == "body":
        spec["article_body"] = BODY
    return spec


def dns(ip):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class PortablePhotos(unittest.TestCase):
    def setUp(self):
        self.root = RUN_DIR / self._testMethodName
        self.root.mkdir()
        self.file = self.root / "input.png"
        Image.new("RGB", (301, 221), (30, 70, 160)).save(self.file)

    def photo(self):
        return TOOLS.import_photo(self.root, self.file, URL, "Synthetic fixture, rights unverified")

    def test_single_card_has_no_misleading_group_page_number(self):
        builder, spec = TOOLS._render_spec('cover', content('cover'))
        self.assertNotIn('01 / 01', builder.compile_spec(spec)[0])
        spec['pages'].append(copy.deepcopy(spec['pages'][0]))
        spec['platform_max_images'] = 2
        pages = builder.compile_spec(spec)
        self.assertIn('01 / 02', pages[0])
        self.assertIn('02 / 02', pages[1])

    def test_photo_contract_decode_and_copy(self):
        item = self.photo()
        self.assertEqual(set(item), TOOLS.ITEM_FIELDS)
        self.assertEqual((item["width"], item["height"], item["format"]), (301, 221, "PNG"))
        self.assertEqual(item["sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(item["dhash"], "0000000000000000")
        self.assertEqual(TOOLS.verify_asset(self.root, item)["review_status"], "unreviewed")
        self.file.unlink()
        TOOLS.verify_asset(self.root, item)

    def test_duplicate_imports_unique_uuid(self):
        first, second = self.photo(), self.photo()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["sha256"], second["sha256"])

    def test_formats_are_real_not_suffix(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            Image.new("RGB", (100, 80)).save(self.file, format=fmt)
            self.assertEqual(self.photo()["format"], fmt)

    def test_dhash_decodes_pixels(self):
        image = Image.new("RGB", (9, 8))
        for x in range(9):
            for y in range(8):
                image.putpixel((x, y), (255-x*25,)*3)
        image.save(self.file)
        self.assertEqual(self.photo()["dhash"], "ffffffffffffffff")

    def test_bad_image_rejected_before_registration(self):
        self.file.write_bytes(b"not an image")
        with self.assertRaises(TOOLS.AssetError):
            self.photo()
        self.assertFalse((self.root / "images").exists())

    def test_missing_source_rights_and_relative_file_rejected(self):
        for source, rights, file in (("file:///x", "rights", self.file), (URL, " ", self.file), (URL, "rights", Path("input.png"))):
            with self.assertRaises(TOOLS.AssetError):
                TOOLS.import_photo(self.root, file, source, rights)

    def test_workspace_path_traversal_and_bad_item_rejected(self):
        item = self.photo()
        for path in ("../input.png", str(self.file), "images/../../input.png", "images" + chr(92) + "x.png"):
            bad = {**item, "path": path}
            with self.assertRaises(TOOLS.AssetError):
                TOOLS.verify_asset(self.root, bad)
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.import_photo(".", self.file, URL, "rights")
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.verify_asset(self.root, {**item, "review_status": "approved"})

    def test_modified_pixels_and_approval_sidecar_rejected(self):
        item = self.photo()
        path = self.root / item["path"]
        Image.new("RGB", (301, 221), "red").save(path)
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.verify_asset(self.root, item)
        item = self.photo()
        metadata = self.root / item["path"]
        sidecar = metadata.parent / "asset.json"
        value = json.loads(sidecar.read_text())
        value["review_status"] = "approved"
        sidecar.write_text(json.dumps(value))
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.verify_asset(self.root, item)

    def test_cli_single_json_and_failure_exit(self):
        cli = SOURCE / "scripts/cli.py"
        args = [sys.executable, str(cli), "import-photo", "--file", str(self.file), "--source-url", URL,
                "--rights-note", "Fixture", "--workspace", str(self.root)]
        completed = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertTrue(json.loads(completed.stdout)["ok"])
        completed = subprocess.run([sys.executable, str(cli), "render-card", "--template", "frost", "--file", str(self.file), "--workspace", str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(json.loads(completed.stdout)["ok"])

    def test_mock_download_records_final_url(self):
        with patch.object(TOOLS, "fetch_public", return_value=(self.file.read_bytes(), "https://example.test/final.png", "image/png")):
            item = TOOLS.download(self.root, "https://example.test/p.png", URL, "rights")
        provenance = TOOLS.verify_asset(self.root, item)["provenance"]
        self.assertEqual(provenance["final_url"], "https://example.test/final.png")

    def test_mock_from_page_no_fabrication_and_limit(self):
        page = b'<meta property="og:image" content="/a.png"><img src="/b.png"><img src="/a.png">'
        with patch.object(TOOLS, "fetch_public", side_effect=[(page, URL, "text/html"), (self.file.read_bytes(), "https://example.test/a.png", "image/png")]) as fetch:
            items = TOOLS.from_page(self.root, URL, "rights", limit=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(items[0]["source_url"], URL)
        with patch.object(TOOLS, "fetch_public", return_value=(b"<html></html>", URL, "text/html")):
            with self.assertRaisesRegex(TOOLS.AssetError, "no usable"):
                TOOLS.from_page(self.root, URL, "rights")

    def test_parser_bounded_and_rejects_non_http(self):
        parser = TOOLS.PageImages(URL)
        parser.feed('<base href="http://127.0.0.1"><img src="file:///secret"><img data-src="/ok.png">' + "".join(f'<img src="/{i}.png">' for i in range(200)))
        self.assertEqual(len(parser.urls), 24)
        self.assertEqual(parser.urls[0], "https://example.test/ok.png")

class NetworkSafety(unittest.TestCase):
    def test_ssrf_addresses_denied(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "224.0.0.1"):
            with self.subTest(ip=ip), patch.object(TOOLS.socket, "getaddrinfo", return_value=dns(ip)):
                with self.assertRaises(TOOLS.AssetError):
                    TOOLS.validate_public_url("https://example.test/x")

    def test_ssrf_bad_urls_and_internal_hosts_denied(self):
        for url in ("file:///secret", "http://localhost/x", "http://metadata.google.internal/x", "https://a.local/x", "http://a:b@example.test/x", "http://example.test:8080/x", "http://127.0.0.1" + chr(92) + "@example.test/", "http://%31%32%37.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(TOOLS.AssetError):
                TOOLS.validate_public_url(url)

    def test_dns_mixed_private_and_failure_denied(self):
        with patch.object(TOOLS.socket, "getaddrinfo", return_value=dns("8.8.8.8") + dns("127.0.0.1")):
            with self.assertRaises(TOOLS.AssetError):
                TOOLS.validate_public_url(URL)
        with patch.object(TOOLS.socket, "getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaisesRegex(TOOLS.AssetError, "DNS"):
                TOOLS.validate_public_url(URL)

    def test_fetch_pins_ip_and_retains_host(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "image/png"}
        response.read.side_effect = [b"pixels", b""]
        with patch.object(TOOLS.socket, "getaddrinfo", return_value=dns("8.8.8.8")), patch.object(TOOLS, "build_opener") as build:
            build.return_value.open.return_value = response
            raw, final, _ = TOOLS.fetch_public(URL, proxy="")
        self.assertEqual(raw, b"pixels")
        self.assertEqual(final, URL)
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).hostname, "8.8.8.8")
        self.assertEqual(request.get_header("Host"), "example.test")

    def test_redirect_to_private_revalidated(self):
        redirect = HTTPError(URL, 302, "redirect", {"Location": "http://127.0.0.1/secret"}, io.BytesIO())
        with patch.object(TOOLS.socket, "getaddrinfo", side_effect=[dns("8.8.8.8"), dns("127.0.0.1")]), patch.object(TOOLS, "build_opener") as build:
            build.return_value.open.side_effect = redirect
            with self.assertRaisesRegex(TOOLS.AssetError, "SSRF"):
                TOOLS.fetch_public(URL, proxy="")
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_response_size_and_compression_rejected(self):
        for headers, chunks in (({"Content-Length": "100"}, []), ({"Content-Encoding": "gzip"}, []), ({}, [b"123456"])):
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = headers
            response.read.side_effect = chunks
            with patch.object(TOOLS.socket, "getaddrinfo", return_value=dns("8.8.8.8")), patch.object(TOOLS, "build_opener") as build:
                build.return_value.open.return_value = response
                with self.assertRaises(TOOLS.AssetError):
                    TOOLS.fetch_public(URL, max_bytes=5, proxy="")

    def test_limits_and_http_proxy_compatibility(self):
        self.assertEqual(TOOLS._proxy(URL, "http://127.0.0.1:7890"), {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})
        for proxy in ("socks5://localhost:7890", "https://proxy.test:443", "http://user:pass@proxy.test"):
            with self.assertRaises(TOOLS.AssetError):
                TOOLS._proxy(URL, proxy)
        for timeout in (0, -1, 61, float("nan"), True):
            with self.assertRaises(TOOLS.AssetError):
                TOOLS.fetch_public(URL, timeout=timeout)

class PortableCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = RUN_DIR / "real-three-templates"
        cls.root.mkdir()
        cls.items = {t: TOOLS.render_card(cls.root, t, content(t)) for t in ("cover", "body", "list")}

    def test_real_cover_png(self):
        item = self.items["cover"]
        self.assertEqual(set(item), TOOLS.ITEM_FIELDS)
        self.assertEqual((item["format"], item["width"], item["height"]), ("PNG", 1080, 1440))
        TOOLS.verify_card(self.root, item, CONTEXT, BODY)

    def test_real_body_png_complete_coverage(self):
        item = self.items["body"]
        TOOLS.verify_card(self.root, item, CONTEXT, BODY)
        compiled = json.loads((self.root / item["path"]).parent.joinpath("compiled-spec.json").read_text(encoding="utf-8"))
        self.assertEqual(compiled["article_body"], BODY)
        self.assertEqual(compiled["body_mapping"][-1]["end"], len(BODY))

    def test_real_list_png(self):
        TOOLS.verify_card(self.root, self.items["list"], CONTEXT, BODY)
        self.assertEqual(self.items["list"]["template"], "list")

    def test_free_html_and_other_templates_denied(self):
        for template, value in (("frost", content("cover")), ("cover", {**content("cover"), "html": "<h1>x</h1>"}), ("cover", {**content("cover"), "slots": {"title": "ok", "css": "body{}"}})):
            with self.assertRaises(ValueError):
                TOOLS.render_card(self.root, template, value)

    def test_body_overflow_and_truncated_mapping_denied(self):
        spec = content("body")
        spec["article_body"] = "字" * 421
        spec["slots"]["paragraphs"] = [spec["article_body"]]
        with self.assertRaises(ValueError):
            TOOLS.render_card(self.root, "body", spec)
        spec = content("body")
        spec["article_body"] += "遗漏末尾"
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.render_card(self.root, "body", spec)

    def test_bad_context_and_sources_denied(self):
        for change in ("context", "sources"):
            spec = content("cover")
            if change == "context":
                spec["context"]["angle_id"] = "legacy-r1"
            else:
                spec["sources"][0]["event_id"] = "other-content"
            with self.assertRaises(ValueError):
                TOOLS.render_card(self.root, "cover", spec)

    def test_verify_cross_draft_and_body_change_denied(self):
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.verify_card(self.root, self.items["cover"], {**CONTEXT, "event_id": "other"}, BODY)
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.verify_card(self.root, self.items["body"], CONTEXT, BODY + "changed")

    def test_verify_pixels_html_manifest_and_spec_changes_denied(self):
        folder = (self.root / self.items["cover"]["path"]).parent
        for name in ("card-01.png", "card-01.html", "manifest.json", "spec.json", "compiled-spec.json"):
            path = folder / name
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"changed")
                with self.assertRaises((ValueError, OSError)):
                    TOOLS.verify_card(self.root, self.items["cover"], CONTEXT, BODY)
            finally:
                path.write_bytes(original)

    def test_verify_dependency_changes_denied(self):
        with patch.object(TOOLS, "dependency_hashes", return_value={"cards/builder.py": "changed"}):
            with self.assertRaisesRegex(TOOLS.AssetError, "dependencies"):
                TOOLS.verify_card(self.root, self.items["cover"], CONTEXT, BODY)

    def test_no_original_paths_and_workspace_migration(self):
        destination = RUN_DIR / "migrated"
        destination.mkdir()
        media = destination / "media/1"
        shutil.copytree(self.root / "images", media / "images")
        for template, item in self.items.items():
            migrated = {**item, "path": "media/1/" + item["path"]}
            TOOLS.verify_card(destination, migrated, CONTEXT, BODY)
            folder = (destination / migrated["path"]).parent
            for name in ("spec.json", "compiled-spec.json", "manifest.json", "card-01.html", "asset.json"):
                self.assertNotIn(str(self.root), (folder / name).read_text(encoding="utf-8"))
                self.assertNotIn(str(SOURCE), (folder / name).read_text(encoding="utf-8"))

    def test_detached_package_no_repo_import(self):
        detached = RUN_DIR / "detached-package"
        shutil.copytree(SOURCE, detached)
        tools = load_tools(detached)
        workspace = RUN_DIR / "detached-workspace"
        workspace.mkdir()
        item = tools.render_card(workspace, "cover", content("cover"))
        tools.verify_card(workspace, item, CONTEXT, BODY)
        for path in (detached / "scripts").glob("*.py"):
            self.assertNotIn("scripts.utils", path.read_text(encoding="utf-8"))
        accepted = json.loads((ROOT / "tests/fixtures/accepted-card-css.json").read_text(encoding="utf-8"))
        for name in ("tokens.css", "base.css", "components.css"):
            self.assertEqual(hashlib.sha256((SOURCE / "cards/css" / name).read_bytes()).hexdigest(), accepted[name])

    def test_failed_real_layout_not_registered(self):
        workspace = RUN_DIR / "failed-layout"
        workspace.mkdir()
        spec = content("body")
        spec["slots"] = {"title": "长标题" * 7, "lead": "长导语" * 20,
                         "paragraphs": ["W" * 140, "W" * 140, "W" * 140],
                         "quote": "长引用" * 14, "attribution": "出处" * 20}
        spec["article_body"] = "".join(spec["slots"]["paragraphs"])
        with self.assertRaises(TOOLS.AssetError):
            TOOLS.render_card(workspace, "body", spec)
        self.assertFalse(list(workspace.glob("images/*/asset.json")))

class PackageValidation(unittest.TestCase):
    def test_build_copied_common_validate_and_needs_input(self):
        package = RUN_DIR / "validation-package"
        shutil.copytree(SOURCE, package)
        shutil.copyfile(ROOT / "tools/skill-packager/common.py", package / "scripts/common.py")
        policy = {"schema_version": 1,
                  "scoring": {"weights": {key: 1.0 for key in ("information_density", "hook_power", "visual_appeal", "audience_fit", "emotional_resonance", "social_shareability")},
                              "threshold": 7.0, "max_iterations": 3},
                  "runtime": {key: 300 for key in ("research", "write", "assets", "review")},
                  "source_digest": "a" * 64}
        (package / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
        workspace = RUN_DIR / "validate-workspace"
        workspace.mkdir()
        source = workspace / "source.png"
        Image.new("RGB", (300, 200), "blue").save(source)
        tools = load_tools(package)
        item = tools.import_photo(workspace, source, URL, "Synthetic")
        result = workspace / "result.json"
        result.write_text(json.dumps({"items": [item]}), encoding="utf-8")
        args = [sys.executable, str(package / "scripts/cli.py"), "validate", "--file", str(result), "--workspace", str(workspace), "--platform", "xhs"]
        run = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["result"], {"items": [item]})
        result.write_text(json.dumps({"outcome": "needs_input", "reason_code": "missing_images", "message": "No related photos"}), encoding="utf-8")
        run = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(json.loads(run.stdout)["ok"])

    def test_registered_dir_migration_and_real_dependency_tamper(self):
        package = RUN_DIR / "tamper-package"
        shutil.copytree(SOURCE, package)
        tools = load_tools(package)
        workspace = RUN_DIR / "tamper-workspace"
        workspace.mkdir()
        item = tools.render_card(workspace, "cover", content("cover"))
        self.assertEqual(tools.registered_asset_dir(workspace, item), (workspace / item["path"]).parent)
        for relative in ("cards/builder.py", "cards/render.py", "cards/css/base.css", "cards/recipes/schema.json", "cards/assets/logo.svg"):
            path = package / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(TOOLS.AssetError.__bases__[0], "dependencies"):
                    tools.verify_card(workspace, item, CONTEXT, BODY)
            finally:
                path.write_bytes(original)

    def test_mock_https_uses_original_tls_hostname(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.side_effect = [b"", b""]
        with patch.object(TOOLS.socket, "getaddrinfo", return_value=dns("8.8.8.8")), patch.object(TOOLS, "build_opener") as build:
            build.return_value.open.return_value = response
            TOOLS.fetch_public(URL, proxy="http://127.0.0.1:7890")
            handler = next(h for h in build.call_args.args if isinstance(h, TOOLS.HTTPSHandler))
            with patch.object(handler, "do_open") as do_open:
                handler.https_open(MagicMock())
                connection_type = do_open.call_args.args[0]
                connection = connection_type("8.8.8.8")
                connection.sock = MagicMock()
                connection._context = MagicMock()
                with patch.object(TOOLS.http.client.HTTPConnection, "connect"):
                    connection.connect()
                self.assertEqual(connection._context.wrap_socket.call_args.kwargs["server_hostname"], "example.test")

if __name__ == "__main__":
    unittest.main(verbosity=2)
