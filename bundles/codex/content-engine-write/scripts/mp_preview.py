"""Portable MP preview, extracted styles and template logic from mp_renderer."""
import re
import sys
from html import escape
from pathlib import Path
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

STYLE_PRESETS = {
    "default": {
        "h1": "font-size:22px;font-weight:bold;color:#1a1a1a;margin:24px 0 12px;line-height:1.4;",
        "h2": "font-size:18px;font-weight:bold;color:#2a2a2a;margin:20px 0 10px;line-height:1.4;border-bottom:1px solid #eee;padding-bottom:8px;",
        "h3": "font-size:16px;font-weight:bold;color:#333;margin:16px 0 8px;line-height:1.4;",
        "p": "font-size:15px;color:#3f3f3f;line-height:1.8;margin:12px 0;letter-spacing:0.5px;",
        "blockquote": "border-left:3px solid #6c5ce7;padding:10px 16px;margin:16px 0;background:#f8f8fa;color:#555;font-size:14px;line-height:1.7;",
        "code": "background:#f4f4f8;padding:2px 6px;border-radius:3px;font-size:13px;color:#e74c3c;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #ddd;padding:8px 12px;background:#f5f5f5;font-weight:bold;text-align:left;",
        "td": "border:1px solid #ddd;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:4px;margin:12px 0;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:15px;color:#3f3f3f;line-height:1.8;margin:4px 0;",
        "strong": "color:#1a1a1a;",
    },
    "minimal": {
        "h1": "font-size:20px;font-weight:700;color:#111111;margin:24px 0 14px;line-height:1.4;",
        "h2": "font-size:17px;font-weight:700;color:#111111;margin:22px 0 10px;line-height:1.4;border-bottom:1px solid #e5e5e5;padding-bottom:8px;",
        "h3": "font-size:15px;font-weight:700;color:#333333;margin:18px 0 8px;line-height:1.4;",
        "p": "font-size:15px;color:#2b2b2b;line-height:1.82;margin:14px 0;",
        "blockquote": "border-left:3px solid #111111;padding:12px 16px;margin:20px 0;background-color:#f5f5f5;color:#222222;font-size:15px;line-height:1.8;font-weight:700;",
        "code": "background-color:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:13px;color:#c0392b;",
        "pre": "background-color:#f7f7f7;color:#333333;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #dddddd;padding:8px 12px;background-color:#f5f5f5;font-weight:700;text-align:left;",
        "td": "border:1px solid #dddddd;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:4px;margin:12px 0;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:15px;color:#2b2b2b;line-height:1.82;margin:4px 0;",
        "strong": "color:#111111;",
    },
    "editorial": {
        "h1": "font-size:20px;font-weight:700;color:#1a1a1a;margin:24px 0 14px;line-height:1.35;font-family:Georgia,'Times New Roman','Songti SC',serif;",
        "h2": "font-size:17px;font-weight:700;color:#1a1a1a;margin:24px 0 10px;line-height:1.4;border-bottom:2px solid #1a1a1a;padding-bottom:6px;font-family:Georgia,'Times New Roman','Songti SC',serif;",
        "h3": "font-size:15px;font-weight:700;color:#333333;margin:18px 0 8px;line-height:1.4;",
        "p": "font-size:15px;color:#2f2f2f;line-height:1.9;margin:16px 0;font-family:Georgia,'Times New Roman','Songti SC','Microsoft YaHei',serif;",
        "blockquote": "border-left:3px solid #1a1a1a;padding:12px 18px;margin:20px 0;background-color:#fafafa;color:#444444;font-size:15px;line-height:1.8;font-style:italic;",
        "code": "background-color:#f2f2f2;padding:2px 6px;border-radius:2px;font-size:13px;color:#b0272a;",
        "pre": "background-color:#fafafa;color:#2f2f2f;padding:16px;border:1px solid #e5e5e5;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border-bottom:2px solid #1a1a1a;padding:8px 12px;font-weight:700;text-align:left;",
        "td": "border-bottom:1px solid #e5e5e5;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:2px;margin:12px 0;",
        "ul": "margin:12px 0;padding-left:26px;",
        "ol": "margin:12px 0;padding-left:26px;",
        "li": "font-size:15px;color:#2f2f2f;line-height:1.9;margin:4px 0;",
        "strong": "color:#1a1a1a;",
    },
    # gzh-design 主题（6 套）
    "gzh-moyu-green": {
        "h1": "font-size:22px;font-weight:bold;color:#111827;margin:24px 0 12px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h2": "font-size:18px;font-weight:bold;color:#111827;margin:20px 0 10px;line-height:1.4;border-bottom:2px solid #059669;padding-bottom:8px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h3": "font-size:16px;font-weight:bold;color:#111827;margin:16px 0 8px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "p": "font-size:14px;color:#374151;line-height:1.9;margin:12px 0;letter-spacing:0.5px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "blockquote": "border-left:4px solid #059669;padding:12px 16px;margin:16px 0;background:#F3F4F6;color:#374151;font-size:14px;line-height:1.7;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "code": "background:#F3F4F6;padding:2px 6px;border-radius:3px;font-size:13px;color:#059669;font-family:Consolas,Monaco,'Courier New',monospace;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;font-family:Consolas,Monaco,'Courier New',monospace;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #D1D5DB;padding:8px 12px;background:#F3F4F6;font-weight:bold;text-align:left;",
        "td": "border:1px solid #D1D5DB;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:14px;color:#374151;line-height:1.9;margin:4px 0;",
        "strong": "color:#111827;font-weight:600;",
    },
    "gzh-red-white": {
        "h1": "font-size:22px;font-weight:bold;color:#1C1917;margin:24px 0 12px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h2": "font-size:18px;font-weight:bold;color:#1C1917;margin:20px 0 10px;line-height:1.4;border-bottom:2px solid #DC2626;padding-bottom:8px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h3": "font-size:16px;font-weight:bold;color:#1C1917;margin:16px 0 8px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "p": "font-size:15px;color:#374151;line-height:1.8;margin:12px 0;letter-spacing:0.5px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "blockquote": "border-left:4px solid #DC2626;padding:12px 16px;margin:16px 0;background:#F9FAFB;color:#374151;font-size:14px;line-height:1.7;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "code": "background:#F9FAFB;padding:2px 6px;border-radius:3px;font-size:13px;color:#DC2626;font-family:Consolas,Monaco,'Courier New',monospace;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;font-family:Consolas,Monaco,'Courier New',monospace;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #E5E7EB;padding:8px 12px;background:#F9FAFB;font-weight:bold;text-align:left;",
        "td": "border:1px solid #E5E7EB;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:15px;color:#374151;line-height:1.8;margin:4px 0;",
        "strong": "color:#1C1917;font-weight:600;",
    },
    "gzh-graphite": {
        "h1": "font-size:22px;font-weight:bold;color:#111827;margin:24px 0 12px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h2": "font-size:18px;font-weight:bold;color:#111827;margin:20px 0 10px;line-height:1.4;border-bottom:2px solid #52525B;padding-bottom:8px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h3": "font-size:16px;font-weight:bold;color:#111827;margin:16px 0 8px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "p": "font-size:15px;color:#374151;line-height:1.8;margin:12px 0;letter-spacing:0.5px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "blockquote": "border-left:4px solid #52525B;padding:12px 16px;margin:16px 0;background:#F9FAFB;color:#374151;font-size:14px;line-height:1.7;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "code": "background:#F9FAFB;padding:2px 6px;border-radius:3px;font-size:13px;color:#52525B;font-family:Consolas,Monaco,'Courier New',monospace;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;font-family:Consolas,Monaco,'Courier New',monospace;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #E5E7EB;padding:8px 12px;background:#F9FAFB;font-weight:bold;text-align:left;",
        "td": "border:1px solid #E5E7EB;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:15px;color:#374151;line-height:1.8;margin:4px 0;",
        "strong": "color:#111827;font-weight:600;",
    },
    "gzh-olive": {
        "h1": "font-size:22px;font-weight:bold;color:#23251d;margin:24px 0 12px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h2": "font-size:18px;font-weight:bold;color:#23251d;margin:20px 0 10px;line-height:1.4;border-bottom:2px solid #ed7b2f;padding-bottom:8px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h3": "font-size:16px;font-weight:bold;color:#23251d;margin:16px 0 8px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "p": "font-size:14px;color:#4d4f46;line-height:1.9;margin:12px 0;letter-spacing:0.5px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "blockquote": "border-left:4px solid #ed7b2f;padding:12px 16px;margin:16px 0;background:#F9FAFB;color:#4d4f46;font-size:14px;line-height:1.7;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "code": "background:#F9FAFB;padding:2px 6px;border-radius:3px;font-size:13px;color:#ed7b2f;font-family:Consolas,Monaco,'Courier New',monospace;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;font-family:Consolas,Monaco,'Courier New',monospace;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #bfc1b7;padding:8px 12px;background:#F9FAFB;font-weight:bold;text-align:left;",
        "td": "border:1px solid #bfc1b7;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:14px;color:#4d4f46;line-height:1.9;margin:4px 0;",
        "strong": "color:#23251d;font-weight:600;",
    },
    "gzh-zen": {
        "h1": "font-size:22px;font-weight:bold;color:#2B2B2B;margin:24px 0 12px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h2": "font-size:18px;font-weight:bold;color:#2B2B2B;margin:20px 0 10px;line-height:1.4;border-bottom:2px solid #4A5D52;padding-bottom:8px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h3": "font-size:16px;font-weight:bold;color:#2B2B2B;margin:16px 0 8px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "p": "font-size:15px;color:#525252;line-height:1.8;margin:12px 0;letter-spacing:0.5px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "blockquote": "border-left:4px solid #4A5D52;padding:12px 16px;margin:16px 0;background:#F9FAFB;color:#525252;font-size:14px;line-height:1.7;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "code": "background:#F9FAFB;padding:2px 6px;border-radius:3px;font-size:13px;color:#4A5D52;font-family:Consolas,Monaco,'Courier New',monospace;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;font-family:Consolas,Monaco,'Courier New',monospace;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #E5E7EB;padding:8px 12px;background:#F9FAFB;font-weight:bold;text-align:left;",
        "td": "border:1px solid #E5E7EB;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:15px;color:#525252;line-height:1.8;margin:4px 0;",
        "strong": "color:#2B2B2B;font-weight:600;",
    },
    "gzh-ticket": {
        "h1": "font-size:22px;font-weight:bold;color:#1a1a1a;margin:24px 0 12px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h2": "font-size:18px;font-weight:bold;color:#1a1a1a;margin:20px 0 10px;line-height:1.4;border-bottom:2px solid #059669;padding-bottom:8px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "h3": "font-size:16px;font-weight:bold;color:#1a1a1a;margin:16px 0 8px;line-height:1.4;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "p": "font-size:14px;color:#555;line-height:1.9;margin:12px 0;letter-spacing:0.5px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "blockquote": "border-left:4px solid #059669;padding:12px 16px;margin:16px 0;background:#F9FAFB;color:#555;font-size:14px;line-height:1.7;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;",
        "code": "background:#F9FAFB;padding:2px 6px;border-radius:3px;font-size:13px;color:#059669;font-family:Consolas,Monaco,'Courier New',monospace;",
        "pre": "background:#282c34;color:#abb2bf;padding:16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;margin:16px 0;font-family:Consolas,Monaco,'Courier New',monospace;",
        "table": "width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;",
        "th": "border:1px solid #E5E7EB;padding:8px 12px;background:#F9FAFB;font-weight:bold;text-align:left;",
        "td": "border:1px solid #E5E7EB;padding:8px 12px;",
        "img": "max-width:100%;height:auto;border-radius:8px;margin:16px 0;display:block;",
        "ul": "margin:12px 0;padding-left:24px;",
        "ol": "margin:12px 0;padding-left:24px;",
        "li": "font-size:14px;color:#555;line-height:1.9;margin:4px 0;",
        "strong": "color:#1a1a1a;font-weight:600;",
    },
}

