"""Dify Code Node: docx / xlsx / pptx (新形式) を直接 Markdown 化する。

担当範囲:
  ★ docx / xlsx / pptx のみ ★

旧形式 (.doc / .xls / .ppt) は OLE2 バイナリで標準ライブラリでは扱えないため、
このノードでは非対応。旧形式は Dify Document Extractor で text 化したあと
office_legacy_to_md.py の Code ノードで整形する運用。

Office Open XML (docx/xlsx/pptx) は ZIP + XML なので、外部ライブラリ不要で
Dify sandbox 内で動かせる。

入力 (Code ノードの Input variables, いずれも String):
  file_url:  Start ノードで {{start.file.url}} を文字列として渡す
  file_ext:  {{start.file.extension}}  (docx / xlsx / pptx)

出力:
  markdown: str
  success:  bool
  error:    str
"""

import io
import posixpath
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"


def _natural_key(name: str) -> int:
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _chart_pts(parent) -> list:
    pts = []
    for pt in parent.iter(f"{C}pt"):
        v = pt.find(f"{C}v")
        if v is None or v.text is None:
            continue
        try:
            idx = int(pt.get("idx", "0"))
        except ValueError:
            idx = 0
        pts.append((idx, v.text.strip()))
    pts.sort()
    return [v for _, v in pts]


def _extract_chart_md(chart_xml: bytes) -> str:
    """chart XML から Markdown table を生成 (cache 値のみ)。"""
    try:
        root = ET.fromstring(chart_xml)
    except ET.ParseError:
        return ""

    title = ""
    for t in root.findall(f".//{C}title//{A}t"):
        if t.text and t.text.strip():
            title = t.text.strip()
            break

    series = []
    for ser in root.iter(f"{C}ser"):
        name_el = ser.find(f".//{C}tx//{C}v")
        name = (name_el.text or "").strip() if name_el is not None else ""
        cat_parent = ser.find(f"{C}cat")
        val_parent = ser.find(f"{C}val")
        cats = _chart_pts(cat_parent) if cat_parent is not None else []
        vals = _chart_pts(val_parent) if val_parent is not None else []
        if not vals:
            continue
        series.append((name, cats, vals))
    if not series:
        return ""

    out = []
    if title:
        out.append(f"**{title}**")

    if len(series) == 1:
        name, cats, vals = series[0]
        col = name or "値"
        out.append(f"| 項目 | {col} |")
        out.append("| --- | --- |")
        for i, v in enumerate(vals):
            cat = cats[i] if i < len(cats) else f"#{i + 1}"
            out.append(f"| {cat} | {v} |")
    else:
        all_cats = []
        seen = set()
        for _, cats, vals in series:
            for i in range(len(vals)):
                cat = cats[i] if i < len(cats) else f"#{i + 1}"
                if cat not in seen:
                    seen.add(cat)
                    all_cats.append(cat)
        header = ["項目"] + [n or f"系列{i + 1}"
                            for i, (n, _, _) in enumerate(series)]
        out.append("| " + " | ".join(header) + " |")
        out.append("| " + " | ".join(["---"] * len(header)) + " |")
        maps = []
        for _, cats, vals in series:
            m = {}
            for i, v in enumerate(vals):
                cat = cats[i] if i < len(cats) else f"#{i + 1}"
                m[cat] = v
            maps.append(m)
        for cat in all_cats:
            row = [cat] + [m.get(cat, "") for m in maps]
            out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _zip_charts_md(z, chart_dir: str) -> list:
    paths = sorted(
        (n for n in z.namelist()
         if n.startswith(chart_dir)
         and n.split("/")[-1].startswith("chart")
         and n.endswith(".xml")),
        key=_natural_key,
    )
    out = []
    for p in paths:
        with z.open(p) as f:
            md = _extract_chart_md(f.read())
        if md:
            out.append(md)
    return out


def _zip_media_count(z, media_dir: str) -> int:
    return sum(1 for n in z.namelist()
               if n.startswith(media_dir) and not n.endswith("/"))


def _figures_section(z, chart_dir: str, media_dir: str) -> str:
    charts = _zip_charts_md(z, chart_dir)
    images = _zip_media_count(z, media_dir)
    if not charts and not images:
        return ""
    parts = ["", "## 図表"]
    if images:
        parts.append(f"画像 {images} 個含まれます（本文には抽出されません）。")
    for i, md in enumerate(charts, 1):
        parts.append("")
        parts.append(f"### Chart {i}")
        parts.append("")
        parts.append(md)
    return "\n".join(parts)


