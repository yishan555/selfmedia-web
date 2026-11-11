"""Compile evidence-bound structured slots into the accepted card component layouts.

CLI: use ../scripts/cli.py render-card --file ABSOLUTE_JSON --workspace ABSOLUTE_DIR
No free HTML/CSS input. Caller hydrates source/asset registries from approved DB
records and must check event relatedness and approval before invoking this module.
This module never approves or registers an image. A successful manifest proves
only compilation and mechanical layout checks, not factual or visual acceptance.
"""

import copy
import hashlib
import html
import importlib.util
import json
import math
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

_DIR = Path(__file__).resolve().parent
# A direct run and importlib.spec_from_file_location both work from arbitrary cwd.
_renderer_spec = importlib.util.spec_from_file_location("_html_cards_renderer", _DIR / "render.py")
render = importlib.util.module_from_spec(_renderer_spec)
_renderer_spec.loader.exec_module(render)
_SCHEMA_PATH = _DIR / "recipes" / "schema.json"


class SpecError(ValueError):
    pass


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _keys(value, allowed, where):
    if not isinstance(value, dict):
        raise SpecError(f"{where} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise SpecError(f"{where}: unknown fields {sorted(unknown)}")


def _text(value, maximum, where):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SpecError(f"{where}: non-empty text length must be <= {maximum}")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise SpecError(f"{where}: control characters not allowed")
    # Bound line count separately: 30 newlines in a 32-character title is not a
    # short title in a fixed canvas. Runtime layout checks remain authoritative.
    if value.count("\n") > 3:
        raise SpecError(f"{where}: too many explicit line breaks")
    return value


def _id(value, where):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SpecError(f"{where}: ID must be a string or integer")
    if isinstance(value, int) and value < 1:
        raise SpecError(f"{where}: integer ID must be positive")
    if isinstance(value, str):
        _text(value, 128, where)
    return str(value)


def _validate_value(value, schema, where):
    kind = schema["type"]
    if kind == "text":
        _text(value, schema["max"], where)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not schema["min"] <= value <= schema["max"]:
            raise SpecError(f"{where}: finite number must be {schema['min']}..{schema['max']}")
    elif kind == "enum":
        if value not in schema["values"]:
            raise SpecError(f"{where}: unknown enum value")
    elif kind == "list":
        if not isinstance(value, list) or not schema["min"] <= len(value) <= schema["max"]:
            raise SpecError(f"{where}: list length must be {schema['min']}..{schema['max']}")
        for i, item in enumerate(value):
            _validate_value(item, schema["items"], f"{where}[{i}]")
        if "total_max" in schema and sum(len(item) for item in value) > schema["total_max"]:
            raise SpecError(f"{where}: total text length exceeds {schema['total_max']}")
    elif kind == "object":
        _validate_slots(value, schema["fields"], where)
    else:
        raise SpecError(f"unsupported slot schema {kind}")


def _validate_slots(slots, fields, where):
    _keys(slots, fields, where)
    for name, definition in fields.items():
        if name not in slots:
            if definition.get("required"):
                raise SpecError(f"{where}.{name}: required slot missing")
        else:
            _validate_value(slots[name], definition, f"{where}.{name}")


def load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def compile_recipe(role: str) -> dict:
    """Compile human recipe metadata to executable schemas, never sample data.

    Multi-theme prose is split into an allowlist; page aliases share the exact
    same validator/layout as direct template IDs. Optional historical page
    variants are represented by basic components, not bespoke agent HTML.
    """
    schema = load_schema()
    if role not in ("deep_dive", "traffic", "tutorial", "trend", "roundup"):
        raise SpecError(f"unknown recipe: {role}")
    path = _DIR / "recipes" / (role + ".json")
    recipe = json.loads(path.read_text(encoding="utf-8"))
    pages = []
    for original in recipe["pages"]:
        template = schema["recipe_aliases"][original["page_type"]]
        if "c-fullbleed" in " ".join(original["components"]):
            template = "frost"
        allowed = [t.strip() for t in original["theme"].split("|")]
        if any(t not in schema["themes"] for t in allowed):
            raise SpecError("recipe has an unknown theme")
        pages.append({"page_type": original["page_type"], "template": template,
                      "themes": allowed, "schema": copy.deepcopy(schema["templates"][template])})
    return {"role": role, "pages": pages, "recipe_sha256": render.sha256_file(path)}


def _registry(items, kind, context):
    if not isinstance(items, list) or len(items) > 100:
        raise SpecError(f"{kind} registry must be a list with <=100 entries")
    registry = {}
    for item in items:
        where = kind
        allowed = ("id", "url", "title", "event_id", "context") if kind == "sources" else ("id", "path", "source_id", "event_id", "context")
        _keys(item, allowed, where)
        key = _id(item.get("id"), where + ".id")
        if key in registry:
            raise SpecError(f"duplicate {kind} ID: {key}")
        if item.get("event_id") != context["event_id"] or item.get("context") != context:
            raise SpecError(f"{kind} {key}: explicit event+context binding must match spec")
        if kind == "sources":
            url = item.get("url")
            if not isinstance(url, str) or len(url) > 4096 or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                raise SpecError(f"source {key}: require original http(s) evidence URL")
            if "title" in item:
                _text(item["title"], 300, where + ".title")
        else:
            _id(item.get("source_id"), where + ".source_id")
            raw = item.get("path")
            if not isinstance(raw, str) or not Path(raw).is_absolute() or not Path(raw).is_file():
                raise SpecError(f"asset {key}: require existing absolute source image path")
            from PIL import Image
            try:
                with Image.open(raw) as image:
                    image.verify()
            except Exception as exc:
                raise SpecError(f"asset {key}: invalid image: {exc}") from exc
        registry[key] = copy.deepcopy(item)
    return registry


def validate_spec(spec: dict) -> dict:
    """Validate before any rendering or file writes. Never silently truncate."""
    schema = load_schema()
    _keys(spec, ("version", "context", "sources", "assets", "pages", "recipe",
                 "platform_max_images", "article_body", "body_sha256", "body_mapping"), "spec")
    if spec.get("version") != 1 or isinstance(spec.get("version"), bool):
        raise SpecError("spec.version must be 1")
    context = spec.get("context")
    _keys(context, ("event_id", "topic_id", "angle_id"), "context")
    for name in ("event_id", "topic_id", "angle_id"):
        _id(context.get(name), "context." + name)
    sources = _registry(spec.get("sources"), "sources", context)
    if not sources:
        raise SpecError("source evidence registry cannot be empty")
    assets = _registry(spec.get("assets", []), "assets", context)
    for key, asset in assets.items():
        if str(asset["source_id"]) not in sources:
            raise SpecError(f"asset {key}: unknown source_id")
    recipe = compile_recipe(spec["recipe"]) if "recipe" in spec else None
    pages = spec.get("pages")
    maximum = spec.get('platform_max_images')
    if type(maximum) is not int or maximum < 1:
        raise SpecError('Explicit platform_max_images required; unknown platform capacity blocks')
    if not isinstance(pages, list) or not 1 <= len(pages) <= min(maximum, 100):
        raise SpecError('spec.pages exceeds explicit platform_max_images or compiler safety bound (100)')
    for index, page in enumerate(pages):
        where = f"pages[{index}]"
        _keys(page, ("template", "theme", "slots", "source_ids", "asset_ids"), where)
        name = page.get("template")
        if not isinstance(name, str) or name not in schema["templates"]:
            raise SpecError(f"{where}: unknown template {name!r}")
        if page.get("theme") not in schema["themes"]:
            raise SpecError(f"{where}: unknown theme")
        if recipe and not any(p["template"] == name and page["theme"] in p["themes"] for p in recipe["pages"]):
            raise SpecError(f"{where}: template/theme not permitted by recipe")
        definition = schema["templates"][name]
        fields = {**schema["common"], **definition["slots"]}
        _validate_slots(page.get("slots"), fields, where + ".slots")
        for registry_name, registry in (("source_ids", sources), ("asset_ids", assets)):
            ids = page.get(registry_name, [] if registry_name == "asset_ids" else None)
            if not isinstance(ids, list) or len(ids) > 100:
                raise SpecError(f"{where}.{registry_name}: must be an ID list")
            seen = set()
            for value in ids:
                key = _id(value, where + "." + registry_name)
                if key not in registry or key in seen:
                    raise SpecError(f"{where}.{registry_name}: duplicate or unknown ID {key}")
                seen.add(key)
        if not page["source_ids"]:
            raise SpecError(f"{where}: every card requires non-empty source_ids evidence")
        aids = page.get("asset_ids", [])
        limits = definition["assets"]
        if not limits["min"] <= len(aids) <= limits["max"] or ("counts" in limits and len(aids) not in limits["counts"]):
            raise SpecError(f"{where}: invalid asset count for {name}")
        for aid in aids:
            if str(assets[str(aid)]["source_id"]) not in {str(v) for v in page["source_ids"]}:
                raise SpecError(f"{where}: asset source must be explicitly cited by this page")
        slots = page["slots"]
        if name in ('grid', 'shots') and len(slots['image_alts']) != len(aids):
            raise SpecError(f"{where}: one image_alt is required for each asset")
        if name == "table" and any(len(row) != len(slots["columns"]) for row in slots["rows"]):
            raise SpecError(f"{where}: table rows must match column count")
    body = validate_body_mapping(spec)
    return {"spec": copy.deepcopy(spec), "sources": sources, "assets": assets, "recipe": recipe, "schema": schema, 'body': body}


def validate_body_mapping(spec):
    """Exact contiguous source spans, not approximate lengths or ellipsis pages."""
    body, mapping = spec.get('article_body'), spec.get('body_mapping')
    if body is None and mapping is None and 'body_sha256' not in spec:
        if any(page.get('template') == 'body' for page in spec.get('pages', [])):
            raise SpecError('Every body template requires complete article_body and body_mapping')
        return None
    if not isinstance(body, str) or not body.strip() or len(body) > 3000:
        raise SpecError('article_body must be complete nonempty text <=3000 chars')
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    if spec.get('body_sha256') != digest:
        raise SpecError('article body sha256 mismatch')
    if not isinstance(mapping, list) or not mapping:
        raise SpecError('complete body_mapping required')
    cursor, slots_seen, last_slot = 0, set(), (0, -1)
    for span in mapping:
        _keys(span, ('page', 'paragraph', 'start', 'end', 'sha256'), 'body_mapping')
        if any(type(span.get(key)) is not int for key in ('page', 'paragraph', 'start', 'end')):
            raise SpecError('body_mapping offsets and page indices must be integers')
        p, para, start, end = (span[key] for key in ('page', 'paragraph', 'start', 'end'))
        if start != cursor or not start < end <= len(body) or not 1 <= p <= len(spec['pages']):
            raise SpecError('body_mapping must cover the complete body in contiguous order')
        page = spec['pages'][p-1]
        paragraphs = page['slots'].get('paragraphs', [])
        if (page['template'] != 'body' or not 0 <= para < len(paragraphs)
                or (p, para) in slots_seen or (p, para) <= last_slot):
            raise SpecError('body_mapping must target distinct body slots in rendered page order')
        last_slot = (p, para)
        text = body[start:end]
        if paragraphs[para] != text or hashlib.sha256(text.encode('utf-8')).hexdigest() != span.get('sha256'):
            raise SpecError('body_mapping slice/slot/hash mismatch (no truncation)')
        slots_seen.add((p, para))
        cursor = end
    if cursor != len(body):
        raise SpecError('body_mapping omits the end of the article')
    actual = {(p+1, i) for p, page in enumerate(spec['pages']) if page['template'] == 'body'
              for i in range(len(page['slots']['paragraphs']))}
    if actual != slots_seen:
        raise SpecError('Every body paragraph must map to the source article')
    return {'sha256': digest, 'char_count': len(body), 'spans': copy.deepcopy(mapping)}


def paginate_body(spec, body, *, platform_max_images, title='正文', theme='navy-dark', source_ids=None):
    """Add fixed typography body pages, preserving every original character.

    Explicit platform_max_images is supplied by verified runtime policy, not an
    assumed real XHS limit. Capacity remains subject to strict browser checks.
    """
    if type(platform_max_images) is not int or platform_max_images < 1:
        raise SpecError('Explicit platform_max_images required; unknown capacity blocks')
    if not isinstance(body, str) or not body.strip() or not 1000 <= len(body) <= 3000:
        raise SpecError('XHS long body must contain 1000..3000 complete characters')
    result = copy.deepcopy(spec)
    if any(page.get('template') == 'body' for page in result['pages']):
        raise SpecError('paginate_body accepts a spec without existing body pages')
    _text(title, 28, 'body title')
    ids = source_ids if source_ids is not None else [item['id'] for item in result['sources']]
    capacity = load_schema()['templates']['body']['slots']['paragraphs']['total_max']
    mapping, start = [], 0
    while start < len(body):
        end = min(start + capacity, len(body))
        # Newlines have height: independently bound them without stripping them.
        chunk = body[start:end]
        if chunk.count('\n') > 3:
            end = start + [i for i, char in enumerate(chunk) if char == '\n'][3]
            chunk = body[start:end]
        if not chunk.strip() or end <= start:
            raise SpecError('Whitespace-heavy body needs an explicit valid layout; never trim silently')
        result['pages'].append({'template': 'body', 'theme': theme,
                                'slots': {'title': title, 'paragraphs': [chunk]},
                                'source_ids': copy.deepcopy(ids), 'asset_ids': []})
        mapping.append({'page': len(result['pages']), 'paragraph': 0, 'start': start, 'end': end,
                        'sha256': hashlib.sha256(chunk.encode('utf-8')).hexdigest()})
        start = end
    if len(result['pages']) > platform_max_images:
        raise SpecError('Complete body exceeds explicitly provided platform_max_images; never truncate')
    result.update(platform_max_images=platform_max_images, article_body=body,
                  body_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest(), body_mapping=mapping)
    validate_spec(result)
    return result


def validate_attempt_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise SpecError('attempt_id must contain 1..64 safe ASCII identifier characters')
    return value


def _e(value) -> str:
    return html.escape(str(value), quote=True).replace("\n", "<br>")


def _block(cls, value, style="") -> str:
    return f'<div class="{cls}"' + (f' style="{style}"' if style else "") + f'>{_e(value)}</div>'


def _document(page, assets, number, total) -> str:
    """Only code-authored attributes/styles/classes. User input is text or IDs."""
    s, name = page["slots"], page["template"]
    title = _block("c-title-serif", s["title"], "font-size:76px;line-height:1.25;margin:16px 0 32px")
    kicker = _block("c-kicker", s["kicker"], "margin-bottom:16px") if s.get("kicker") else ""
    parts = []
    if name == "cover":
        parts = [kicker, _block("c-title-serif", s["title"], "font-size:108px;line-height:1.26;margin:44px 0"), '<div class="c-rule"></div>']
        if s.get("subtitle"):
            parts.append(_block("c-body", s["subtitle"], "font-size:40px"))
        if s.get("capsules"):
            parts.append('<div class="c-capsules">'+"".join(_block("c-capsule", v) for v in s["capsules"])+"</div>")
        if s.get("note"):
            parts.append(_block("c-body", s["note"], "font-size:30px;margin-top:auto;color:var(--ink-soft)"))
    elif name == 'cover_alt':
        src = html.escape(Path(assets[str(page['asset_ids'][0])]['path']).resolve().as_uri(), quote=True)
        parts = [kicker, _block('c-title-black', s['title'], 'font-size:88px;line-height:1.22;margin:24px 0')]
        if s.get('subtitle'):
            parts.append(_block('c-subtitle-accent', s['subtitle'], 'font-size:36px;margin:0 0 30px'))
        parts += ['<div class="c-img-hero" style="height:690px;flex-shrink:0"><img src="'+src+'" alt="'+html.escape(s['image_alt'], quote=True)+'"></div>', _block('c-caption', s['caption'])]
    elif name == "body":
        parts = [kicker, title]
        if s.get("lead"):
            parts.append('<div class="c-rule-v" style="margin:0 0 24px">'+_block("c-body", s["lead"], "font-size:34px;line-height:1.6")+"</div>")
        parts += [_block('c-body', value, 'font-size:34px;line-height:1.65;letter-spacing:0;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:18px') for value in s['paragraphs']]
        if s.get("quote"):
            parts.append(_block("c-quote", s["quote"], "font-size:48px;margin-top:28px"))
        if s.get("attribution"):
            parts.append(_block("c-quote-source", s["attribution"]))
    elif name == "quote":
        parts = [kicker, title, '<div class="c-rule"></div>', _block("c-quote c-quote--accent", s["quote"], "font-size:76px;margin:36px 0"), _block("c-quote-source", s["attribution"])]
        if s.get("note"):
            parts.append(_block("c-body", s["note"], "font-size:32px;margin-top:46px"))
    elif name == "list":
        parts = [kicker, title, '<div class="c-list2col">'+"".join('<div class="c-list2col-item"><span class="term">'+_e(v["term"])+'</span><span class="note">'+_e(v["note"])+"</span></div>" for v in s["items"])+"</div>"]
        if s.get("rule"):
            parts.append('<div class="c-framecard" style="margin-top:54px">'+_block("val", s["rule"])+"</div>")
    elif name == "map":
        parts = [kicker, title]
        for row in s["rows"]:
            parts.append('<div class="c-map-row"><div class="c-map-card">'+_block("logo", row["label"])+(_block("org", row["org"]) if row.get("org") else "")+'</div><div class="c-map-arrow">→</div><div class="c-map-block c-map-block--'+row["color"]+'">'+_block("name", row["name"])+(_block("meta", row["meta"]) if row.get("meta") else "")+"</div></div>")
    elif name == "table":
        parts = [kicker, title, '<table class="c-table" style="table-layout:fixed"><thead><tr>'+"".join('<th>'+_e(c)+'</th>' for c in s["columns"])+"</tr></thead><tbody>"+"".join('<tr>'+"".join('<td>'+_e(c)+'</td>' for c in row)+'</tr>' for row in s["rows"])+"</tbody></table>"]
        if s.get("verdict"):
            parts.append(_block("c-colorcard", s["verdict"], "font-size:36px;margin-top:30px"))
    elif name == "grid":
        imgs = []
        for aid, alt in zip(page["asset_ids"], s["image_alts"]):
            imgs.append('<img src="'+html.escape(Path(assets[str(aid)]["path"]).resolve().as_uri(), quote=True)+'" alt="'+html.escape(alt, quote=True)+'">')
        count = len(imgs)
        rows = 2 if count in (4, 6) else 3
        parts = [kicker, title, f'<div class="c-grid{count}" style="height:850px;flex-shrink:0;grid-template-rows:repeat({rows},1fr)">'+"".join(imgs)+"</div>", _block("c-caption", s["caption"])]
    elif name == 'shots':
        imgs = []
        for index, (aid, alt) in enumerate(zip(page['asset_ids'], s['image_alts'])):
            src = html.escape(Path(assets[str(aid)]['path']).resolve().as_uri(), quote=True)
            img = '<img src="'+src+'" alt="'+html.escape(alt, quote=True)+'">'
            if len(page['asset_ids']) == 3:
                img = '<div class="c-imgcell">'+img+_block('c-imgtag', 'STEP '+str(index+1).zfill(2))+'</div>'
            imgs.append(img)
        if len(imgs) == 3:
            layout = '<div class="c-imgrow" style="height:820px;flex-shrink:0">'+imgs[0]+'<div class="c-imgcol">'+''.join(imgs[1:])+'</div></div>'
        else:
            rows = 2
            layout = '<div class="c-grid'+str(len(imgs))+'" style="height:820px;flex-shrink:0;grid-template-rows:repeat('+str(rows)+',1fr)">'+''.join(imgs)+'</div>'
        parts = [kicker, title, layout, _block('c-caption', s['caption'])]
    elif name == "end":
        parts = [kicker, title, _block("c-colorcard", s["verdict"], "margin-top:80px;font-size:48px")]
        if s.get("credits"):
            parts.append('<div class="c-credits" style="margin-top:auto">'+"<br>".join(_e(v) for v in s["credits"])+"</div>")
    elif name == "frost":
        src = html.escape(Path(assets[str(page["asset_ids"][0])]["path"]).resolve().as_uri(), quote=True)
        ratio = s.get("frost_ratio", 0.42)
        parts = ['<div class="c-fullbleed"><img data-frost="'+str(ratio)+'" data-frost-target="card-frost" src="'+src+'" alt="'+html.escape(s["image_alt"], quote=True)+'"></div>', _block("c-overlay-kicker", s["kicker"]) if s.get("kicker") else "", '<div id="card-frost" class="c-frost"><div class="c-frost-body">'+_block("c-overlay-text", s["title"])+(_block("c-frost-foot", s["footer"]) if s.get("footer") else "")+"</div></div>"]
    if name == "frost":
        parts.append('<div class="c-logo c-logo--sm" style="position:absolute;bottom:28px;right:44px;color:#fff" aria-label="Account logo"></div>')
        body = "".join(parts)
        body_style = ' style="padding:0"'
    else:
        brand = _e(s.get("brand", ""))
        page_label = f"{number:02d} / {total:02d}" if total > 1 else ""
        top = f'<div class="c-topbar"><span class="brand">{brand}</span><span>{page_label}</span></div>'
        watermark = _block("c-pagenum-mark", f"{number:02d}") if total > 1 and name in ("body", "quote") else ""
        footer = '<div class="c-footer" style="align-items:center"><span style="display:flex;align-items:center;gap:16px"><span class="c-logo c-logo--sm c-logo--soft" aria-label="Account logo"></span>'+_e(s.get("footer", ""))+f'</span><span>{page_label}</span></div>'
        body = watermark+top+'<main class="page-body">'+"".join(parts)+"</main>"+footer
        body_style = ""
    links = "".join("<style>" + (_DIR / "css" / f).read_text(encoding="utf-8") + "</style>" for f in render.CSS_NAMES)
    # The renderer checks ancestors and footer. Do not hide overflow or shrink
    # typography dynamically to disguise invalid input.
    fixed = '<style>.page-body{min-height:0;flex-shrink:0} .c-topbar,.c-footer{flex-shrink:0} .c-fullbleed img{display:block} td,th{overflow-wrap:anywhere}</style>'
    return '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'+links+fixed+'<title>'+html.escape(s["title"])+f'</title></head><body class="theme-{page["theme"]}"'+body_style+'>'+body+"</body></html>"


def compile_spec(spec: dict) -> list:
    checked = validate_spec(spec)
    return [_document(page, checked["assets"], index+1, len(spec["pages"])) for index, page in enumerate(spec["pages"])]


def _atomic_json(path: Path, value) -> None:
    pending = path.with_suffix(".json.tmp")
    pending.write_bytes(_canonical(value))
    pending.replace(path)


def build_spec(spec: dict, workspace, out_dir, browser=None, spec_path=None, *, attempt_id=None) -> dict:
    """Render into an explicitly selected absolute directory within workspace."""
    checked = validate_spec(spec)
    raw_root, raw_output = Path(workspace), Path(out_dir)
    if not raw_root.is_absolute() or not raw_output.is_absolute():
        raise SpecError("workspace and out directory must be absolute")
    root, output = raw_root.resolve(), raw_output.resolve()
    if not root.is_dir() or not output.is_relative_to(root / "images"):
        raise SpecError("output must be inside workspace/images")
    input_hash = _hash(spec)
    attempt_id = validate_attempt_id(attempt_id or uuid.uuid4().hex)
    # A directory is claimed once. Concurrent identical specs never share paths;
    # reusing an explicit output is an error, not permission to overwrite evidence.
    output.mkdir(parents=True, exist_ok=False)
    template_hash = render.sha256_file(_DIR / "builder.py")
    schema_hash = render.sha256_file(_SCHEMA_PATH)
    manifest = {"ok": False, "version": 1,  "context": copy.deepcopy(spec["context"]),
                "input_sha256": input_hash, "template_sha256": template_hash,
                "schema_sha256": schema_hash, "renderer_sha256": render.sha256_file(_DIR / "render.py"),
                "css_sha256": render.css_hashes(), "cards": [], "review_status": "unreviewed",
                "manifest": "manifest.json", "mechanical_only": True,
                'attempt_id': attempt_id, 'platform_max_images': spec['platform_max_images'],
                'body': checked['body']}
    if spec_path is not None:
        manifest["spec"] = Path(spec_path).name
        manifest["spec_file_sha256"] = render.sha256_file(Path(spec_path))
    if checked["recipe"]:
        manifest["recipe_sha256"] = checked["recipe"]["recipe_sha256"]
    jobs = []
    for index, page in enumerate(spec["pages"]):
        stem = f"card-{index+1:02d}"
        html_path, png_path = output / (stem + ".html"), output / (stem + ".png")
        html_path.write_text(_document(page, checked["assets"], index+1, len(spec["pages"])), encoding="utf-8")
        source_images = []
        for aid in page.get("asset_ids", []):
            asset = copy.deepcopy(checked["assets"][str(aid)])
            asset["sha256"] = render.sha256_file(Path(asset["path"]))
            source_images.append(asset)
        card = {"index": index+1, "template": page["template"], "theme": page["theme"],
                "template_sha256": template_hash, "schema_sha256": schema_hash,
                "css_sha256": manifest["css_sha256"], "input_sha256": _hash({"page": page, "context": spec["context"]}),
                "context": copy.deepcopy(spec["context"]), "html": html_path.name, "png": png_path.name,
                "source_ids": page["source_ids"], "asset_ids": page.get("asset_ids", []),
                "source_images": source_images,
                "source_evidence": [checked["sources"][str(sid)] for sid in page["source_ids"]],
                "review_status": "unreviewed", "ok": False}
        manifest["cards"].append(card)
        jobs.append((html_path, png_path))
    def run(active_browser):
        for card, (html_path, png_path) in zip(manifest["cards"], jobs):
            result = render.render_one(active_browser, html_path, png_path, strict=True)
            card["ok"] = result["ok"]
            card["mechanical_checks"] = {**result, "html": html_path.name, "png": png_path.name}
            card["html_sha256"] = render.sha256_file(html_path)
            if result["ok"]:
                card["png_sha256"] = result["png_sha256"]
            else:
                card["error"] = result.get("error", "render failed")
            # Source pixels changing mid-render invalidate this manifest. This
            # check is mechanical only; DB approval and context recheck are upstream.
            for source in card["source_images"]:
                if source["sha256"] != render.sha256_file(Path(source["path"])):
                    card.update(ok=False, error="source image changed during render")
    try:
        if browser is not None:
            run(browser)
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                active_browser = playwright.chromium.launch()
                try:
                    run(active_browser)
                finally:
                    active_browser.close()
        manifest["ok"] = all(c["ok"] for c in manifest["cards"])
    except Exception as exc:
        manifest["error"] = str(exc)
    _atomic_json(output / "manifest.json", manifest)
    return manifest


def main(argv=None) -> int:
    print(json.dumps({"ok": False, "error": "Use scripts/cli.py render-card with an absolute --workspace"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
