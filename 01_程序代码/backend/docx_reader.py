"""Read a DOCX body in document order without a third-party Word library.

A .docx is an OPC (ZIP) package whose body lives in word/document.xml. Only the
standard library is used, so the portable runtime needs no new binary wheel and
no new licence obligation. Heading levels come from the levels the file itself
declares (w:outlineLvl or a heading style name); nothing is inferred from font
size or position.

DOCX stores no fixed page layout. This module therefore exposes one logical
page and reports paragraph/table order instead of inventing page breaks.

``build`` assembles the same element shape into a minimal valid package. It
exists for synthetic samples and test fixtures, not for editing user files.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape as _escape

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BODY_PART = "word/document.xml"
STYLES_PART = "word/styles.xml"
HEADING_LIMIT = 9
MAX_PART_BYTES = 40 * 1024 * 1024
HEADING_NAME = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.I)
# Runs whose text is not displayed content in the rendered document.
SKIPPED = frozenset({W + name for name in
                     ("del", "instrText", "fldChar", "drawing", "pict", "object", "tbl", "commentReference")})


def _part(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise ValueError("DOCX 内部部件过大，已停止解析")
    try:
        return ElementTree.fromstring(archive.read(name))
    except ElementTree.ParseError as exc:
        raise ValueError("DOCX 内部 XML 无法解析") from exc


def _style_id(paragraph):
    properties = paragraph.find(W + "pPr")
    if properties is None:
        return None
    style = properties.find(W + "pStyle")
    return style.get(W + "val") if style is not None else None


def _outline_level(properties):
    if properties is None:
        return 0
    outline = properties.find(W + "outlineLvl")
    if outline is None:
        return 0
    value = (outline.get(W + "val") or "").strip()
    if not value.lstrip("-").isdigit():
        return 0
    number = int(value)
    return number + 1 if 0 <= number <= 8 else 0


def _paragraph_level(paragraph, levels):
    """A direct outline level wins; otherwise use the level its style declares."""
    own = _outline_level(paragraph.find(W + "pPr"))
    if own:
        return own
    style = _style_id(paragraph)
    return levels.get(style, 0) if style else 0


def _heading_levels(styles):
    """Style id -> heading level, from declared outline levels or style names."""
    levels = {}
    if styles is None:
        return levels
    for style in styles.findall(W + "style"):
        if style.get(W + "type") not in (None, "paragraph"):
            continue
        style_id = style.get(W + "styleId")
        if not style_id:
            continue
        level = _outline_level(style.find(W + "pPr"))
        if not level:
            node = style.find(W + "name")
            label = ((node.get(W + "val") if node is not None else "") or "").strip()
            for candidate in (label, style_id):
                match = HEADING_NAME.match(candidate)
                if match:
                    level = int(match.group(1))
                    break
        if level:
            levels[style_id] = level
    return levels


def _text(node, out):
    for child in node:
        tag = child.tag
        if tag in SKIPPED:
            continue
        if tag == W + "t":
            out.append(child.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
        else:
            _text(child, out)


def _paragraph_text(node):
    parts = []
    _text(node, parts)
    return "".join(parts).rstrip("\n")


def _table_rows(node):
    rows = []
    for row in node.findall(W + "tr"):
        cells = []
        for cell in row.findall(W + "tc"):
            parts = []
            _text(cell, parts)
            cells.append(" ".join("".join(parts).split()))
        if cells:
            rows.append(cells)
    return rows


def _walk(container, out, levels):
    for node in container:
        if node.tag == W + "p":
            out.append({"kind": "paragraph", "text": _paragraph_text(node),
                        "level": _paragraph_level(node, levels)})
        elif node.tag == W + "tbl":
            rows = _table_rows(node)
            if rows:
                out.append({"kind": "table", "rows": rows})
        elif node.tag == W + "sdt":
            content = node.find(W + "sdtContent")
            if content is not None:
                _walk(content, out, levels)


def elements(path):
    """Document-order body elements: paragraphs (with declared level) and tables."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            document = _part(archive, BODY_PART)
            styles = _part(archive, STYLES_PART)
    except zipfile.BadZipFile:
        raise ValueError("DOCX 无法读取：文件可能已损坏或不是有效的 Word 文档") from None
    if document is None:
        raise ValueError("DOCX 缺少正文部件，无法解析")
    body = document.find(W + "body")
    if body is None:
        raise ValueError("DOCX 正文结构不正确")
    out = []
    _walk(body, out, _heading_levels(styles))
    return out