_STYLE_ORDER = ["h1", "h2", "h3", "p", "blockquote", "code", "pre",
                "table", "th", "td", "img", "ul", "ol", "li", "strong"]

def auto_select_theme(content_role: str, frontmatter: dict) -> str:
    """根据 content_role 自动选择 gzh-design 主题

    优先级：
    1. frontmatter 中显式指定的 theme 字段
    2. content_role 自动映射
    3. 默认 gzh-moyu-green

    映射关系：
    - traffic: gzh-moyu-green（流量型：信息密度高，卡片丰富）
    - product_insight: gzh-graphite（产品洞察：专业、克制）
    - deep_dive: gzh-olive（深度拆解：内刊质感）
    - trend: gzh-red-white（趋势判断：力量感）
    """
    # 显式指定优先
    if "theme" in frontmatter:
        theme = frontmatter["theme"]
        # 验证主题是否存在
        if theme in STYLE_PRESETS:
            return theme
        # 兼容旧的 style 字段
        if theme in ["default", "minimal", "editorial"]:
            return theme

    # content_role 映射
    theme_map = {
        "traffic": "gzh-moyu-green",
        "product_insight": "gzh-graphite",
        "deep_dive": "gzh-olive",
        "trend": "gzh-red-white",
    }

    return theme_map.get(content_role, "gzh-moyu-green")

