"""Render the accepted 1080x1440 card layouts; layout checks are NOT visual approval.

Portable CLI: use ../scripts/cli.py render-card (structured templates only).
Playwright is deliberately imported only when a validated rendering job starts.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent


THEMES = frozenset(("cream-light", "navy-dark", "black", "olive", "wine", "gold", "paper", "purple"))
CSS_NAMES = ("tokens.css", "base.css", "components.css")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def css_hashes() -> dict:
    return {name: sha256_file(_SCRIPT_DIR / "css" / name) for name in CSS_NAMES}


def _inject_base_css(page) -> None:
    for name in CSS_NAMES:
        page.add_style_tag(content=(_SCRIPT_DIR / "css" / name).read_text(encoding="utf-8"))
    # Legacy fullbleed samples use an inline img. Its text baseline adds a
    # phantom 4-5px scroll area even when the picture exactly fills the canvas.
    page.add_style_tag(content=".c-fullbleed img { display: block; }")


_CHECK_JS = r"""() => {
  const W = window.innerWidth, H = window.innerHeight;
  const res = {broken_images: [], page_overflow: false, out_of_canvas: [],
    clipped_text: [], footer_overlap: [], theme_missing: false, canvas_mismatch: false};
  const themes = ['cream-light','navy-dark','black','olive','wine','gold','paper','purple'];
  const body = document.body, bcs = getComputedStyle(body), br = body.getBoundingClientRect();
  const selected = [...body.classList].filter(c => c.startsWith('theme-'));
  res.theme_missing = selected.length !== 1 || !themes.includes(selected[0].slice(6)) ||
    !['--bg','--ink','--accent'].every(v => bcs.getPropertyValue(v).trim());
  res.canvas_mismatch = Math.abs(br.width-W)>1 || Math.abs(br.height-H)>1 ||
    Math.abs(br.left)>1 || Math.abs(br.top)>1;
  for (const img of document.images) {
    if (!img.complete || img.naturalWidth === 0) res.broken_images.push(img.src.split('/').pop());
  }
  const de = document.documentElement;
  res.page_overflow = de.scrollHeight > H+1 || de.scrollWidth > W+1 ||
    body.scrollHeight > H+1 || body.scrollWidth > W+1;
  const label = el => (el.getAttribute('class') || el.tagName).trim().split(/\s+/)[0];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('.c-fullbleed,.c-pagenum-mark')) continue;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (r.left < -2 || r.top < -2 || r.right > W+2 || r.bottom > H+2)
      res.out_of_canvas.push(label(el)+' ['+Math.round(r.left)+','+Math.round(r.top)+
        ' '+Math.round(r.width)+'x'+Math.round(r.height)+']');
  }
  // Text ranges, not just the leaf's overflow style: a flex/absolute ancestor
  // can clip otherwise perfectly normal paragraphs. Decorative layers are exempt.
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  const footer = document.querySelector('.c-footer');
  const fr = footer && footer.getBoundingClientRect();
  const footerTop = fr ? fr.top + parseFloat(getComputedStyle(footer).paddingTop) : H;
  let node;
  while ((node = walker.nextNode())) {
    const el = node.parentElement;
    if (!node.textContent.trim() || !el || el.closest('script,style,.c-pagenum-mark')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const range = document.createRange(); range.selectNodeContents(node);
    const rects = [...range.getClientRects()].filter(r => r.width && r.height);
    for (let a = el; a; a = a.parentElement) {
      const acs = getComputedStyle(a), ar = a.getBoundingClientRect();
      const clipX = ['hidden','clip','auto','scroll'].includes(acs.overflowX);
      const clipY = ['hidden','clip','auto','scroll'].includes(acs.overflowY);
      if (!clipX && !clipY) continue;
      const left = ar.left+a.clientLeft, top = ar.top+a.clientTop;
      if (rects.some(r => (clipX && (r.left<left-2 || r.right>left+a.clientWidth+2)) ||
                         (clipY && (r.top<top-2 || r.bottom>top+a.clientHeight+2)))) {
        res.clipped_text.push(label(el)+' via '+label(a)+': '+node.textContent.trim().slice(0,24));
        break;
      }
    }
    // Only normal content vs normal footer; watermark/fullbleed/frost overlays
    // intentionally overlap and must not generate false footer alarms.
    if (fr && !el.closest('.c-footer,.c-pagenum-mark,.c-fullbleed,.c-frost,.c-overlay-text,.c-overlay-kicker')) {
      if (rects.some(r => r.bottom>footerTop+2 && r.top<fr.bottom && r.right>fr.left && r.left<fr.right))
        res.footer_overlap.push(label(el)+': '+node.textContent.trim().slice(0,24));
    }
  }
  for (const key of ['out_of_canvas','clipped_text','footer_overlap']) res[key] = [...new Set(res[key])];
  return res;
}"""

_READY_JS = r"""async timeout => {
  const bounded = async (promise, name) => {
    let timer;
    try { return await Promise.race([promise.then(() => null, e => name+': '+String(e)),
      new Promise(resolve => { timer=setTimeout(() => resolve(name+': timeout'), timeout); })]); }
    finally { clearTimeout(timer); }
  };
  const font = await bounded(document.fonts.ready, 'font readiness');
  const imgs = await Promise.all([...document.images].map((img,i) =>
    bounded(img.decode(), 'image decode '+i)));
  return [font,...imgs].filter(Boolean);
}"""

_FROST_JS = r"""() => [...document.querySelectorAll('img[data-frost]')].map((img,i) => {
  const explicit = img.getAttribute('data-frost-target');
  const scope = img.closest('.c-fullbleed')?.parentElement || img.parentElement;
  const target = explicit ? document.getElementById(explicit) : scope?.querySelector('.c-frost');
  const rect = el => {const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
  const cs = getComputedStyle(img);
  return {index:i,src:img.currentSrc || img.src,ratio:img.getAttribute('data-frost') || '0.42',
    fit:cs.objectFit,position:cs.objectPosition,rect:rect(img),target:target ? rect(target) : null};
})"""


def _local_image(src: str, html_path: Path) -> Path:
    parsed = urlparse(src)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        if parsed.netloc:
            path = "//" + parsed.netloc + path
        return Path(path)
    if parsed.scheme:
        raise ValueError("frost requires a local bound image")
    return (html_path.parent / unquote(src)).resolve()


def _frost_color(item: dict, html_path: Path) -> tuple:
    """Sample the actually displayed object-fit crop, intersecting its title panel."""
    from PIL import Image, ImageOps
    ratio = float(item["ratio"])
    if not math.isfinite(ratio) or not 0 < ratio <= 1:
        raise ValueError("data-frost ratio must be finite and in (0, 1]")
    rect = item["rect"]
    dw, dh = rect["width"], rect["height"]
    if dw <= 0 or dh <= 0:
        raise ValueError("frost image has an empty rendered rectangle")
    with Image.open(_local_image(item["src"], html_path)) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
    iw, ih = im.size
    fit = item["fit"]
    if fit == "cover":
        sx = sy = max(dw/iw, dh/ih)
    elif fit in ("contain", "scale-down"):
        sx = sy = min(dw/iw, dh/ih, 1) if fit == "scale-down" else min(dw/iw, dh/ih)
    elif fit == "none":
        sx = sy = 1
    elif fit == "fill":
        sx, sy = dw/iw, dh/ih
    else:
        raise ValueError("unsupported frost object-fit")
    rw, rh = iw*sx, ih*sy
    positions = item["position"].split()
    if len(positions) != 2:
        raise ValueError("unsupported frost object-position")
    def offset(value, free):
        if value.endswith("%"):
            return float(value[:-1])*free/100
        if value.endswith("px"):
            return float(value[:-2])
        raise ValueError("unsupported frost object-position")
    ox, oy = offset(positions[0], dw-rw), offset(positions[1], dh-rh)
    left, top, right, bottom = 0, dh*(1-ratio), dw, dh
    target = item.get("target")
    if target:
        left = max(left, target["x"]-rect["x"])
        top = max(top, target["y"]-rect["y"])
        right = min(right, target["x"]+target["width"]-rect["x"])
        bottom = min(bottom, target["y"]+target["height"]-rect["y"])
    # Ignore contain letterboxing: only pixels from the displayed source image.
    box = (max(0, (left-ox)/sx), max(0, (top-oy)/sy),
           min(iw, (right-ox)/sx), min(ih, (bottom-oy)/sy))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("frost title area does not intersect the source image")
    color = im.crop(box).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    return tuple(int(c*0.45) for c in color)


def _frost_tint(page, html_path: Path) -> dict:
    result = {"warnings": [], "tints": []}
    for item in page.evaluate(_FROST_JS):
        try:
            if not item.get("target"):
                raise ValueError("data-frost image has no associated .c-frost title panel")
            rgb = _frost_color(item, html_path)
            tint = f"rgba({rgb[0]},{rgb[1]},{rgb[2]},0.78)"
            page.evaluate(r"""({index,tint}) => {
              const img=document.querySelectorAll('img[data-frost]')[index];
              const explicit=img.getAttribute('data-frost-target');
              const scope=img.closest('.c-fullbleed')?.parentElement || img.parentElement;
              const target=explicit ? document.getElementById(explicit) : scope.querySelector('.c-frost');
              target.style.setProperty('--frost-tint',tint);
            }""", {"index": item["index"], "tint": tint})
            result["tints"].append({"image": item["src"], "tint": tint, "region": item["target"]})
        except Exception as exc:
            result["warnings"].append(f"frost tint: {exc}")
    return result


def render_one(browser, html_path: Path, out_path: Path,
               width: int = 1080, height: int = 1440, scale: float = 1.0,
               strict: bool = False, readiness_timeout_ms: int = 8000) -> dict:
    """Backward-compatible entry point. Strict failures never produce a new PNG."""
    html_path, out_path = Path(html_path).resolve(), Path(out_path).resolve()
    result = {"ok": False, "html": str(html_path), "png": str(out_path), "warnings": []}
    page = None
    try:
        if (width, height) != (1080, 1440):
            raise ValueError("card canvas must be 1080x1440; use --scale for higher density")
        if not math.isfinite(scale) or not 0 < scale <= 4:
            raise ValueError("scale must be finite and in (0, 4]")
        if not 1 <= readiness_timeout_ms <= 30000:
            raise ValueError("readiness timeout must be 1..30000 milliseconds")
        if not html_path.is_file():
            raise ValueError(f"HTML file missing: {html_path}")
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
        allowed = {html_path, *(_SCRIPT_DIR / "css" / name for name in CSS_NAMES)}
        def local_only(route):
            parsed = urlparse(route.request.url)
            if parsed.scheme == "data":
                route.continue_()
            elif parsed.scheme == "file" and _local_image(route.request.url, html_path).resolve() in allowed:
                route.continue_()
            else:
                route.abort()
        page.route("**/*", local_only)
        page.goto(html_path.as_uri(), wait_until="domcontentloaded", timeout=10000)
        _inject_base_css(page)
        warnings = page.evaluate(_READY_JS, readiness_timeout_ms)
        frost = _frost_tint(page, html_path)
        warnings += frost["warnings"]
        checks = page.evaluate(_CHECK_JS)
        warnings += (["主题缺失: require one known theme and loaded tokens"] if checks["theme_missing"] else [])
        warnings += (["画布不匹配: body must fill 1080x1440"] if checks["canvas_mismatch"] else [])
        warnings += [f"破图: {name}" for name in checks["broken_images"]]
        warnings += (["页面溢出: 内容超过画布"] if checks["page_overflow"] else [])
        for key, title in (("out_of_canvas", "元素越界"), ("clipped_text", "文本裁切"), ("footer_overlap", "页脚重叠")):
            warnings += [f"{title}: {detail}" for detail in checks[key][:8]]
        result.update(warnings=warnings, checks=checks, frost_tints=frost["tints"],
                      canvas={"width": width, "height": height, "scale": scale},
                      html_sha256=sha256_file(html_path), css_sha256=css_hashes())
        for warning in warnings:
            print(warning, file=sys.stderr)
        if strict and warnings:
            result["error"] = "strict layout checks failed (not visual approval)"
            return result
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_path), full_page=False)
        result.update(ok=True, png_sha256=sha256_file(out_path), review_status="unreviewed")
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                result.update(ok=False, error=f"page close failed: {exc}")
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None) -> int:
    print(json.dumps({"ok": False, "error": "Use scripts/cli.py render-card; free HTML is not accepted"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
