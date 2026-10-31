"""Safe, original NamuMark-compatible renderer for NamuLite Wiki.

The renderer deliberately implements a useful subset of NamuMark instead of
claiming full the-seed compatibility. User-supplied raw HTML and arbitrary CSS
are never emitted as executable markup.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"

TOC_MARKER = "\x00NAMULITE_TOC\x00"
FOOTNOTES_MARKER = "\x00NAMULITE_FOOTNOTES\x00"

_SAFE_COLORS = {
    "black", "white", "red", "green", "blue", "yellow", "orange", "purple",
    "pink", "gray", "grey", "silver", "maroon", "navy", "teal", "olive",
    "lime", "aqua", "fuchsia", "transparent", "tomato", "crimson", "coral",
    "gold", "indigo", "violet", "brown", "rebeccapurple", "darkred", "darkblue",
    "darkgreen", "lightgray", "lightgrey", "skyblue", "steelblue", "whitesmoke",
}


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _safe_attr(text: str) -> str:
    return html.escape(text, quote=True)


def _internal_href(title: str) -> str:
    return "/w/" + quote(title.strip(), safe="")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w가-힣-]+", "-", text, flags=re.UNICODE).strip("-_").lower()
    return slug or "section"


def _valid_color(value: str) -> str | None:
    value = value.strip().lower()
    if value in _SAFE_COLORS:
        return value
    if re.fullmatch(r"#[0-9a-f]{3}(?:[0-9a-f]{3}|[0-9a-f]{5})?", value):
        return value
    return None


def _valid_color_option(value: str) -> str | None:
    """Pick the first safe color from NamuMark's light,dark color syntax."""
    for candidate in value.split(","):
        safe = _valid_color(candidate)
        if safe:
            return safe
    return None


def _safe_dimension(value: str, *, allow_percent: bool = True, maximum: int = 2400) -> str | None:
    value = value.strip().lower()
    match = re.fullmatch(r"(\d{1,4})(px|%|em)?", value)
    if not match:
        return None
    number = int(match.group(1))
    unit = match.group(2) or "px"
    if number < 1 or number > maximum or (unit == "%" and not allow_percent and number > 100):
        return None
    return f"{number}{unit}"


@dataclass
class RenderContext:
    headings: list[dict[str, object]] = field(default_factory=list)
    slug_counts: dict[str, int] = field(default_factory=dict)
    footnotes: list[dict[str, object]] = field(default_factory=list)
    footnote_by_key: dict[str, int] = field(default_factory=dict)
    footnote_ref_counts: dict[int, int] = field(default_factory=dict)


def _render_file(raw: str) -> str:
    """Render local uploaded images. File names are constrained to basenames."""
    target, _, option_text = raw.partition("|")
    name = target[3:].strip()
    if not name or Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_. -]{1,180}", name):
        return f'<span class="unsupported">[잘못된 파일 이름: {_escape(name)}]</span>'
    if name.startswith(".") or name in {".", ".."}:
        return '<span class="unsupported">[잘못된 파일 이름]</span>'
    path = UPLOAD_DIR / name
    # In Vercel mode, uploaded bytes live in the primary PostgreSQL database,
    # so the local filesystem cannot be used to check whether the image exists.
    if (not path.is_file() and not os.environ.get("DATABASE_URL")) or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return f'<span class="missing-file">[파일을 찾을 수 없음: {_escape(name)}]</span>'

    width = None
    height = None
    alt = Path(name).stem
    align = ""
    thumbnail = False
    options = []
    for group in option_text.split("|"):
        options.extend(group.split("&"))
    for option in options:
        option = option.strip()
        low = option.lower()
        if low.startswith("width="):
            width = _safe_dimension(option.split("=", 1)[1], maximum=2000)
        elif low.startswith("height="):
            height = _safe_dimension(option.split("=", 1)[1], maximum=2000)
        elif low.startswith("alt="):
            alt = option.split("=", 1)[1].strip()[:200] or alt
        elif low in {"left", "right", "center"}:
            align = low
        elif low.startswith("align=") and low.split("=", 1)[1] in {"left", "right", "center"}:
            align = low.split("=", 1)[1]
        elif low in {"thumb", "thumbnail", "섬네일"}:
            thumbnail = True

    attrs = [f'src="/static/uploads/{quote(name, safe="")}"', f'alt="{_safe_attr(alt)}"', 'loading="lazy"', 'decoding="async"']
    image_styles = []
    if width:
        if width.endswith("px"):
            attrs.append(f'width="{_safe_attr(width[:-2])}"')
            image_styles.extend(["max-width:100%", "height:auto"])
        else:
            image_styles.extend([f"max-width:{width}", "height:auto"])
    if height:
        if height.endswith("px"):
            attrs.append(f'height="{_safe_attr(height[:-2])}"')
        image_styles.append(f"max-height:{height}")
    if not image_styles:
        image_styles.extend(["max-width:100%", "height:auto"])
    attrs.append(f'style="{_safe_attr(";".join(image_styles))}"')
    image = "<img " + " ".join(attrs) + ">"
    classes = "wiki-image"
    if align:
        classes += " align-" + align
    if thumbnail:
        classes += " thumbnail"
    return f'<span class="{classes}">{image}</span>'