def inline_styles(html: str, style_id: str = "default") -> str:
    """Inline every style attribute.

    WeChat strips <style> blocks and external CSS, so each tag must carry its
    own style attribute. The style string is taken from STYLE_PRESETS; the
    regex substitution order (see _STYLE_ORDER) is preserved from the original
    behaviour so the "default" preset renders identically.
    """
    tokens = STYLE_PRESETS.get(style_id, STYLE_PRESETS["default"])
    styled_html = html
    for tag in _STYLE_ORDER:
        style = tokens.get(tag)
        if not style:
            continue
        styled_html = re.sub(
            rf"<{tag}(.*?)>", rf'<{tag}\1 style="{style}">', styled_html)
    return styled_html

_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)

def _strip_duplicate_h1(html: str, title: str) -> str:
    """去掉正文里与 frontmatter 标题重复的首个 <h1>，消除公众号双标题。"""
    if not title:
        return html
    m = _H1_RE.search(html)
    if not m:
        return html
    inner_plain = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if inner_plain == title.strip():
        html = _H1_RE.sub("", html, count=1)
    return html

_SECTION_FONT = ("-apple-system,BlinkMacSystemFont,'Helvetica Neue',"
                 "'PingFang SC','Microsoft YaHei',sans-serif")

def _minimal_section(html_content: str) -> str:
    """Fallback wrapper with no template syntax — always safe to publish."""
    return (
        f'<section style="padding:16px 20px;max-width:100%;'
        f'font-family:{_SECTION_FONT};">\n{html_content}\n'
        '<p style="font-size:13px;color:#999;text-align:center;'
        'margin-top:32px;">— END —</p>\n</section>'
    )