def _md_table(rows: list) -> str:
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    rows = [[(c or "").replace("|", "\\|").replace("\n", " ") for c in r]
            + [""] * (cols - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * cols) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


# ---------- docx ----------

def _docx_paragraph(p) -> str:
    text = "".join(t.text or "" for t in p.iter(f"{W}t"))
    if not text.strip():
        return ""
    style_el = p.find(f"{W}pPr/{W}pStyle")
    style = style_el.get(f"{W}val") if style_el is not None else ""
    if style.startswith("Heading"):
        m = re.search(r"\d+", style)
        level = min(int(m.group()) if m else 1, 6)
        return f"{'#' * level} {text}"
    if p.find(f"{W}pPr/{W}numPr") is not None:
        return f"- {text}"
    return text


def _docx_table(tbl) -> str:
    rows = []
    for tr in tbl.findall(f"{W}tr"):
        cells = []
        for tc in tr.findall(f"{W}tc"):
            txt = " ".join(t.text or "" for t in tc.iter(f"{W}t")).strip()
            cells.append(txt)
        rows.append(cells)
    return _md_table(rows)


def _docx_to_md(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open("word/document.xml") as f:
            root = ET.parse(f).getroot()
        body = root.find(f"{W}body")
        out = []
        for el in body:
            if el.tag == f"{W}p":
                s = _docx_paragraph(el)
            elif el.tag == f"{W}tbl":
                s = _docx_table(el)
            else:
                s = ""
            if s.strip():
                out.append(s)
        body_md = "\n\n".join(out)
        figures = _figures_section(z, "word/charts/", "word/media/")
    return body_md + ("\n\n" + figures if figures else "")


# ---------- xlsx ----------

def _xlsx_to_md(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()

        shared = []
        if "xl/sharedStrings.xml" in names:
            with z.open("xl/sharedStrings.xml") as f:
                ss = ET.parse(f).getroot()
            for si in ss.findall(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))

        sheet_names = []
        with z.open("xl/workbook.xml") as f:
            wb = ET.parse(f).getroot()
        for s in wb.find(f"{S}sheets"):
            sheet_names.append(s.get("name"))

        sheet_files = sorted(
            (n for n in names
             if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
            key=_natural_key,
        )

        out = []
        for idx, path in enumerate(sheet_files):
            with z.open(path) as f:
                root = ET.parse(f).getroot()
            rows = []
            for row in root.iter(f"{S}row"):
                cells = []
                for c in row.findall(f"{S}c"):
                    t_attr = c.get("t", "")
                    v = c.find(f"{S}v")
                    val = v.text if v is not None else ""
                    if t_attr == "s" and val and val.isdigit():
                        i = int(val)
                        val = shared[i] if 0 <= i < len(shared) else ""
                    elif t_attr == "inlineStr":
                        is_el = c.find(f"{S}is")
                        val = "".join(t.text or "" for t in is_el.iter(f"{S}t")) \
                            if is_el is not None else ""
                    cells.append(val or "")
                rows.append(cells)
            if not rows:
                continue
            name = sheet_names[idx] if idx < len(sheet_names) else f"Sheet{idx + 1}"
            out.append(f"## {name}")
            out.append(_md_table(rows))
            out.append("")
        figures = _figures_section(z, "xl/charts/", "xl/media/")
        if figures:
            out.append(figures)
    return "\n".join(out)


# ---------- pptx ----------

R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
SVG_NS = "{http://www.w3.org/2000/svg}"

_PAGE_NUM_RE = re.compile(r"^\d{1,3}$")
_LEAD_BULLET_RE = re.compile(r"^[・•●◆▪▫●○]\s*")


def _clean(text: str) -> str:
    return text.replace("", " ").replace(" ", " ").strip()


def _pptx_paragraph_text(para) -> str:
    return "".join(t.text or "" for t in para.iter(f"{A}t"))


def _shape_xy(shape):
    off = (shape.find(f"{P}spPr/{A}xfrm/{A}off")
           or shape.find(f"{P}xfrm/{A}off"))
    if off is None:
        return (10**18, 10**18)
    try:
        return (int(off.get("y", "0")), int(off.get("x", "0")))
    except ValueError:
        return (10**18, 10**18)


def _pptx_table(tbl) -> str:
    rows = []
    for tr in tbl.findall(f"{A}tr"):
        cells = []
        for tc in tr.findall(f"{A}tc"):
            lines = []
            for p in tc.iter(f"{A}p"):
                t = _clean(_pptx_paragraph_text(p))
                if t:
                    lines.append(t)
            cells.append(" ".join(lines))
        rows.append(cells)
    return _md_table(rows)


def _resolve_rels(z, slide_path: str) -> dict:
    rels_path = (slide_path.replace("ppt/slides/", "ppt/slides/_rels/")
                 + ".rels")
    if rels_path not in z.namelist():
        return {}
    with z.open(rels_path) as f:
        root = ET.parse(f).getroot()
    slide_dir = posixpath.dirname(slide_path)
    rels = {}
    for r in root.findall(f"{PKG_REL}Relationship"):
        target = r.get("Target") or ""
        rels[r.get("Id")] = posixpath.normpath(
            posixpath.join(slide_dir, target))
    return rels


def _extract_svg_texts(z, slide_path: str) -> list:
    rels = _resolve_rels(z, slide_path)
    texts = []
    for target in rels.values():
        if not target.lower().endswith(".svg"):
            continue
        if target not in z.namelist():
            continue
        try:
            with z.open(target) as f:
                root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for t in root.iter(f"{SVG_NS}text"):
            s = "".join(t.itertext()).strip()
            if s:
                texts.append(s)
    return texts


def _format_paragraph(lvl: int, text: str) -> str:
    if _LEAD_BULLET_RE.match(text):
        text = _LEAD_BULLET_RE.sub("", text)
        return "  " * max(lvl, 0) + "- " + text
    if lvl <= 0:
        return text
    return "  " * (lvl - 1) + "- " + text


def _shape_max_font_size(sp) -> int:
    tx = sp.find(f"{P}txBody")
    if tx is None:
        return 0
    sizes = []
    for el in tx.iter():
        sz = el.get("sz") if el.tag in (f"{A}rPr",
                                         f"{A}endParaRPr",
                                         f"{A}defRPr") else None
        if sz and sz.isdigit():
            sizes.append(int(sz))
    return max(sizes) if sizes else 0


def _collect_text_shape(sp):
    """sp shape を (is_title, [(lvl, text), ...]) で返す。空なら None。"""
    ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
    is_title = ph is not None and ph.get("type") in ("title", "ctrTitle")
    tx = sp.find(f"{P}txBody")
    if tx is None:
        return None
    paras = []
    for para in tx.findall(f"{A}p"):
        text = _clean(_pptx_paragraph_text(para))
        if not text or _PAGE_NUM_RE.match(text):
            continue
        pPr = para.find(f"{A}pPr")
        lvl = int(pPr.get("lvl", "0")) if pPr is not None else 0
        paras.append((lvl, text))
    if not paras:
        return None
    return (is_title, paras)


def _pptx_slide_to_md(z, slide_path: str, root, num: int) -> str:
    spTree = root.find(f"{P}cSld/{P}spTree")
    if spTree is None:
        return f"# Slide {num}"

    title_text = None
    items = []  # (y, x, kind, payload)
    title_cands = []  # (font_size, y, x, head, paras_ref)

    for sp in spTree.iter(f"{P}sp"):
        collected = _collect_text_shape(sp)
        if collected is None:
            continue
        is_title, paras = collected
        y, x = _shape_xy(sp)
        if is_title and title_text is None:
            title_text = " ".join(t for _, t in paras)
        else:
            head = " ".join(t for _, t in paras)
            if 0 < len(head) <= 60:
                title_cands.append(
                    (_shape_max_font_size(sp), y, x, head, paras))
            items.append((y, x, "text", paras))

    for gf in spTree.iter(f"{P}graphicFrame"):
        y, x = _shape_xy(gf)
        for tbl in gf.iter(f"{A}tbl"):
            md = _pptx_table(tbl)
            if md:
                items.append((y, x, "table", md))

    svg_texts = _extract_svg_texts(z, slide_path)
    svg_joined = "".join(svg_texts).strip()
    if svg_joined:
        if title_text:
            title_text = svg_joined + title_text
        else:
            title_text = svg_joined

    if title_text is None and title_cands:
        title_cands.sort(key=lambda c: (-c[0], c[1], c[2]))
        chosen = title_cands[0]
        title_text = chosen[3]
        chosen_paras = chosen[4]
        items = [it for it in items
                 if not (it[2] == "text" and it[3] is chosen_paras)]

    items.sort(key=lambda i: (i[0], i[1]))

    out = [f"# Slide {num}: {title_text}" if title_text
           else f"# Slide {num}"]
    for _, _, kind, payload in items:
        if kind == "text":
            for lvl, text in payload:
                out.append(_format_paragraph(lvl, text))
        else:
            out.append(payload)
    return "\n\n".join(out)


def _pptx_to_md(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted(
            (n for n in z.namelist()
             if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=_natural_key,
        )
        out = []
        for i, path in enumerate(slides, start=1):
            with z.open(path) as f:
                root = ET.parse(f).getroot()
            out.append(_pptx_slide_to_md(z, path, root, i))
        figures = _figures_section(z, "ppt/charts/", "ppt/media/")
    return "\n\n".join(out) + ("\n\n" + figures if figures else "")


# ---------- entry ----------

def main(file_url: str, file_ext: str) -> dict:
    if not file_url:
        return {"markdown": "", "success": False, "error": "file_url が空"}

    ext = (file_ext or "").lower().lstrip(".")
    if ext in {"doc", "xls", "ppt"}:
        return {"markdown": "", "success": False,
                "error": (f".{ext} (旧形式) は非対応。Document Extractor で "
                          "text 化したあと office_legacy_to_md を使ってください")}
    if ext not in {"docx", "xlsx", "pptx"}:
        return {"markdown": "", "success": False,
                "error": f"未対応拡張子: {ext} (docx/xlsx/pptx のみ)"}

    try:
        with urllib.request.urlopen(file_url, timeout=30, context=_SSL_CTX) as r:
            data = r.read()
    except Exception as e:
        return {"markdown": "", "success": False, "error": f"download failed: {e}"}

    try:
        if ext == "docx":
            md = _docx_to_md(data)
        elif ext == "xlsx":
            md = _xlsx_to_md(data)
        else:
            md = _pptx_to_md(data)
    except Exception as e:
        return {"markdown": "", "success": False,
                "error": f"convert failed ({ext}): {e}"}

    return {"markdown": md, "success": True, "error": ""}
