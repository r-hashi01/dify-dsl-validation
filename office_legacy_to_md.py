"""Dify Code Node: doc / xls / ppt (旧形式) を Markdown 化する後処理。

担当範囲:
  ★ doc / xls / ppt (OLE2 旧形式) ★ + Document Extractor の text 出力全般

旧形式は標準ライブラリで直接読めないため、まず Dify Document Extractor で
text 化し、その出力を本ノードで Markdown 整形する運用。

新形式 (.docx / .xlsx / .pptx) は office_modern_to_md.py で直接処理する。

整形ルール:
  - Version フッタを削除
  - 先頭の短い行 → # タイトル
  - （XXX）単独行 → ## XXX
  - 第N条 行頭 → ### 第N条
  - （１）行頭 → 1. 番号付きリスト
  - 連続する Markdown table → ## シート N で見出し化 (xls 由来)
  - "Unnamed: N" を除去 (xls 由来)

入力 (Code ノードの Input variables, String):
  text:  Document Extractor の出力 ({{document_extractor.text}})

出力:
  markdown: str
  success:  bool
  error:    str
"""

import re

_VERSION_RE = re.compile(r"^Version[\w\-.]+$", re.IGNORECASE)
_DIGITS = "一二三四五六七八九十百千０１２３４５６７８９0123456789"
_ARTICLE_RE = re.compile(
    rf"^(第[{_DIGITS}]+条(?:の[{_DIGITS}]+)?)\s*(.*)"
)
_SECTION_RE = re.compile(r"^[（(](.+?)[）)]\s*$")
_NUMBERED_RE = re.compile(r"^[（(]([0-9０-９]+)[）)]\s*(.*)")
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")
_UNNAMED_RE = re.compile(r"Unnamed:\s*\d+")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|(\s*:?-+:?\s*\|)+\s*$")


def _annotate_sheets(text: str) -> str:
    """連続する Markdown table を ## シート N で見出し化"""
    lines = text.splitlines()
    out = []
    sheet_idx = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TABLE_LINE_RE.match(line) and (
            i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            sheet_idx += 1
            if out and out[-1].strip():
                out.append("")
            out.append(f"## シート {sheet_idx}")
            out.append("")
            while i < len(lines) and _TABLE_LINE_RE.match(lines[i]):
                out.append(lines[i])
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _format(text: str) -> str:
    text = _UNNAMED_RE.sub("", text)
    text = _annotate_sheets(text)
    lines = text.replace("\r\n", "\n").split("\n")
    out: list = []
    title_done = False

    def push_blank():
        if out and out[-1] != "":
            out.append("")

    for raw in lines:
        line = raw.strip()
        if not line:
            push_blank()
            continue
        if _VERSION_RE.match(line):
            continue
        if line.startswith("#"):
            out.append(line)
            push_blank()
            title_done = True
            continue
        if line.startswith("|"):
            out.append(line)
            title_done = True
            continue
        if not title_done and len(line) <= 30:
            out.append(f"# {line}")
            push_blank()
            title_done = True
            continue
        m = _SECTION_RE.match(line)
        if m:
            push_blank()
            out.append(f"## {m.group(1)}")
            push_blank()
            continue
        m = _ARTICLE_RE.match(line)
        if m:
            out.append(f"### {m.group(1)}")
            push_blank()
            body = m.group(2).strip()
            if body:
                out.append(body)
            continue
        m = _NUMBERED_RE.match(line)
        if m:
            n = m.group(1).translate(_ZEN2HAN)
            out.append(f"{int(n)}. {m.group(2).strip()}")
            continue
        out.append(line)

    cleaned = []
    for line in out:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip() + "\n"


def main(text: str) -> dict:
    if not text:
        return {"markdown": "", "success": False, "error": "text が空"}
    try:
        md = _format(text)
    except Exception as e:
        return {"markdown": "", "success": False, "error": f"format failed: {e}"}
    return {"markdown": md, "success": True, "error": ""}