def load_base_template() -> str:
    template_path = TEMPLATES_DIR / "mp_base.html"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return "{{ content }}"

def wrap_with_template(html_content: str, title: str, author: str) -> str:
    """Wrap the article body in templates/mp_base.html.

    Jinja2 is required for the real template because it uses {% if %} blocks —
    a plain str.replace() fallback would leave literal "{% if author %}" text
    in the published article. If Jinja2 is unavailable we fall back to the
    minimal inline template instead, which contains no control flow.
    """
    template = load_base_template()
    try:
        from jinja2 import Template
    except ImportError:
        print("[WARN] Jinja2 not installed; using minimal template. "
              "Run: pip install Jinja2", file=sys.stderr)
        return _minimal_section(html_content)

    return Template(template).render(title=escape(title), author=escape(author), content=html_content)

# The portable entry accepts validated JSON prose, never arbitrary HTML/CSS or
# templates supplied by the caller. Markdown raw tags are escaped before parse.
from html.parser import HTMLParser
from urllib.parse import urlsplit
import importlib.util
_common_spec = importlib.util.spec_from_file_location('_portable_mp_preview_common',
                                                     Path(__file__).resolve().parent / 'common.py')
_common = importlib.util.module_from_spec(_common_spec)
_common_spec.loader.exec_module(_common)
PortableError, safe_path, _require = _common.PortableError, _common.safe_path, _common._require

_ALLOWED = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code',
            'em', 'strong', 'ul', 'ol', 'li', 'a', 'br', 'hr', 'table', 'thead',
            'tbody', 'tr', 'th', 'td'}

class _SafeProse(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []

    def handle_starttag(self, tag, attrs):
        _require(tag in _ALLOWED, 'Unsupported preview tag; images belong to assets')
        output_attrs = []
        if tag == 'a':
            hrefs = [v for k, v in attrs if k == 'href']
            _require(len(hrefs) == 1 and type(hrefs[0]) is str, 'Preview link requires one URL')
            href = hrefs[0]
            _require(not any(ord(char) <= 32 or ord(char) == 127 or char == chr(92)
                             for char in href), 'Unsafe preview URL')
            parts = urlsplit(href)
            _require(parts.scheme in ('http', 'https', 'mailto') and
                     (parts.scheme == 'mailto' or bool(parts.hostname)) and
                     parts.username is None and parts.password is None, 'Unsafe preview URL scheme')
            output_attrs.append('href="' + escape(href, quote=True) + '"')
        # Drop Markdown-generated ids/classes/table inline styles and all caller
        # attributes. Only the known style presets add CSS after this sanitizer.
        self.output.append('<' + tag + (' ' + ' '.join(output_attrs) if output_attrs else '') + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        _require(tag in _ALLOWED, 'Unsupported preview tag')
        self.output.append('</' + tag + '>')

    def handle_data(self, data):
        self.output.append(escape(data))

    def handle_comment(self, data):
        raise PortableError('Preview HTML comments forbidden')


def preview(draft, style='default'):
    _require(style in STYLE_PRESETS, 'Unknown preview style')
    try:
        import markdown
    except ImportError as exc:
        raise PortableError('Markdown required for MP preview', 'dependency_missing') from exc
    # Escape angle brackets only, retaining Markdown punctuation and ampersands.
    body = draft['body'].replace('<', '&lt;').replace('>', '&gt;')
    generated = markdown.markdown(body, extensions=['fenced_code', 'tables', 'nl2br'])
    sanitizer = _SafeProse()
    sanitizer.feed(generated)
    sanitizer.close()
    styled = inline_styles(''.join(sanitizer.output), style_id=style)
    styled = _strip_duplicate_h1(styled, draft['title'])
    return {'title': draft['title'], 'html': wrap_with_template(styled, draft['title'], ''),
            'word_count': len(draft['body']), 'publishing_disabled': True}


def article_image(root, value):
    """Local relative cover/body references share one boundary (no article_dir bug)."""
    return safe_path(root, value)