def _footnote_ref(raw_inner: str, context: RenderContext) -> str:
    """Handle [* note], [*A named note], and a repeated [*A] reference."""
    # A leading space means the first word is note text, not a reference label.
    text = raw_inner
    key: str | None = None
    body = text.strip()
    if text and not text[0].isspace():
        named = re.match(r"^([A-Za-z가-힣][A-Za-z0-9가-힣_-]{0,19})\s+(.+)$", text.strip(), re.DOTALL)
        if named:
            key = named.group(1)
            body = named.group(2).strip()
        elif re.fullmatch(r"[A-Za-z가-힣][A-Za-z0-9가-힣_-]{0,19}", text.strip()) and text.strip() in context.footnote_by_key:
            key = text.strip()
            body = ""

    note_index = context.footnote_by_key.get(key) if key else None
    if note_index is None:
        note_index = len(context.footnotes)
        note = {"number": note_index + 1, "key": key, "body": body, "refs": []}
        context.footnotes.append(note)
        if key:
            context.footnote_by_key[key] = note_index
    else:
        note = context.footnotes[note_index]
        # Repeated named references use the first definition.
        if not note.get("body") and body:
            note["body"] = body

    context.footnote_ref_counts[note_index] = context.footnote_ref_counts.get(note_index, 0) + 1
    ref_count = context.footnote_ref_counts[note_index]
    ref_id = f"fnref-{note_index + 1}" + (f"-{ref_count}" if ref_count > 1 else "")
    note["refs"].append(ref_id)  # type: ignore[union-attr]
    display = str(note.get("key") or note.get("number"))
    return (f'<sup class="footnote-ref" id="{ref_id}">'
            f'<a href="#fn-{note_index + 1}" aria-label="각주 {display}">[{_escape(display)}]</a></sup>')


_TOKEN_RE = re.compile(
    r"(?P<link>\[\[[^\]\n]+\]\])"
    r"|(?P<footnote>\[\*[^\]\n]*\])"
    r"|(?P<bold>'''[\s\S]*?''')"
    r"|(?P<italic>''[\s\S]*?'')"
    r"|(?P<underline>__[\s\S]*?__)"
    r"|(?P<strike>~~[\s\S]*?~~)"
    r"|(?P<strike_dash>--[\s\S]*?--)"
    r"|(?P<sup>\^\^[\s\S]*?\^\^)"
    r"|(?P<sub>,,[\s\S]*?,,)"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<macro>\[(?:br|clearfix|목차|각주|tableofcontents|footnote|date|datetime)\])"
    r"|(?P<param_macro>\[(?:age|dday|anchor|ruby|youtube|vimeo|nicovideo|kakaotv|navertv)\([^\]\n]*\)\])"
    r"|(?P<url>(?:https?|ftp)://[^\s<>]+)",
    re.IGNORECASE,
)


