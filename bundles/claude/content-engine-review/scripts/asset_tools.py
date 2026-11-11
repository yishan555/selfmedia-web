"""Portable mechanical-only assets. No database, model calls, or approval authority."""
from __future__ import annotations
import copy
import hashlib
import http.client
import importlib.util
import io
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
import socket
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import (HTTPHandler, HTTPSHandler, HTTPRedirectHandler,
                            ProxyHandler, Request, build_opener, getproxies, proxy_bypass)
import uuid
import warnings

SKILL_ROOT = Path(__file__).resolve().parent.parent
CARDS = SKILL_ROOT / "cards"
ITEM_FIELDS = frozenset(("id", "kind", "path", "source_url", "rights_note", "sha256",
                         "width", "height", "format", "dhash", "template"))
TEMPLATES = frozenset(("cover", "body", "list"))
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000

class AssetError(ValueError):
    pass

def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()

def file_hash(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _json(path, value):
    path = Path(path)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_bytes(canonical(value))
    pending.replace(path)

def _workspace(value) -> Path:
    raw = Path(value)
    if not raw.is_absolute() or str(raw).startswith(("\\\\", "//")):
        raise AssetError("--workspace must be an absolute local directory")
    root = raw.resolve()
    if not root.is_dir():
        raise AssetError("workspace directory does not exist")
    return root

def _within(root, relative) -> Path:
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise AssetError("asset path must be workspace-relative POSIX")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or str(parts) != relative:
        raise AssetError("unsafe asset path")
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise AssetError("asset path escapes workspace")
    return result

def _url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise AssetError("require a bounded http(s) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AssetError("invalid URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise AssetError("require http(s) URL without credentials")
    if "\\" in value or "%" in parsed.hostname or port not in (None, 80, 443):
        raise AssetError("invalid host or unsupported public port")
    return parsed

def _rights(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise AssetError("rights_note must be nonempty text <=4000 characters")
    return value

def _public_ip(value):
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved or
            getattr(address, "ipv4_mapped", None) is not None or
            address in ipaddress.ip_network("64:ff9b::/96") or
            address in ipaddress.ip_network("2002::/16")):
        raise AssetError("SSRF: private, loopback, metadata, or special address refused")
    return str(address)

def validate_public_url(url) -> tuple:
    """Reject all nonpublic DNS answers and return a pinned public address."""
    parsed = _url(url)
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or host in ("metadata.google.internal", "instance-data"):
        raise AssetError("SSRF: internal hostname refused")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AssetError("DNS resolution failed") from exc
    ips = list(dict.fromkeys(_public_ip(a[4][0]) for a in answers))
    if not ips:
        raise AssetError("DNS returned no addresses")
    return parsed, ips[0]

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _limits(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise AssetError("timeout must be 1..60 seconds")

def _proxy(url, explicit):
    chosen = explicit
    if chosen is None:
        chosen = getproxies().get(urlsplit(url).scheme) if not proxy_bypass(urlsplit(url).hostname) else None
    if chosen:
        parsed = urlsplit(chosen)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise AssetError("proxy must be http:// host:port without credentials")
        return {"http": chosen, "https": chosen}
    return {}

def fetch_public(url, *, timeout=15, proxy=None, max_bytes=MAX_IMAGE_BYTES):
    """Bounded urllib GET; redirects revalidated and numeric host blocks rebinding.

    HTTPS keeps original certificate hostname/SNI, including HTTP CONNECT.
    Proxy is user-configured transport, never taken from untrusted page data.
    """
    _limits(timeout)
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_IMAGE_BYTES:
        raise AssetError("invalid response size bound")
    deadline = time.monotonic() + timeout
    current = url
    for redirect in range(6):
        parsed, ip = validate_public_url(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssetError("fetch timed out")
        host = parsed.hostname
        authority = "[" + ip + "]" if ":" in ip else ip
        if parsed.port:
            authority += ":" + str(parsed.port)
        pinned = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))

        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                http.client.HTTPConnection.connect(self)
                self.sock = self._context.wrap_socket(self.sock, server_hostname=host)

        class PinnedHandler(HTTPSHandler):
            def https_open(self, request):
                return self.do_open(PinnedHTTPS, request, context=self._context)

        opener = build_opener(ProxyHandler(_proxy(current, proxy)), _NoRedirect(), HTTPHandler(), PinnedHandler())
        request = Request(pinned, headers={"Host": parsed.netloc, "User-Agent": "PortableAssets/1.0", "Accept-Encoding": "identity"})
        try:
            response = opener.open(request, timeout=remaining)
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                exc.close()
                if not location or redirect == 5:
                    raise AssetError("redirect missing Location or exceeds 5 hops")
                current = urljoin(current, location)
                continue
            exc.close()
            raise AssetError(f"HTTP request failed ({exc.code})") from exc
        except (OSError, ValueError) as exc:
            raise AssetError("public URL fetch failed") from exc
        with response:
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise AssetError("compressed responses not accepted")
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > max_bytes):
                raise AssetError("response exceeds size bound")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssetError("fetch timed out")
                raw = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
                if sock:
                    sock.settimeout(remaining)
                reader = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
                chunk = reader(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise AssetError("response exceeds size bound")
            return b"".join(chunks), current, response.headers.get("Content-Type", "")
    raise AssetError("redirect limit exceeded")

def image_info(data) -> dict:
    from PIL import Image, ImageOps
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise AssetError("image empty or exceeds size bound")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt, width, height = image.format, image.width, image.height
                if fmt not in ("PNG", "JPEG", "WEBP") or not 1 <= width <= 20000 or not 1 <= height <= 20000 or width * height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise AssetError("unsupported image format/dimensions/animation")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                small = ImageOps.exif_transpose(image).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                pixels = [small.getpixel((x, y)) for y in range(8) for x in range(9)]
                bits = 0
                for y in range(8):
                    for x in range(8):
                        bits = (bits << 1) | (pixels[y*9+x] > pixels[y*9+x+1])
        return {"sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height,
                "format": fmt, "dhash": f"{bits:016x}"}
    except AssetError:
        raise
    except Exception as exc:
        raise AssetError("image cannot be decoded") from exc

def _new_directory(root, asset_id):
    folder = _within(root, f"images/{asset_id}")
    folder.mkdir(parents=True, exist_ok=False)
    return folder

def _store_photo(root, data, source_url, rights_note, provenance):
    _url(source_url)
    _rights(rights_note)
    info = image_info(data)
    asset_id = str(uuid.uuid4())
    folder = _new_directory(root, asset_id)
    extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[info["format"]]
    path = folder / ("photo." + extension)
    path.write_bytes(data)
    item = {"id": asset_id, "kind": "photo", "path": path.relative_to(root).as_posix(),
            "source_url": source_url, "rights_note": rights_note, **info, "template": None}
    _json(folder / "asset.json", {"asset": item, "review_status": "unreviewed", "provenance": provenance})
    return item

def import_photo(workspace, file, source_url, rights_note) -> dict:
    root = _workspace(workspace)
    path = Path(file)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")) or not path.is_file():
        raise AssetError("--file requires existing absolute local photo")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise AssetError("image exceeds size bound")
    return _store_photo(root, path.read_bytes(), source_url, rights_note, {"method": "import-photo"})

def download(workspace, url, source_url, rights_note, timeout=15, proxy=None) -> dict:
    root = _workspace(workspace)
    _url(source_url)
    _rights(rights_note)
    data, final_url, _ = fetch_public(url, timeout=timeout, proxy=proxy)
    return _store_photo(root, data, source_url, rights_note,
                        {"method": "download", "download_url": url, "final_url": final_url})

class PageImages(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.urls = base, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        candidates = []
        if tag == "meta" and (attrs.get("property", "").lower() == "og:image" or attrs.get("name", "").lower() == "twitter:image"):
            candidates.append(attrs.get("content"))
        if tag == "img":
            candidates.extend(attrs.get(key) for key in ("src", "data-src", "data-original"))
            if attrs.get("srcset"):
                candidates.extend(v.strip().split()[0] for v in attrs["srcset"].split(",") if v.strip())
        for candidate in candidates:
            if not candidate or len(self.urls) >= 24:
                continue
            url = urljoin(self.base, candidate)
            try:
                _url(url)
            except AssetError:
                continue
            if url not in self.urls:
                self.urls.append(url)

def from_page(workspace, url, rights_note, limit=6, timeout=15, proxy=None) -> list:
    root = _workspace(workspace)
    _rights(rights_note)
    _limits(timeout)
    if type(limit) is not int or not 1 <= limit <= 12:
        raise AssetError("limit must be 1..12")
    deadline = time.monotonic() + timeout
    data, final_url, content_type = fetch_public(url, timeout=timeout, proxy=proxy, max_bytes=MAX_HTML_BYTES)
    if "text/html" not in content_type.lower() and "application/xhtml+xml" not in content_type.lower():
        raise AssetError("from-page requires HTML response")
    encoding = re.search(r"charset=([\w-]+)", content_type, flags=re.I)
    try:
        text = data.decode(encoding.group(1) if encoding else "utf-8", errors="replace")
    except LookupError as exc:
        raise AssetError("unsupported page charset") from exc
    parser = PageImages(final_url)
    parser.feed(text)
    items, failures = [], []
    for candidate in parser.urls:
        if len(items) >= limit:
            break
        remaining = deadline - time.monotonic()
        if remaining < 1:
            failures.append("page download budget exhausted")
            break
        try:
            raw, fetched, _ = fetch_public(candidate, timeout=min(60, remaining), proxy=proxy)
            items.append(_store_photo(root, raw, url, rights_note,
                         {"method": "from-page", "page_url": url, "final_page_url": final_url,
                          "download_url": candidate, "final_url": fetched}))
        except AssetError as exc:
            failures.append(str(exc))
    if failures:
        print(json.dumps({"from_page_skipped": failures[:24]}, ensure_ascii=False), file=sys.stderr)
    if not items:
        raise AssetError("no usable public page images: " + (failures[0] if failures else "no image URLs in HTML"))
    return items

def _builder():
    spec = importlib.util.spec_from_file_location("_portable_card_builder", CARDS / "builder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def dependency_hashes() -> dict:
    paths = [CARDS / "builder.py", CARDS / "render.py", CARDS / "recipes/schema.json",
             CARDS / "assets/logo.svg", *sorted((CARDS / "css").glob("*.css")),
             Path(__file__).resolve()]
    common = SKILL_ROOT / "scripts/common.py"
    if common.is_file():
        paths.append(common)
    return {p.relative_to(SKILL_ROOT).as_posix(): file_hash(p) for p in paths}

def _render_spec(template, value):
    if template not in TEMPLATES:
        raise AssetError("only cover/body/list templates are available")
    allowed = {"context", "sources", "theme", "slots", "rights_note", "article_body"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise AssetError("render input allows structured context/sources/theme/slots/rights_note/article_body only")
    _rights(value.get("rights_note"))
    context = value.get("context")
    if not isinstance(context, dict) or set(context) != {"event_id", "topic_id", "angle_id"}:
        raise AssetError("require exact content/topic/revision context")
    if not isinstance(context.get("angle_id"), str) or not re.fullmatch(r"r[1-9][0-9]*:[0-9a-f]{64}", context["angle_id"]):
        raise AssetError("angle_id must be r<revision>:<draftdigest sha256>")
    sources = value.get("sources")
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        raise AssetError("sources must be a list of evidence objects")
    for source in sources:
        _url(source.get("url"))
    page = {"template": template, "theme": value.get("theme"), "slots": copy.deepcopy(value.get("slots")),
            "source_ids": [s.get("id") for s in sources], "asset_ids": []}
    spec = {"version": 1, "context": copy.deepcopy(context), "sources": copy.deepcopy(sources),
            "assets": [], "platform_max_images": 1, "pages": [page]}
    if template == "body":
        body = value.get("article_body")
        paragraphs = page["slots"].get("paragraphs", []) if isinstance(page["slots"], dict) else []
        if not isinstance(body, str) or not body.strip() or not isinstance(paragraphs, list) or not all(isinstance(p, str) for p in paragraphs) or "".join(paragraphs) != body:
            raise AssetError("body paragraphs must cover complete article_body without trimming or truncation")
        mapping, cursor = [], 0
        for index, text in enumerate(paragraphs):
            end = cursor + len(text)
            mapping.append({"page": 1, "paragraph": index, "start": cursor, "end": end,
                            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            cursor = end
        spec.update(article_body=body, body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(), body_mapping=mapping)
    elif "article_body" in value:
        raise AssetError("article_body is only valid for body template")
    builder = _builder()
    builder.validate_spec(spec)
    return builder, spec

def render_card(workspace, template, spec) -> dict:
    root = _workspace(workspace)
    builder, compiled = _render_spec(template, spec)
    asset_id = str(uuid.uuid4())
    folder = _within(root, f"images/{asset_id}")
    before = dependency_hashes()
    manifest = builder.build_spec(compiled, root, folder, attempt_id=asset_id)
    if not manifest.get("ok") or before != dependency_hashes():
        detail = manifest.get("error", [c.get("error") for c in manifest.get("cards", [])])
        raise AssetError("card render failed or dependencies changed: " + str(detail))
    path = folder / "card-01.png"
    info = image_info(path.read_bytes())
    if (info["format"], info["width"], info["height"]) != ("PNG", 1080, 1440):
        raise AssetError("invalid rendered canvas")
    _json(folder / "spec.json", spec)
    _json(folder / "compiled-spec.json", compiled)
    item = {"id": asset_id, "kind": "card", "path": path.relative_to(root).as_posix(),
            "source_url": spec["sources"][0]["url"], "rights_note": spec["rights_note"], **info, "template": template}
    provenance = {"context": compiled["context"], "dependencies": before,
                  "files": {name: file_hash(folder / name) for name in
                            ("spec.json", "compiled-spec.json", "card-01.html", "manifest.json", "card-01.png")},
                  "spec_sha256": digest(spec), "manifest_sha256": file_hash(folder / "manifest.json")}
    _json(folder / "asset.json", {"asset": item, "review_status": "unreviewed", "provenance": provenance})
    return item

def verify_asset(root, item) -> dict:
    root = _workspace(root)
    if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
        raise AssetError("asset fields do not match contract")
    try:
        valid_id = str(uuid.UUID(item["id"])) == item["id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    if not valid_id or item["kind"] not in ("photo", "card"):
        raise AssetError("invalid asset identity/kind")
    expected_name = "card-01.png" if item["kind"] == "card" else "photo." + {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(item["format"], "invalid")
    path_parts = PurePosixPath(item["path"]).parts
    if tuple(path_parts[-3:]) != ("images", item["id"], expected_name) or item["template"] not in (TEMPLATES if item["kind"] == "card" else {None}):
        raise AssetError("asset path/template mismatch")
    path = _within(root, item["path"])
    _url(item["source_url"])
    _rights(item["rights_note"])
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise AssetError("image exceeds size bound")
    info = image_info(path.read_bytes())
    if any(type(item[key]) != type(info[key]) or item[key] != info[key] for key in info):
        raise AssetError("image hash/dimensions changed")
    metadata_path = path.parent / "asset.json"
    if not metadata_path.resolve().is_relative_to(root) or metadata_path.stat().st_size > MAX_HTML_BYTES:
        raise AssetError("asset sidecar escapes workspace or exceeds size bound")
    sidecar = json.loads(metadata_path.read_text(encoding="utf-8"))
    original = sidecar.get("asset")
    expected_original = {**item, "path": f"images/{item['id']}/{expected_name}"}
    if original != expected_original or sidecar.get("review_status") != "unreviewed":
        raise AssetError("asset sidecar changed or claims approval")
    return sidecar

def verify_card(root, item, context, draft_body) -> None:
    """Recheck card evidence before a separate actual-Read review binds it."""
    sidecar = verify_asset(root, item)
    if item["kind"] != "card":
        raise AssetError("verify_card requires card")
    folder = _within(_workspace(root), item["path"]).parent
    provenance = sidecar["provenance"]
    if provenance.get("context") != context or provenance.get("dependencies") != dependency_hashes():
        raise AssetError("card context or dependencies changed")
    expected_files = {"spec.json", "compiled-spec.json", "card-01.html", "manifest.json", "card-01.png"}
    if set(provenance.get("files", {})) != expected_files:
        raise AssetError("incomplete card evidence")
    for name, recorded in provenance["files"].items():
        evidence_path = folder / name
        if not evidence_path.resolve().is_relative_to(folder) or evidence_path.stat().st_size > MAX_IMAGE_BYTES:
            raise AssetError("card evidence escapes directory or exceeds size bound")
        if file_hash(evidence_path) != recorded:
            raise AssetError("card evidence changed: " + name)
    value = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    builder, compiled = _render_spec(item["template"], value)
    if value["context"] != context or digest(value) != provenance.get("spec_sha256"):
        raise AssetError("spec context/digest mismatch")
    if json.loads((folder / "compiled-spec.json").read_text(encoding="utf-8")) != compiled:
        raise AssetError("compiled spec changed")
    if item["template"] == "body" and value["article_body"] != draft_body:
        raise AssetError("body card is not complete current draft")
    if item["source_url"] != value["sources"][0]["url"] or item["rights_note"] != value["rights_note"]:
        raise AssetError("card source/rights changed")
    expected_html = builder.compile_spec(compiled)[0]
    if (folder / "card-01.html").read_text(encoding="utf-8") != expected_html:
        raise AssetError("HTML differs from current compiler output")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (not manifest.get("ok") or manifest.get("context") != context or
            manifest.get("review_status") != "unreviewed" or len(manifest.get("cards", [])) != 1 or
            provenance.get("manifest_sha256") != file_hash(folder / "manifest.json")):
        raise AssetError("invalid card manifest")
    card = manifest["cards"][0]
    if (not card.get("ok") or card.get("context") != context or card.get("template") != item["template"] or
            card.get("png_sha256") != item["sha256"] or card.get("html_sha256") != file_hash(folder / "card-01.html")):
        raise AssetError("card manifest evidence mismatch")


def registered_asset_dir(root, item) -> Path:
    """Validated directory containing pixels, sidecar, and complete card evidence."""
    verify_asset(root, item)
    return _within(_workspace(root), item["path"]).parent
