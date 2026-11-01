"""PDF Writer's tools: make a PDF from a document description, list the PDFs made, describe the format."""
from __future__ import annotations

import os
import re
import time

from . import pdf
from .toolkit import ToolError, VALUES, data_dir, prop, setting, tool

MODULE = "pdf"

setting("pageSize", "select", "Default page size", "A4", "Used when a document names no page size.",
        options=["A4", "Letter", "A5", "Legal"])
setting("accent", "text", "Accent colour", "#2E86AB",
        "Headings, table headers, bullets and callouts use it when a document names none (#RRGGBB).")
setting("footer", "text", "Default footer", "{page} / {pages}",
        "Printed at the bottom of every page; {page}, {pages} and {title} are replaced. Empty: no footer.")

FORMAT = {
    "document": {
        "title": "big title on the first page (also the PDF's title)", "subtitle": "line under the title",
        "author": "text", "date": "text",
        "page": {"size": "A4 | Letter | A5 | Legal", "orientation": "portrait | landscape",
                 "margin_mm": "number, or [top, right, bottom, left] (default 18)"},
        "theme": {"accent": "#RRGGBB or a colour name", "text": "#RRGGBB", "muted": "#RRGGBB",
                  "size": "body text size in points (default 10.5)"},
        "header": "text on top of every page but the title page; {page} {pages} {title}",
        "footer": "text at the bottom of every page (default '{page} / {pages}'); null or '' for none",
        "blocks": "the content, in order: a list of blocks (a plain text is a paragraph)",
    },
    "inline": "in any text: **bold**, *italic*, `mono`, {red}coloured text{/} or {#C73E1D}...{/} (nestable, "
              "mixes with bold/italic), [link text](https://...) or [mail](mailto:...) as a clickable underlined link, a newline breaks the line, \\ escapes * ` and {",
    "colours": "#RRGGBB or black, white, gray, red, green, blue, orange, purple, yellow, teal, violet",
    "blocks": {
        "heading": {"text": "text", "level": "1-4 (1 has a rule under it)", "color": "colour", "align": "left|center|right"},
        "paragraph": {"text": "text", "size": "points", "color": "colour", "align": "left|center|right|justify",
                      "style": "bold | italic (whole paragraph)"},
        "list": {"items": "texts, or {text, items: [...], ordered} for nested lists", "ordered": "true for 1. 2. 3.",
                 "size": "points"},
        "table": {"columns": "header texts", "rows": "[[cell, ...], ...] (texts or numbers, inline styles allowed)",
                  "footer": "a bold totals row", "widths": "relative widths, one per column",
                  "align": "left|center|right per column (default: numbers right)", "zebra": "striped rows (default true)",
                  "grid": "lines between columns", "headerColor": "colour", "size": "points", "caption": "text"},
        "chart": {"kind": "bar | hbar | line | area | pie | donut", "title": "text", "labels": "category names",
                  "series": "[{name, values: [numbers], color}] (or `values` for one series)",
                  "stacked": "bar/hbar stacked", "valueLabels": "print each value", "unit": "suffix for values, e.g. ' dB'",
                  "min": "axis start", "max": "axis end", "yLabel": "axis caption", "legend": "force a legend for one series",
                  "height_mm": "default 70", "width": "fraction of the text width, 0.2-1 (default 1)", "caption": "text"},
        "image": {"path": "absolute path of a JPEG or PNG", "data": "or the image as base64",
                  "width": "millimetres, or '50%' of the text width (default: its own size, at most the text width)",
                  "align": "left|center|right", "caption": "text", "link": "https://... (the image is clickable)"},
        "callout": {"text": "text", "title": "bold first line", "color": "colour (tinted box, bar on the left)"},
        "quote": {"text": "text in italics with a bar on the left", "color": "colour"},
        "rule": {"color": "colour"}, "spacer": {"height_mm": "number"}, "pagebreak": {},
    },
    "notes": "Text uses the standard PDF fonts (Helvetica, Courier): Western European characters, accents included. "
             "PNG: not interlaced; with transparency, 8 bits per channel.",
}


def _folder() -> str:
    d = os.path.join(data_dir(), "pdf")
    os.makedirs(d, exist_ok=True)
    return d


def _safe(name: str) -> str:
    s = re.sub(r"[/\\:*?\"<>|\x00-\x1f]+", "_", name).strip(" .")
    if s.lower().endswith(".pdf"):
        s = s[:-4].rstrip(" .")
    return s[:120]


def _free_path(name: str) -> str:
    base = os.path.join(_folder(), name)
    path = base + ".pdf"
    n = 2
    while os.path.exists(path):
        path = "%s (%d).pdf" % (base, n)
        n += 1
    return path


@tool("make_pdf", "Writes a PDF from a document description (title, page, theme, header, footer and blocks: "
      "headings, paragraphs, lists, tables, charts, images, callouts, quotes, rules, page breaks) into the add-on's "
      "data folder, never over a file. Answers its path, page count and size. pdf_format describes every field.",
      props={"name": prop("string", "The file's name, without .pdf.", minLength=1, maxLength=120),
             "document": prop("object", "The document: {title, subtitle, author, date, page, theme, header, footer, "
                                        "blocks: [...]}.")},
      required=("name", "document"), writes=True, timeout_ms=180000, module=MODULE)
def make_pdf(a: dict) -> dict:
    name = _safe(str(a["name"]))
    if not name:
        raise ToolError("`name` has no usable characters: give the file a name", field="name")
    spec = dict(a["document"])
    page = dict(spec.get("page") or {})
    page.setdefault("size", VALUES.get("pageSize") or "A4")
    spec["page"] = page
    theme = dict(spec.get("theme") or {})
    theme.setdefault("accent", VALUES.get("accent") or "#2E86AB")
    spec["theme"] = theme
    if "footer" not in spec:
        spec["footer"] = VALUES.get("footer", "{page} / {pages}")
    try:
        data, pages, images = pdf.render(spec)
    except pdf.PdfError as e:
        raise ToolError("the document was not written: %s (pdf_format describes the fields)" % e, "invalid_document",
                        "document")
    path = _free_path(name)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return {"path": path, "pages": pages, "images": images, "bytes": len(data)}


@tool("list_pdfs", "The PDFs this add-on wrote, newest first: path, size and when.", module=MODULE)
def list_pdfs(a: dict) -> dict:
    d = _folder()
    rows = []
    for f in os.listdir(d):
        if f.lower().endswith(".pdf"):
            p = os.path.join(d, f)
            st = os.stat(p)
            rows.append({"path": p, "bytes": st.st_size, "written": time.strftime("%Y-%m-%d %H:%M",
                                                                                time.localtime(st.st_mtime)),
                         "_t": st.st_mtime})
    rows.sort(key=lambda r: r["_t"], reverse=True)
    for r in rows:
        del r["_t"]
    return {"folder": d, "pdfs": rows[:100], "count": len(rows)}


@tool("pdf_format", "The document format make_pdf takes: page, theme, header and footer, inline styles and every "
      "block type with its fields.", module=MODULE)
def pdf_format(a: dict) -> dict:
    return FORMAT