def _render_inline_braces(token: str, context: RenderContext, depth: int) -> str:
    inner = token[3:-3]
    # Paired light,dark foreground colors switch with the active theme.
    theme_color = re.match(r"^#([0-9A-Fa-f]{6}),#?([0-9A-Fa-f]{6})\s+([\s\S]*)$", inner)
    if theme_color:
        light, dark, body = theme_color.groups()
        return (f'<span class="wiki-theme-color" style="--nm-light-color:#{light};'
                f'--nm-dark-color:#{dark}">{render_inline(body, context, depth + 1)}</span>')
    # Inline monospace is the default form.
    color_match = re.match(r"^#((?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})|[A-Za-z]+)(?:,(#?(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8}|[A-Za-z]+)))?\s+([\s\S]*)$", inner)
    if color_match:
        fg = _valid_color("#" + color_match.group(1) if re.fullmatch(r"[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{5})?", color_match.group(1)) else color_match.group(1))
        bg_raw = color_match.group(2)
        bg = None
        if bg_raw:
            bg = _valid_color("#" + bg_raw if re.fullmatch(r"[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{5})?", bg_raw) else bg_raw)
        if fg:
            styles = [f"color:{fg}"]
            if bg:
                styles.append(f"background-color:{bg}")
            return f'<span style="{_safe_attr(";".join(styles))}">{render_inline(color_match.group(3), context, depth + 1)}</span>'
    size_match = re.match(r"^([+-]\d{1,2})\s+([\s\S]*)$", inner)
    if size_match:
        delta = max(-5, min(5, int(size_match.group(1))))
        factor = 1 + (delta * 0.15)
        return f'<span style="font-size:{factor:.2f}em">{render_inline(size_match.group(2), context, depth + 1)}</span>'
    if inner.startswith("#!"):
        # Do not execute embedded HTML, JS, arbitrary styles, or template code.
        return f'<code class="inline-code">{_escape(inner)}</code>'
    return f'<code class="inline-code">{_escape(inner)}</code>'


def _render_macro(token: str, context: RenderContext, depth: int) -> str:
    macro = token[1:-1]
    name = macro.lower()
    if name == "br":
        return "<br>"
    if name == "clearfix":
        return '<span class="wiki-clearfix"></span>'
    if name in {"목차", "tableofcontents"}:
        return TOC_MARKER
    if name in {"각주", "footnote"}:
        return FOOTNOTES_MARKER

    video = re.fullmatch(r"(youtube|vimeo|nicovideo|kakaotv|navertv)\(([^)]{1,200})\)", macro, re.IGNORECASE)
    if video:
        provider, media_id = video.group(1).lower(), video.group(2).strip()
        if provider == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", media_id):
            src = f"https://www.youtube-nocookie.com/embed/{quote(media_id, safe='')}"
            return f'<div class="wiki-video"><iframe src="{_safe_attr(src)}" title="YouTube video" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" sandbox="allow-scripts allow-same-origin allow-presentation" allowfullscreen></iframe></div>'
        if provider == "vimeo" and re.fullmatch(r"[0-9]{5,12}", media_id):
            src = f"https://player.vimeo.com/video/{media_id}"
            return f'<div class="wiki-video"><iframe src="{_safe_attr(src)}" title="Vimeo video" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" sandbox="allow-scripts allow-same-origin allow-presentation" allowfullscreen></iframe></div>'
        # Other providers remain ordinary links until their embed behaviour is
        # explicitly supported; never construct an iframe from arbitrary input.
        url = {
            "nicovideo": "https://www.nicovideo.jp/watch/" + media_id,
            "kakaotv": "https://tv.kakao.com/",
            "navertv": "https://tv.naver.com/",
        }.get(provider)
        if url and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", media_id):
            return f'<a href="{_safe_attr(url)}" rel="nofollow noopener noreferrer">{_escape(provider + " video: " + media_id)}</a>'
        return _escape(token)

    now = datetime.now()
    if name == "date":
        return now.strftime("%Y-%m-%d")
    if name == "datetime":
        return now.strftime("%Y-%m-%d %H:%M:%S")

    call = re.fullmatch(r"(age|dday|anchor|ruby)\((.*)\)", macro, re.IGNORECASE | re.DOTALL)
    if not call:
        return _escape(token)
    macro_name, args = call.group(1).lower(), call.group(2).strip()
    if macro_name == "anchor":
        slug = _slugify(args)
        return f'<span id="{_safe_attr(slug)}"></span>'
    if macro_name == "ruby":
        base, sep, reading = args.partition("|")
        if not sep:
            return _escape(token)
        return f'<ruby>{render_inline(base, context, depth + 1)}<rp>(</rp><rt>{render_inline(reading, context, depth + 1)}</rt><rp>)</rp></ruby>'
    try:
        target_date = date.fromisoformat(args[:10])
    except ValueError:
        return _escape(token)
    today = date.today()
    if macro_name == "age":
        years = today.year - target_date.year - ((today.month, today.day) < (target_date.month, target_date.day))
        return _escape(str(years)) if years >= 0 else _escape("0")
    delta = (target_date - today).days
    return "D-Day" if delta == 0 else (f"D-{delta}" if delta > 0 else f"D+{abs(delta)}")