def has_media(path):
    """True only when the package actually embeds an image part.

    Used to decide whether "no extractable text" means "needs recognition"
    instead of assuming every empty body is a scan.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            return any(name.startswith("word/media/") and not name.endswith("/")
                       for name in archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def page(path):
    """One logical page: continuous text plus ordered blocks and headings.

    Block offsets index the returned text, so existing range location works.
    """
    text = ""
    blocks = []
    headings = []
    titles = []
    index = 0
    tables = 0

    def emit(value, kind, level=0):
        nonlocal text, index
        block = None
        if value.strip():
            start = len(text)
            text += value
            block = {"id": "p1-b" + str(index), "start": start, "end": len(text), "text": value,
                     "kind": kind, "level": level, "title_path": " / ".join(titles), "box": None, "chars": []}
            blocks.append(block)
            index += 1
        text += "\n"
        return block

    for element in elements(path):
        if element["kind"] == "paragraph":
            value = element["text"]
            level = element["level"]
            if level and value.strip():
                # Update the path before emitting so the heading carries itself,
                # matching the Markdown reader's title_path convention.
                titles[:] = titles[:level - 1] + [value.strip()]
            block = emit(value, "heading" if level else "text", level)
            if level and block:
                headings.append({"title": value.strip(), "level": level, "page": 1,
                                 "start": block["start"], "end": block["end"], "block_id": block["id"]})
        else:
            tables += 1
            rows = element["rows"]
            width = max(len(row) for row in rows)
            for position, row in enumerate(rows):
                cells = [cell.replace("|", "\\|") for cell in list(row) + [""] * (width - len(row))]
                emit("| " + " | ".join(cells) + " |", "table_row")
                if position == 0 and len(rows) > 1:
                    emit("| " + " | ".join(["---"] * width) + " |", "table_row")
    if not blocks:
        blocks.append({"id": "p1-b0", "start": 0, "end": 0, "text": "", "kind": "text", "level": 0,
                       "title_path": "", "box": None, "chars": []})
    return {"number": 1, "text": text, "blocks": blocks, "headings": headings, "tables": tables}


CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                 '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                 '</Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
             '</Relationships>')
DOCUMENT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                 '</Relationships>')


def _styles_xml():
    parts = ['<w:styles xmlns:w="' + NS + '">',
             '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>']
    for level in range(1, HEADING_LIMIT + 1):
        parts.append('<w:style w:type="paragraph" w:styleId="Heading' + str(level) + '">'
                     '<w:name w:val="heading ' + str(level) + '"/>'
                     '<w:pPr><w:outlineLvl w:val="' + str(level - 1) + '"/></w:pPr></w:style>')
    parts.append('</w:styles>')
    return "".join(parts)


def _run(text):
    parts = []
    for line_index, line in enumerate(str(text).split("\n")):
        if line_index:
            parts.append("<w:br/>")
        for piece_index, piece in enumerate(line.split("\t")):
            if piece_index:
                parts.append("<w:tab/>")
            if piece:
                parts.append('<w:t xml:space="preserve">' + _escape(piece) + "</w:t>")
    return "<w:r>" + "".join(parts) + "</w:r>"


def _table_xml(rows):
    body = []
    for row in rows:
        cells = ["<w:tc><w:p>" + _run(cell) + "</w:p></w:tc>" for cell in row]
        body.append("<w:tr>" + "".join(cells) + "</w:tr>")
    return "<w:tbl>" + "".join(body) + "</w:tbl>"


def build(elements, *, title=None):
    """Assemble the element shape returned by ``elements`` into DOCX bytes."""
    body = []
    for element in elements:
        if element.get("kind") == "paragraph":
            level = min(max(int(element.get("level") or 0), 0), HEADING_LIMIT)
            properties = "" if not level else '<w:pPr><w:pStyle w:val="Heading' + str(level) + '"/></w:pPr>'
            body.append("<w:p>" + properties + _run(element.get("text", "")) + "</w:p>")
        elif element.get("kind") == "table":
            rows = element.get("rows") or []
            if rows:
                body.append(_table_xml(rows))
        else:
            raise ValueError("无法写入该元素：" + str(element.get("kind")))
    core = ""
    if title:
        core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
                ' xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>' + _escape(str(title)) + "</dc:title></cp:coreProperties>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="' + NS + '"><w:body>' + "".join(body) +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr(BODY_PART, document)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        archive.writestr(STYLES_PART, _styles_xml())
        if core:
            archive.writestr("docProps/core.xml", core)
    return out.getvalue()
