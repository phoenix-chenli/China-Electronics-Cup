"""Disposable PDF extraction worker; parent enforces wall-clock limits."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf

MAX_STREAM = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000


def normalized(rect, page):
    transformed = pymupdf.Rect(rect) * page.rotation_matrix
    bounds = page.rect
    return [round(max(0, min(1, v)), 6) for v in (transformed.x0 / bounds.width, transformed.y0 / bounds.height, transformed.x1 / bounds.width, transformed.y1 / bounds.height)]


def _tables_unrotated(page):
    if len(page.get_drawings()) > 12000:
        return [], ["页面矢量线条过多，已略过表格结构检测；原文仍保留"]
    tables, notices = [], []
    finder = page.find_tables(strategy="lines_strict")
    for table in finder.tables[:20]:
        if table.row_count < 2 or table.col_count < 2:
            continue
        if table.row_count * table.col_count > 10000:
            notices.append("单张 PDF 表格超过 10,000 格，已保留文字而略过结构提取")
            continue
        rows = table.extract()
        cells = []
        physical = []
        for r, row in enumerate(table.rows):
            for c, rectangle in enumerate(row.cells):
                if rectangle is not None:
                    physical.append((r, c, tuple(rectangle)))
        # Boundaries come from physical cells, including actual merged rectangles.
        xs = sorted(set(round(x, 2) for _, _, b in physical for x in (b[0], b[2])))
        ys = sorted(set(round(y, 2) for _, _, b in physical for y in (b[1], b[3])))
        merged = False
        for r, row in enumerate(table.rows):
            for c, rectangle in enumerate(row.cells):
                text = rows[r][c] if r < len(rows) and c < len(rows[r]) else None
                rowspan = colspan = 1
                covered = None
                if rectangle is not None:
                    b = tuple(rectangle)
                    rowspan = max(1, sum(b[1] + .5 < y < b[3] - .5 for y in ys) + 1)
                    colspan = max(1, sum(b[0] + .5 < x < b[2] - .5 for x in xs) + 1)
                    merged |= rowspan > 1 or colspan > 1
                elif len(xs) >= table.col_count + 1 and len(ys) >= table.row_count + 1:
                    cx, cy = (xs[c] + xs[c + 1]) / 2, (ys[r] + ys[r + 1]) / 2
                    origin = next(((rr + 1, cc + 1) for rr, cc, b in physical if b[0] < cx < b[2] and b[1] < cy < b[3]), None)
                    covered = list(origin) if origin else None
                cells.append(dict(row=r + 1, col=c + 1, text=text, bbox=normalized(rectangle, page) if rectangle else None,
                                  rowspan=rowspan, colspan=colspan, covered_by=covered, confidence=None))
        tables.append(dict(bbox=normalized(table.bbox, page), extraction="native_grid", rows=rows, row_count=table.row_count,
                           column_count=table.col_count, cells=cells, header_names=list(table.header.names or []), has_merged_cells=merged,
                           data_row_offset=0, requires_review=merged))
    return tables, notices


def _tables(page):
    # find_tables works in the current page orientation, unlike get_text's native
    # coordinates. Extract an upright grid, then map exactly once to preview space.
    rotation = page.rotation
    try:
        page.set_rotation(0)
        tables, notices = _tables_unrotated(page)
        if rotation:
            from .ocr_native import inverse_box
            for table in tables:
                table["bbox"] = inverse_box(table["bbox"], rotation)
                for cell in table["cells"]:
                    if cell["bbox"] is not None:
                        cell["bbox"] = inverse_box(cell["bbox"], rotation)
        return tables, notices
    finally:
        page.set_rotation(rotation)


def extract(path, index=None, image_path=None):
    with pymupdf.open(path) as document:
        if document.needs_pass and not document.authenticate(""):
            raise ValueError("PDF 已加密，请解密后重新导入")
        if not len(document):
            raise ValueError("PDF 没有可读取的页面")
        if index is None:
            return dict(page_count=len(document), outline=[dict(level=r[0], title=r[1], page=r[2] if r[2] > 0 else None, source="pdf_bookmark") for r in document.get_toc(simple=True)[:2000]])
        page = document[index]
        if page.rect.width <= 0 or page.rect.height <= 0 or max(page.rect.width, page.rect.height) > 100000:
            raise ValueError("PDF 页面尺寸不受支持")
        # Content-stream decompression is explicitly capped before MuPDF text parsing.
        from pypdf import PdfReader, overwrite_configuration
        overwrite_configuration(maximum_declared_stream_length=MAX_STREAM, array_based_stream_maximum_output_length=MAX_STREAM,
                                zlib_maximum_output_length=MAX_STREAM, lzw_maximum_output_length=MAX_STREAM,
                                run_length_maximum_output_length=MAX_STREAM, jbig2_maximum_output_length=MAX_STREAM,
                                image_maximum_buffer_size=MAX_STREAM, stream_decoding_work_maximum_length=64 * 1024 * 1024,
                                page_tree_maximum_entries=20000, xform_maximum_invocations_per_extraction=500,
                                jbig2dec_binary=None, disable_legacy_handling=True)
        reader = PdfReader(path)
        if reader.is_encrypted:
            reader.decrypt("")
        contents = reader.pages[index].get_contents()
        if contents is not None and len(contents.get_data()) > MAX_STREAM:
            raise ValueError("PDF 单页内容流超过安全上限")
        images = page.get_images(full=True)
        if any(int(i[2]) * int(i[3]) > MAX_PIXELS for i in images):
            raise ValueError("PDF 内嵌图片超过 4,000 万像素，请缩小后重试")
        blocks = []
        native = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES, sort=True)
        for group in native["blocks"]:
            for line in group.get("lines", []):
                text = "".join(span["text"] for span in line["spans"]).strip()
                if text:
                    blocks.append(dict(text=text, bbox=normalized(line["bbox"], page), confidence=None,
                                       font_size=max((round(float(s['size']), 2) for s in line['spans'] if s.get('text', '').strip()), default=None),
                                       bold=all(bool(s.get('flags', 0) & 16) for s in line['spans'] if s.get('text', '').strip()),
                                       typography=[dict(text=s['text'], size=round(float(s['size']), 2), flags=s.get('flags', 0),
                                                        bbox=normalized(s['bbox'], page)) for s in line['spans'][:64]],
                                       kind="formula_candidate" if any(s in text for s in ("=", "∑", "∫", "√", "≤", "≥")) else "text"))
        if sum(len(b["text"]) for b in blocks) > 2_000_000:
            raise ValueError("PDF 单页文字超过处理上限")
        tables, notices = _tables(page)
        # Image info exposes actual placements without decoding image pixels.
        # Multiple uses/overlaps are unioned in original-page coordinates.
        from .adaptive_processing import geometry, MAX_BOXES
        placements = page.get_image_info(xrefs=False)
        image_boxes = [normalized(item['bbox'], page) for item in placements[:MAX_BOXES]]
        page_features = geometry(blocks, image_boxes, tables, geometry_truncated=len(placements) > MAX_BOXES)
        scale = min(3, 2800 / max(page.rect.width, page.rect.height))
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(image_path)
        return dict(blocks=blocks, has_image=bool(images or placements), tables=tables, warnings=notices, page_rotation=page.rotation,
                    page_features=page_features,
                    printed_page_label=page.get_label() or None,
                    width=page.rect.width, height=page.rect.height)


if __name__ == "__main__":
    try:
        value = extract(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
        print(json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:500]}, ensure_ascii=False))
        sys.exit(1)