def _iter_inline_tokens(text: str):
    """Yield inline tokens while respecting nested triple-brace constructs."""
    pos = 0
    length = len(text)
    while pos < length:
        match = _TOKEN_RE.search(text, pos)
        brace_start = text.find("{{{", pos)
        if brace_start != -1 and (match is None or brace_start <= match.start()):
            level = 0
            i = brace_start
            end = None
            while i < length - 2:
                if text.startswith("{{{", i):
                    level += 1
                    i += 3
                elif text.startswith("}}}", i):
                    level -= 1
                    i += 3
                    if level == 0:
                        end = i
                        break
                else:
                    i += 1
            if end is not None:
                yield brace_start, end, "brace", text[brace_start:end]
                pos = end
                continue
            # An unmatched opener is ordinary escaped text.
            if match is None:
                break
            yield match.start(), match.end(), match.lastgroup, match.group(0)
            pos = match.end()
            continue
        if match is None:
            break
        yield match.start(), match.end(), match.lastgroup, match.group(0)
        pos = match.end()


def render_inline(text: str, context: RenderContext | None = None, depth: int = 0, *, allow_footnotes: bool = True) -> str:
    """Render inline NamuMark. All unrecognized source text is HTML-escaped."""
    if context is None:
        context = RenderContext()
    if depth > 12:
        return _escape(text)

    out: list[str] = []
    last = 0
    for token_start, token_end, kind, token in _iter_inline_tokens(text):
        out.append(_escape(text[last:token_start]))
        if kind == "link":
            raw = token[2:-2].strip()
            if raw.startswith("분류:"):
                # Category metadata is displayed by the page template, not inline.
                pass
            elif raw.startswith("파일:"):
                out.append(_render_file(raw))
            else:
                target, sep, label = raw.partition("|")
                target = target.strip()
                label = label.strip() if sep else target
                if target.startswith(("http://", "https://", "ftp://")):
                    parsed = urlparse(target)
                    if parsed.scheme in {"http", "https", "ftp"} and parsed.netloc:
                        out.append(f'<a href="{_safe_attr(target)}" rel="nofollow noopener noreferrer">{render_inline(label, context, depth + 1, allow_footnotes=allow_footnotes)}</a>')
                    else:
                        out.append(_escape(label))
                elif target.startswith("#"):
                    anchor = quote(_slugify(target[1:]), safe="-_")
                    out.append(f'<a href="#{anchor}">{render_inline(label, context, depth + 1, allow_footnotes=allow_footnotes)}</a>')
                else:
                    page_title, has_hash, anchor = target.partition("#")
                    href = _internal_href(page_title or target)
                    if has_hash and anchor:
                        href += "#" + quote(_slugify(anchor), safe="-_")
                    out.append(f'<a href="{_safe_attr(href)}">{render_inline(label, context, depth + 1, allow_footnotes=allow_footnotes)}</a>')
        elif kind == "footnote":
            if allow_footnotes:
                out.append(_footnote_ref(token[2:-1], context))
            else:
                out.append(_escape(token))
        elif kind == "brace":
            out.append(_render_inline_braces(token, context, depth))
        elif kind == "bold":
            out.append("<strong>" + render_inline(token[3:-3], context, depth + 1, allow_footnotes=allow_footnotes) + "</strong>")
        elif kind == "italic":
            out.append("<em>" + render_inline(token[2:-2], context, depth + 1, allow_footnotes=allow_footnotes) + "</em>")
        elif kind == "underline":
            out.append("<u>" + render_inline(token[2:-2], context, depth + 1, allow_footnotes=allow_footnotes) + "</u>")
        elif kind in {"strike", "strike_dash"}:
            out.append("<del>" + render_inline(token[2:-2], context, depth + 1, allow_footnotes=allow_footnotes) + "</del>")
        elif kind == "sup":
            out.append("<sup>" + render_inline(token[2:-2], context, depth + 1, allow_footnotes=allow_footnotes) + "</sup>")
        elif kind == "sub":
            out.append("<sub>" + render_inline(token[2:-2], context, depth + 1, allow_footnotes=allow_footnotes) + "</sub>")
        elif kind == "code":
            out.append('<code class="inline-code">' + _escape(token[1:-1]) + "</code>")
        elif kind == "macro":
            out.append(_render_macro(token, context, depth))
        elif kind == "param_macro":
            out.append(_render_macro(token, context, depth))
        elif kind == "url":
            url = token.rstrip(".,!?;:)")
            suffix = token[len(url):]
            parsed = urlparse(url)
            if parsed.scheme in {"http", "https", "ftp"} and parsed.netloc:
                out.append(f'<a href="{_safe_attr(url)}" rel="nofollow noopener noreferrer">{_escape(url)}</a>{_escape(suffix)}')
            else:
                out.append(_escape(token))
        last = token_end
    out.append(_escape(text[last:]))
    return "".join(out)


def _parse_table_cell(raw_cell: str, table_styles: list[str], row_styles: list[str], context: RenderContext, *, first_cell: bool) -> tuple[str, str]:
    cell = raw_cell.strip()
    cell_styles: list[str] = []
    attrs: list[str] = []
    header = False
    while cell.startswith("<") and ">" in cell:
        option, rest = cell[1:].split(">", 1)
        option = option.strip()
        low = option.lower()
        if (low.startswith("tablewidth=") or low.startswith("table width=")) and first_cell:
            value = _safe_dimension(option.split("=", 1)[1], maximum=2400)
            if value:
                table_styles.append(f"width:{value}")
        elif (low.startswith("tablealign=") or low.startswith("table align=")) and first_cell:
            value = option.split("=", 1)[1].strip().lower()
            if value in {"left", "center", "right"}:
                if value == "center":
                    table_styles.extend(["margin-left:auto", "margin-right:auto"])
                elif value == "right":
                    table_styles.extend(["margin-left:auto", "margin-right:0"])
                else:
                    table_styles.extend(["margin-left:0", "margin-right:auto"])
        elif low.startswith("tablebgcolor=") and first_cell:
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                table_styles.append(f"background-color:{value}")
        elif low.startswith("tablecolor=") and first_cell:
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                table_styles.append(f"color:{value}")
        elif low.startswith("tablebordercolor=") and first_cell:
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                table_styles.append(f"border-color:{value}")
        elif low.startswith("tableborder=") and first_cell:
            value = option.split("=", 1)[1].strip()
            if value in {"0", "1", "2", "3", "4", "5"}:
                table_styles.append(f"border-width:{value}px")
        elif low.startswith("rowbgcolor=") and first_cell:
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                row_styles.append(f"background-color:{value}")
        elif low.startswith("rowcolor=") and first_cell:
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                row_styles.append(f"color:{value}")
        elif low.startswith("bgcolor="):
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                cell_styles.append(f"background-color:{value}")
        elif low.startswith("color="):
            value = _valid_color_option(option.split("=", 1)[1])
            if value:
                cell_styles.append(f"color:{value}")
        elif low.startswith("width="):
            value = _safe_dimension(option.split("=", 1)[1], maximum=2400)
            if value:
                cell_styles.append(f"width:{value}")
        elif low.startswith("height="):
            value = _safe_dimension(option.split("=", 1)[1], maximum=800)
            if value:
                cell_styles.append(f"height:{value}")
        elif low.startswith("align="):
            value = option.split("=", 1)[1].strip().lower()
            if value in {"left", "center", "right", "justify"}:
                cell_styles.append(f"text-align:{value}")
        elif low.startswith("valign="):
            value = option.split("=", 1)[1].strip().lower()
            if value in {"top", "middle", "bottom"}:
                cell_styles.append(f"vertical-align:{value}")
        elif option in {"(", ":", ")"}:
            cell_styles.append({"(": "text-align:left", ":": "text-align:center", ")": "text-align:right"}[option])
        elif option == "nopad":
            cell_styles.append("padding:0")
        elif re.fullmatch(r"[\^v]\|\d+", option, re.IGNORECASE):
            direction, count = option.split("|", 1)
            rowspan = max(1, min(30, int(count)))
            if rowspan > 1:
                attrs.append(f'rowspan="{rowspan}"')
            cell_styles.append("vertical-align:top" if direction == "^" else "vertical-align:bottom")
        elif re.fullmatch(r"-\d+", option):
            colspan = max(1, min(12, abs(int(option))))
            if colspan > 1:
                attrs.append(f'colspan="{colspan}"')
        elif re.fullmatch(r"\|\d+", option):
            rowspan = max(1, min(30, int(option[1:])))
            if rowspan > 1:
                attrs.append(f'rowspan="{rowspan}"')
        elif option == "=":
            header = True
        else:
            # Unknown table options are ignored and never interpreted as HTML.
            pass
        cell = rest.strip()
    tag = "th" if header else "td"
    if cell_styles:
        attrs.append(f'style="{_safe_attr(";".join(cell_styles))}"')
    value = render_inline(cell, context)
    if not value.strip():
        value = "&nbsp;"
    return f"<{tag}{(' ' + ' '.join(attrs)) if attrs else ''}>{value}</{tag}>", cell



def _render_table(lines: list[str], context: RenderContext) -> str:
    table_styles: list[str] = ["border-collapse:collapse"]
    rendered_rows: list[str] = []
    for line in lines:
        raw = line.strip()
        if raw.startswith("||"):
            raw = raw[2:]
        if raw.endswith("||"):
            raw = raw[:-2]
        raw_cells = raw.split("||")
        # NamuMark's |||| denotes a blank row, often used with row-spanning cells.
        if not raw.strip() and line.strip() == "||||":
            rendered_rows.append("<tr></tr>")
            continue
        if len(raw_cells) == 1 and not raw_cells[0].strip():
            continue
        row_styles: list[str] = []
        rendered_cells: list[str] = []
        for cell_index, raw_cell in enumerate(raw_cells):
            rendered, _ = _parse_table_cell(raw_cell, table_styles, row_styles, context, first_cell=cell_index == 0)
            rendered_cells.append(rendered)
        style = f' style="{_safe_attr(";".join(dict.fromkeys(row_styles)))}"' if row_styles else ""
        rendered_rows.append("<tr" + style + ">" + "".join(rendered_cells) + "</tr>")
    table_style = f' style="{_safe_attr(";".join(dict.fromkeys(table_styles)))}"' if table_styles else ""
    return '<div class="table-wrap"><table class="wiki-table"' + table_style + "><tbody>" + "".join(rendered_rows) + "</tbody></table></div>" if rendered_rows else ""


def _render_list_tree(items: list[dict[str, object]], context: RenderContext) -> str:
    if not items:
        return ""
    chunks: list[str] = []
    index = 0
    while index < len(items):
        kind = str(items[index]["kind"])
        start = index
        while index < len(items) and str(items[index]["kind"]) == kind:
            index += 1
        if kind == "ul":
            tag_open, tag_close = "<ul>", "</ul>"
        else:
            typ = kind.split(":", 1)[1] if ":" in kind else "1"
            type_attr = f' type="{_safe_attr(typ)}"' if typ in {"a", "A", "i", "I"} else ""
            start_value = items[start].get("start")
            start_attr = f' start="{int(start_value)}"' if isinstance(start_value, int) and 0 <= start_value <= 2147483647 else ""
            tag_open, tag_close = f"<ol{type_attr}{start_attr}>", "</ol>"
        body = []
        for item in items[start:index]:
            child_html = _render_list_tree(item["children"], context)  # type: ignore[arg-type]
            body.append("<li>" + render_inline(str(item["text"]), context) + child_html + "</li>")
        chunks.append(tag_open + "".join(body) + tag_close)
    return "".join(chunks)


def _render_toc(context: RenderContext) -> str:
    if not context.headings:
        return '<nav class="wiki-toc"><div class="wiki-toc-title">목차</div><p class="wiki-toc-empty">문단이 없습니다.</p></nav>'
    entries = []
    for heading in context.headings:
        level = int(heading["level"])
        slug = str(heading["slug"])
        title = _escape(str(heading["title"]))
        entries.append(f'<li class="toc-level-{level}"><a href="#{_safe_attr(quote(slug, safe="-_"))}">{title}</a></li>')
    return '<nav class="wiki-toc"><div class="wiki-toc-title">목차</div><ol>' + "".join(entries) + "</ol></nav>"


def _render_footnotes(context: RenderContext) -> str:
    if not context.footnotes:
        return ""
    entries = []
    for index, note in enumerate(context.footnotes, start=1):
        body = render_inline(str(note.get("body", "")), context, allow_footnotes=False)
        refs = note.get("refs", [])
        backrefs = " ".join(
            f'<a class="footnote-backref" href="#{_safe_attr(str(ref))}" aria-label="각주 참조 위치 {n}">↩</a>'
            for n, ref in enumerate(refs, start=1)  # type: ignore[union-attr]
        )
        entries.append(f'<li id="fn-{index}"><span class="footnote-body">{body}</span> {backrefs}</li>')
    return '<section class="wiki-footnotes" aria-label="각주"><ol>' + "".join(entries) + "</ol></section>"


def _strip_comment(line: str) -> str:
    # NamuMark comments start with ##; URLs and heading anchors use single #.
    position = line.find("##")
    return line[:position].rstrip() if position >= 0 else line


def render_namumark(source: str) -> str:
    """Render the supported NamuMark subset to safe HTML."""
    lines = (source or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []
    context = RenderContext()
    i = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append("<p>" + "<br>\n".join(render_inline(x, context) for x in paragraph) + "</p>")
            paragraph.clear()

    while i < len(lines):
        original = lines[i]
        stripped = original.strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue

        if stripped.startswith("##"):
            i += 1
            continue

        # Folding block: {{{#!folding title ... }}}
        folding_match = re.match(r"^\{\{\{#!folding(?:\s+(.*))?$", stripped, re.IGNORECASE)
        if folding_match:
            flush_paragraph()
            title = (folding_match.group(1) or "More").strip()
            body_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "}}}":
                body_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            body_html = render_namumark("\n".join(body_lines))
            blocks.append(f'<details class="wiki-folding"><summary>{render_inline(title, context)}</summary><div class="wiki-folding-body">{body_html}</div></details>')
            continue

        # Wiki/style wrappers parse their body as NamuMark. User-supplied CSS is
        # intentionally ignored rather than copied into style attributes.
        wrapper_match = re.match(r"^\{\{\{#!(wiki|style)(?:\s+.*)?$", stripped, re.IGNORECASE)
        if wrapper_match and "}}}" not in stripped[3:]:
            flush_paragraph()
            body_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "}}}":
                body_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            blocks.append('<div class="wiki-block">' + render_namumark("\n".join(body_lines)) + "</div>")
            continue

        # Fenced code: {{{#!python ... }}} or a plain {{{ ... }}} block.
        if stripped.startswith("{{{") and (stripped == "{{{" or "}}}" not in stripped[3:]):
            flush_paragraph()
            first = stripped[3:].strip()
            language = ""
            if first.startswith("#!"):
                language_raw = first[2:].split()[0] if first[2:].split() else ""
                language = re.sub(r"[^A-Za-z0-9_+-]", "", language_raw)
                code_lines: list[str] = []
            else:
                code_lines = [first] if first else []
            i += 1
            while i < len(lines) and lines[i].strip() != "}}}":
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            lang_attr = f' class="language-{language}"' if language else ""
            code_text = _escape("\n".join(code_lines))
            blocks.append(f'<pre class="code-block"><code{lang_attr}>{code_text}</code></pre>')
            continue

        if stripped in {"[목차]", "[tableofcontents]"}:
            flush_paragraph()
            blocks.append(TOC_MARKER)
            i += 1
            continue
        if stripped.lower() in {"[각주]", "[footnote]"}:
            flush_paragraph()
            blocks.append(FOOTNOTES_MARKER)
            i += 1
            continue

        video_line = re.fullmatch(r"\[(?:youtube|vimeo|nicovideo|kakaotv|navertv)\([^\]]+\)\]", stripped, re.IGNORECASE)
        if video_line:
            flush_paragraph()
            blocks.append(_render_macro(stripped, context, 0))
            i += 1
            continue

        heading = re.match(r"^(={2,6})\s*(.*?)\s*\1\s*$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            base_slug = _slugify(title.lstrip("#").strip())
            duplicate = context.slug_counts.get(base_slug, 0) + 1
            context.slug_counts[base_slug] = duplicate
            slug = base_slug if duplicate == 1 else f"{base_slug}-{duplicate}"
            context.headings.append({"level": level, "title": title.lstrip("#").strip(), "slug": slug})
            heading_class = ' class="wiki-heading-collapsed"' if title.startswith("#") else ""
            blocks.append(f'<h{level}{heading_class} id="{_safe_attr(slug)}">{render_inline(title.lstrip("#").strip(), context)}</h{level}>')
            i += 1
            continue

        if re.fullmatch(r"-{4,}", stripped):
            flush_paragraph()
            blocks.append("<hr>")
            i += 1
            continue

        if stripped.startswith("||"):
            flush_paragraph()
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("||"):
                table_lines.append(lines[i])
                i += 1
            blocks.append(_render_table(table_lines, context))
            continue

        quote_match = re.match(r"^(>+)\s?(.*)$", stripped)
        if quote_match:
            flush_paragraph()
            quote_parts: list[tuple[int, str]] = []
            while i < len(lines):
                qm = re.match(r"^\s*(>+)\s?(.*)$", lines[i].strip())
                if not qm:
                    break
                quote_parts.append((len(qm.group(1)), qm.group(2)))
                i += 1
            rendered_quote = []
            current_depth = 0
            for depth, text in quote_parts:
                target_depth = min(depth, 6)
                if target_depth > current_depth:
                    rendered_quote.extend("<blockquote>" for _ in range(target_depth - current_depth))
                elif target_depth < current_depth:
                    rendered_quote.extend("</blockquote>" for _ in range(current_depth - target_depth))
                if rendered_quote and rendered_quote[-1] not in {"<blockquote>", "</blockquote>"}:
                    rendered_quote.append("<br>")
                rendered_quote.append(render_inline(text, context))
                current_depth = target_depth
            rendered_quote.extend("</blockquote>" for _ in range(current_depth))
            blocks.append("".join(rendered_quote))
            continue

        # Nested ordered/unordered lists. Repeated '*' and leading spaces both
        # create nesting; a list is kept as a valid nested HTML structure.
        list_match = re.match(r"^([ \t]*)(\*+|[0-9]+\.|[a-zA-Z]\.(?:#\d+)?)\s+(.*)$", original)
        if list_match:
            flush_paragraph()
            items: list[dict[str, object]] = []
            base_depth = None
            last_at_depth: dict[int, dict[str, object]] = {}
            while i < len(lines):
                lm = re.match(r"^([ \t]*)(\*+|[0-9]+\.|[a-zA-Z]\.(?:#\d+)?)\s+(.*)$", lines[i])
                if not lm:
                    break
                indentation = len(lm.group(1).replace("\t", "    "))
                marker = lm.group(2)
                marker_depth = len(marker) - 1 if marker.startswith("*") else 0
                raw_depth = indentation + marker_depth
                if base_depth is None:
                    base_depth = raw_depth
                depth = max(0, min(6, raw_depth - base_depth))
                list_marker = marker.split("#", 1)[0]
                start_value = int(marker.split("#", 1)[1]) if "#" in marker and marker.split("#", 1)[1].isdigit() else None
                if marker.startswith("*"):
                    kind = "ul"
                elif list_marker.endswith(".") and list_marker[:-1] in {"a", "A", "i", "I"}:
                    kind = "ol:" + list_marker[:-1]
                else:
                    kind = "ol:1"
                while depth > 0 and (depth - 1) not in last_at_depth:
                    depth -= 1
                parent = last_at_depth[depth - 1]["children"] if depth > 0 else items
                item = {"kind": kind, "text": lm.group(3), "children": [], "start": start_value}
                parent.append(item)  # type: ignore[union-attr]
                last_at_depth[depth] = item
                for old_depth in [k for k in last_at_depth if k > depth]:
                    del last_at_depth[old_depth]
                i += 1
            blocks.append(_render_list_tree(items, context))
            continue

        # Strip inline comments in ordinary content while keeping code blocks
        # untouched (they were consumed above).
        cleaned = _strip_comment(original)
        if not cleaned.strip():
            flush_paragraph()
            i += 1
            continue
        paragraph.append(cleaned)
        i += 1

    flush_paragraph()
    result = "\n".join(block for block in blocks if block)
    toc_html = _render_toc(context)
    footnotes_html = _render_footnotes(context)
    if TOC_MARKER in result:
        result = result.replace(TOC_MARKER, toc_html)
    if FOOTNOTES_MARKER in result:
        result = result.replace(FOOTNOTES_MARKER, footnotes_html)
    elif footnotes_html:
        result += footnotes_html
    return result


def extract_categories(source: str) -> list[str]:
    categories: list[str] = []
    for match in re.finditer(r"\[\[분류:([^\]|]+)(?:\|[^\]]*)?\]\]", source or ""):
        name = match.group(1).strip()
        if name and name not in categories:
            categories.append(name)
    return categories


def extract_redirect(source: str) -> str | None:
    """Return a redirect target when the document starts with #redirect."""
    text = source or ""
    match = re.match(r"^\s*#redirect\s+(?:\[\[([^\]|]+)(?:\|[^\]]*)?\]\]|([^\r\n]+))", text, re.IGNORECASE)
    if not match:
        return None
    target = (match.group(1) or match.group(2) or "").strip()
    if "#" in target:
        target = target.split("#", 1)[0].strip()
    return target or None
