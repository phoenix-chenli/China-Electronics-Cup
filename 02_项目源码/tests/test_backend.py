"""Stage-one invariants. These generated fixtures are tests, not business samples."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.parsers import readonly_database


@pytest.fixture
def client(tmp_path):
    application = create_app(data_dir=tmp_path / "runtime", samples_dir=tmp_path / "samples", enable_workers=False)
    with TestClient(application) as connection:
        yield connection


def project(client, name="研究项目"):
    response = client.post("/api/projects", json={"name": name, "description": "测试夹具"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, project_id, name, content, process=True):
    response = client.post(f"/api/projects/{project_id}/import", files=[("files", (name, content))])
    assert response.status_code == 200, response.text
    result = response.json()
    if process:
        for job in result["jobs"]:
            client.app.state.service.process_job(job["id"])
    return result


def detail(client, source_id):
    response = client.get(f"/api/sources/{source_id}")
    assert response.status_code == 200
    return response.json()


def make_png():
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (64, 40), "white").save(stream, "PNG")
    return stream.getvalue()


def make_pdf(text=True):
    import pymupdf
    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), "Stage one actual PDF text: 2026")
    return document.tobytes()


def test_empty_instance_and_name_validation(client):
    assert client.get("/api/projects").json() == []
    health = client.get("/api/health").json()
    assert health["stage"] == 10 and health["version"] == "1.0.0-rc1"
    assert health["release_status"] == "competition-candidate"
    assert client.post("/api/projects", json={"name": "   "}).status_code == 422
    pid = project(client)
    assert client.patch(f"/api/projects/{pid}", json={"name": " 改名 "}).json()["name"] == "改名"
    assert client.get(f"/api/projects/{pid}/overview").json()["stats"]["sources"] == 0


def test_hash_original_dedup_and_project_isolation(client):
    first, second = project(client), project(client, "另一项目")
    original = "年份,编号,金额\n2026,001,18.50\n2025,002,12.00\n".encode()
    item = upload(client, first, "统计.csv", original)["sources"][0]
    assert detail(client, item["id"])["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    repeated = upload(client, first, "改名.csv", original)
    assert repeated["sources"] == [] and repeated["jobs"] == []
    assert repeated["duplicates"][0]["id"] == item["id"]
    other = upload(client, second, "统计.csv", original)["sources"][0]
    assert other["id"] != item["id"]
    assert len(client.get(f"/api/projects/{first}/sources").json()) == 1
    assert client.get(f"/api/sources/{item['id']}/original").content == original
    page = client.get(f"/api/sources/{item['id']}/tables/数据表").json()
    assert page["rows"][0] == ["2026", "001", "18.50"]
    assert client.post(f"/api/projects/{second}/drafts", json={"question": "查看金额", "source_ids": [item["id"]]}).status_code == 422


def test_table_pagination_and_injection_are_read_only(client, tmp_path):
    database = tmp_path / "business.db"
    with sqlite3.connect(database) as c:
        c.execute('CREATE TABLE "sales / 特殊表" (id INTEGER PRIMARY KEY, amount REAL)')
        c.executemany('INSERT INTO "sales / 特殊表" VALUES(?,?)', [(i, i * 2.5) for i in range(123)])
    before = database.read_bytes()
    sid = upload(client, project(client), "business.db", before)["sources"][0]["id"]
    table_path = f"/api/sources/{sid}/tables/" + quote("sales / 特殊表", safe="")
    first = client.get(table_path + "?offset=50&limit=20").json()
    assert first["row_count"] == 123 and len(first["rows"]) == 20
    assert first["rows"][0] == [50, 125.0]
    assert client.get(table_path + "?offset=999&limit=20").json()["rows"] == []
    assert client.get(table_path + "?limit=201").status_code == 422
    assert client.get(table_path + "?offset=-1").status_code == 422
    attack = quote('sales / 特殊表"; DROP TABLE "sales / 特殊表";--', safe="")
    assert client.get(f"/api/sources/{sid}/tables/{attack}").status_code == 404
    assert client.get(f"/api/sources/{sid}/original").content == before
    original = Path(client.app.state.service.store.get("sources", sid)["original_path"])
    with readonly_database(original) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute('DELETE FROM "sales / 特殊表"')
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("ATTACH ':memory:' AS evil")
    assert original.read_bytes() == before


@pytest.mark.parametrize("name,content", [("bad.pdf", b"not a pdf"), ("bad.docx", b"broken zip"), ("bad.xlsx", b"broken workbook"), ("bad.db", b"not sqlite"), ("bad.png", b"not png"), ("empty.txt", b"")])
def test_corrupt_files_remain_visible_and_retryable(client, name, content):
    pid = project(client)
    sid = upload(client, pid, name, content)["sources"][0]["id"]
    record = detail(client, sid)["source"]
    assert record["status"] == "failed" and record["error"]
    retry = client.post(f"/api/sources/{sid}/retry")
    assert retry.status_code == 200
    client.app.state.service.process_job(retry.json()["job"]["id"])
    assert detail(client, sid)["source"]["status"] == "failed"
    assert len(client.get(f"/api/projects/{pid}/jobs").json()) == 2
    assert client.get(f"/api/sources/{sid}/original").content == content


def test_unsupported_files_are_preserved_but_not_downloadable(client):
    sid = upload(client, project(client), "payload.html", b"<script>alert(1)</script>")["sources"][0]["id"]
    assert detail(client, sid)["source"]["status"] == "failed"
    assert client.get(f"/api/sources/{sid}/original?download=false").status_code == 415


def test_archive_restore_preserves_original_and_active_uniqueness(client):
    pid = project(client)
    original = b"Actual document"
    sid = upload(client, pid, "note.txt", original)["sources"][0]["id"]
    assert client.post(f"/api/sources/{sid}/archive").json() == {"ok": True}
    assert client.get(f"/api/projects/{pid}/sources").json() == []
    assert client.get(f"/api/projects/{pid}/sources?archived=true").json()[0]["id"] == sid
    assert client.get(f"/api/sources/{sid}/original").content == original
    replacement = upload(client, pid, "note.txt", original)["sources"][0]["id"]
    assert client.post(f"/api/sources/{sid}/restore").status_code == 409
    client.post(f"/api/sources/{replacement}/archive")
    assert client.post(f"/api/sources/{sid}/restore").status_code == 200


def test_drafts_require_readable_scoped_active_sources(client):
    pid = project(client)
    queued = upload(client, pid, "queued.txt", b"content", process=False)["sources"][0]["id"]
    image = upload(client, pid, "image.png", make_png())["sources"][0]["id"]
    assert detail(client, image)["source"]["status"] == "attention"
    assert detail(client, image)["source"]["usable"] is False
    assert client.get(f"/api/sources/{image}/pages/1/image").content.startswith(b"\x89PNG")
    for ids in ([], [queued], [image], ["missing"]):
        assert client.post(f"/api/projects/{pid}/drafts", json={"question": "如何理解", "source_ids": ids}).status_code == 422
    ready = upload(client, pid, "usable.txt", "真实文本".encode())["sources"][0]["id"]
    assert client.post(f"/api/projects/{pid}/drafts", json={"question": "  ", "source_ids": [ready]}).status_code == 422
    saved = client.post(f"/api/projects/{pid}/drafts", json={"question": " 如何理解 ", "source_ids": [ready, ready]})
    assert saved.status_code == 201 and saved.json()["source_ids"] == [ready]
    assert saved.json()["question"] == "如何理解"
    assert detail(client, ready)["source"]["usable"] is True
    assert "answer" not in saved.json()
    client.post(f"/api/sources/{ready}/archive")
    assert client.post(f"/api/projects/{pid}/drafts", json={"question": "如何理解", "source_ids": [ready]}).status_code == 422


def test_pdf_text_and_scan_attention_with_actual_preview(client):
    pid = project(client)
    native = upload(client, pid, "native.pdf", make_pdf())["sources"][0]["id"]
    scan = upload(client, pid, "scan.pdf", make_pdf(text=False))["sources"][0]["id"]
    assert detail(client, native)["source"]["status"] == "ready"
    assert "2026" in detail(client, native)["pages"][0]["text"]
    assert detail(client, scan)["source"]["status"] == "attention"
    assert detail(client, scan)["metadata"]["ocr_performed"] is False
    preview = client.get(f"/api/sources/{native}/pages/1/image")
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    assert client.get(f"/api/sources/{native}/pages/2/image").status_code == 404
    assert client.get(f"/api/sources/{scan}/pages/1/image").status_code == 200


def test_encrypted_pdf_has_actionable_failure(client):
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(make_pdf())))
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    sid = upload(client, project(client), "locked.pdf", buffer.getvalue())["sources"][0]["id"]
    assert "加密" in detail(client, sid)["source"]["error"]


def test_docx_logical_sections_and_real_table(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>研究范围</w:t></w:r></w:p><w:p><w:r><w:t>原生Word正文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>指标</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>10</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>''')
    sid = upload(client, project(client), "native.docx", buffer.getvalue())["sources"][0]["id"]
    result = detail(client, sid)
    assert result["source"]["status"] == "ready"
    assert result["metadata"]["page_unit"] == "logical_section"
    assert "原生Word正文" in result["pages"][0]["text"]
    assert result["tables"][0]["preview"] == [["指标", "10"]]


def test_xlsx_values_and_uncalculated_formula_warning(client):
    from openpyxl import Workbook
    book, buffer = Workbook(), io.BytesIO()
    sheet = book.active
    sheet.title = "年度指标"
    sheet.append(["编号", "金额", "公式"])
    sheet.append(["001", 18.5, "=B2*2"])
    book.save(buffer)
    sid = upload(client, project(client), "values.xlsx", buffer.getvalue())["sources"][0]["id"]
    result = detail(client, sid)
    assert result["source"]["status"] == "attention"
    assert result["metadata"]["formulas_evaluated"] is False
    assert result["tables"][0]["preview"][0] == ["001", 18.5, None]
    assert any("没有缓存值" in warning for warning in result["source"]["warnings"])


def test_reboot_recovers_interrupted_jobs_and_persists_drafts(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(create_app(data_dir=runtime, enable_workers=False)) as first:
        pid = project(first)
        ready = upload(first, pid, "ready.txt", b"Ready content")["sources"][0]["id"]
        draft = first.post(f"/api/projects/{pid}/drafts", json={"question": "What changed?", "source_ids": [ready]}).json()
        pending = upload(first, pid, "pending.txt", b"Pending content", process=False)
        job = pending["jobs"][0]
        first.app.state.service.store.claim(job["id"])
        assert detail(first, pending["sources"][0]["id"])["source"]["status"] == "processing"
    with TestClient(create_app(data_dir=runtime, enable_workers=False)) as second:
        restored = second.get(f"/api/projects/{pid}/jobs").json()
        assert next(item for item in restored if item["id"] == job["id"])["status"] == "queued"
        assert second.get(f"/api/projects/{pid}/drafts").json()[0]["id"] == draft["id"]
        second.app.state.service.process_job(job["id"])
        assert detail(second, pending["sources"][0]["id"])["source"]["status"] == "ready"
        assert second.get(f"/api/sources/{ready}/original").content == b"Ready content"


def test_demo_provenance_and_path_escape(client, tmp_path):
    pid = project(client)
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "public.txt").write_text("public source text", encoding="utf-8")
    manifest = [{"name": "公开原件", "file": "public.txt", "origin": "https://example.org/original", "license": "来源标注", "source_group": "publisher-one", "derived_from": "public-source"}]
    (samples / "manifest.json").write_text(json.dumps(manifest))
    first = client.post(f"/api/projects/{pid}/demo")
    assert first.status_code == 200
    sid = first.json()["sources"][0]["id"]
    assert detail(client, sid)["metadata"]["source_group"] == "publisher-one"
    assert detail(client, sid)["metadata"]["derived_from"] == "public-source"
    assert len(client.post(f"/api/projects/{pid}/demo").json()["duplicates"]) == 1
    manifest[0]["file"] = "../outside.txt"
    (tmp_path / "outside.txt").write_text("outside")
    (samples / "manifest.json").write_text(json.dumps(manifest))
    assert client.post(f"/api/projects/{pid}/demo").status_code == 422


def test_local_origin_and_no_query_execution_endpoint(client):
    assert client.post("/api/projects", json={"name": "blocked"}, headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/api/projects", headers={"Host": "attacker.example"}).status_code == 421
    assert client.post("/api/projects", json={"name": "blocked"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/query", json={"question": "anything"}).status_code in {404, 405}
    assert client.get("/api/projects").json() == []


def test_size_limit_does_not_leave_partial_import(client, monkeypatch):
    from backend import parsers
    pid = project(client)
    monkeypatch.setattr(parsers, "MAX_BYTES", 16)
    response = client.post(f"/api/projects/{pid}/import", files=[("files", ("small.txt", b"ok")), ("files", ("large.txt", b"a" * 17))])
    assert response.status_code == 413
    assert client.get(f"/api/projects/{pid}/sources").json() == []
    assert list((client.app.state.service.root / "incoming").iterdir()) == []


def test_background_workers_complete_real_jobs_and_demo_quality_notes(tmp_path):
    import time
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "notes.txt").write_text("Published text with an editorial note", encoding="utf-8")
    (samples / "manifest.json").write_text(json.dumps([{
        "file": "notes.txt", "name": "资料.txt", "origin": "https://example.org/source",
        "license": "sample", "quality_notes": ["原文两处口径不同，请结合注释阅读"]
    }]), encoding="utf-8")
    with TestClient(create_app(data_dir=tmp_path / "runtime", samples_dir=samples)) as client:
        pid = project(client)
        result = client.post(f"/api/projects/{pid}/demo").json()
        sid = result["sources"][0]["id"]
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            current = detail(client, sid)["source"]
            if current["status"] not in {"queued", "processing"}:
                break
            time.sleep(0.02)
        assert current["status"] == "attention" and current["usable"] is True
        assert current["warnings"] == ["资料审阅提示：原文两处口径不同，请结合注释阅读"]
        assert client.post(f"/api/projects/{pid}/drafts", json={"question": "解释这些不同", "source_ids": [sid]}).status_code == 201


def test_zip_bomb_and_docx_entities_are_rejected(client):
    pid = project(client)
    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * (2 * 1024 * 1024))
    sid = upload(client, pid, "suspicious.docx", compressed.getvalue())["sources"][0]["id"]
    assert "压缩比异常" in detail(client, sid)["source"]["error"]
    entities = io.BytesIO()
    with zipfile.ZipFile(entities, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>')
    sid = upload(client, pid, "entities.docx", entities.getvalue())["sources"][0]["id"]
    assert "实体声明" in detail(client, sid)["source"]["error"]


def test_exif_preview_uses_image_orientation(client):
    from PIL import Image
    buffer = io.BytesIO()
    image = Image.new("RGB", (80, 40), "blue")
    exif = Image.Exif()
    exif[274] = 6
    image.save(buffer, "JPEG", exif=exif)
    sid = upload(client, project(client), "rotated.jpg", buffer.getvalue())["sources"][0]["id"]
    preview = client.get(f"/api/sources/{sid}/pages/1/image")
    assert preview.status_code == 200
    with Image.open(io.BytesIO(preview.content)) as result:
        assert result.size == (40, 80)


def test_double_running_instances_cannot_share_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(create_app(data_dir=runtime, enable_workers=False)):
        with pytest.raises(RuntimeError, match="另一个知衡进程"):
            with TestClient(create_app(data_dir=runtime, enable_workers=False)):
                pass


@pytest.mark.parametrize("storage", ["STORED", "VIRTUAL"])
def test_sqlite_generated_columns_keep_names_aligned_with_values(client, tmp_path, storage):
    database = tmp_path / "generated.db"
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE sales(price REAL, quantity INTEGER, amount REAL GENERATED ALWAYS AS(price*quantity) {storage}, label TEXT)")
        connection.execute("INSERT INTO sales(price,quantity,label) VALUES(12.5,2,'样品')")
    sid = upload(client, project(client), "generated.db", database.read_bytes())["sources"][0]["id"]
    source = detail(client, sid)
    assert source["source"]["status"] == "ready"
    table = source["tables"][0]
    assert [column["name"] for column in table["columns"]] == ["price", "quantity", "amount", "label"]
    assert table["preview"] == [[12.5, 2, 25.0, "样品"]]
    paged = client.get(f"/api/sources/{sid}/tables/sales").json()
    assert paged["columns"] == table["columns"]
    assert paged["rows"] == table["preview"]


@pytest.mark.parametrize("kind", ["horizontal", "vertical", "missing_origin"])
def test_docx_merged_cells_preserve_grid_and_mark_ambiguity(client, kind):
    if kind == "horizontal":
        grid = '<w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>'
        rows = '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>合并标题</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>第三列标题</w:t></w:r></w:p></w:tc></w:tr>'
    else:
        grid = '<w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>'
        rows = '' if kind == "missing_origin" else '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>区域A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc></w:tr>'
        rows += '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>200</w:t></w:r></w:p></w:tc></w:tr>'
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>' + grid + rows + '</w:tbl></w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('word/document.xml', xml)
    sid = upload(client, project(client), kind + '.docx', buffer.getvalue())["sources"][0]["id"]
    result = detail(client, sid)
    assert result["source"]["status"] == "attention"
    layout = result["metadata"]["table_layouts"][0]
    if kind == "horizontal":
        assert result["tables"][0]["preview"] == [["合并标题", None, "第三列标题"]]
        assert layout["merged_cells"][0]["column_span"] == 2
    elif kind == "vertical":
        assert result["tables"][0]["preview"] == [["区域A", "100"], [None, "200"]]
        assert layout["merged_cells"][1]["origin"] == {"row": 1, "column": 1}
    else:
        assert result["tables"] == [] and layout["reliable_grid"] is False
        assert "200" in result["pages"][0]["text"]


def test_manual_review_preserves_parse_truth_and_requires_completed_source(client):
    pid = project(client)
    ready = upload(client, pid, "readable.txt", b"Readable content")["sources"][0]["id"]
    image = upload(client, pid, "scan.png", make_png())["sources"][0]["id"]
    failed = upload(client, pid, "broken.pdf", b"broken")["sources"][0]["id"]
    queued = upload(client, pid, "queued.txt", b"Still waiting", process=False)
    pending_id = queued["sources"][0]["id"]
    body = {"status": "reviewed", "note": " 已查看原件，仍需文字识别 "}

    before = detail(client, image)["source"]
    reviewed = client.patch(f"/api/sources/{image}/review", json=body)
    assert reviewed.status_code == 200
    result = reviewed.json()
    assert result["review_status"] == "reviewed" and result["reviewed_at"]
    assert result["review_note"] == body["note"].strip()
    assert result["reviewed_parser_version"] == before["parser_version"]
    for field in ("status", "warnings", "usable", "sha256", "page_count", "table_count"):
        assert result[field] == before[field]
    assert result["status"] == "attention" and result["usable"] is False
    assert client.patch(f"/api/sources/{ready}/review", json={"status": "reviewed", "note": ""}).json()["usable"] is True

    for sid in (failed, pending_id):
        assert client.patch(f"/api/sources/{sid}/review", json=body).status_code == 409
        assert detail(client, sid)["source"]["review_status"] == "pending"
    client.app.state.service.store.claim(queued["jobs"][0]["id"])
    assert client.patch(f"/api/sources/{pending_id}/review", json=body).status_code == 409
    pending = client.patch(f"/api/sources/{image}/review", json={"status": "pending", "note": "稍后再核对"}).json()
    assert pending["reviewed_at"] is None and pending["reviewed_parser_version"] is None
    assert pending["review_note"] == "稍后再核对" and pending["warnings"] == before["warnings"]
    assert client.patch(f"/api/sources/{image}/review", json={"status": "accepted", "note": ""}).status_code == 422
    assert client.patch(f"/api/sources/{image}/review", json={"status": "reviewed", "note": "x" * 2001}).status_code == 422
    assert client.patch("/api/sources/missing/review", json=body).status_code == 404


def test_reparse_invalidates_manual_review_and_records_current_parser(client, monkeypatch):
    from backend import parsers
    sid = upload(client, project(client), "image.png", make_png())["sources"][0]["id"]
    original = detail(client, sid)["source"]
    client.patch(f"/api/sources/{sid}/review", json={"status": "reviewed", "note": "仅核对原图，识别尚未完成"})
    retry = client.post(f"/api/sources/{sid}/retry").json()
    assert retry["source"]["review_status"] == "pending"
    assert retry["source"]["reviewed_at"] is None and retry["source"]["reviewed_parser_version"] is None
    assert retry["source"]["review_note"] == "仅核对原图，识别尚未完成"
    monkeypatch.setattr(parsers, "PARSER_VERSION", "test-native-next")
    client.app.state.service.process_job(retry["job"]["id"])
    parsed = detail(client, sid)["source"]
    assert parsed["review_status"] == "pending" and parsed["warnings"] == original["warnings"]
    assert parsed["parser_version"] == "test-native-next"
    rereview = client.patch(f"/api/sources/{sid}/review", json={"status": "reviewed", "note": "重新查看原图"}).json()
    assert rereview["reviewed_parser_version"] == "test-native-next"


def test_ready_source_can_upgrade_parser_without_duplicate_or_original_change(client, monkeypatch):
    from backend import parsers
    pid = project(client)
    content = b"name,amount\nA,12\n"
    sid = upload(client, pid, "upgrade.csv", content)["sources"][0]["id"]
    initial = detail(client, sid)["source"]
    assert initial["status"] == "ready" and not initial["reprocess_available"]
    assert client.post(f"/api/sources/{sid}/retry").status_code == 409
    monkeypatch.setattr(parsers, "PARSER_VERSION", "test-multimodal-next")
    assert detail(client, sid)["source"]["reprocess_available"]
    duplicate = upload(client, pid, "same.csv", content)
    assert duplicate["duplicates"][0]["id"] == sid
    assert duplicate["duplicates"][0]["reprocess_available"]
    retry = client.post(f"/api/sources/{sid}/retry")
    assert retry.status_code == 200
    assert not retry.json()["source"]["reprocess_available"]
    assert client.post(f"/api/sources/{sid}/retry").status_code == 409
    client.app.state.service.process_job(retry.json()["job"]["id"])
    parsed = detail(client, sid)["source"]
    assert parsed["id"] == sid and parsed["sha256"] == initial["sha256"]
    assert parsed["parser_version"] == "test-multimodal-next" and not parsed["reprocess_available"]
    assert client.get(f"/api/sources/{sid}/original").content == content


def test_archived_source_rejects_review_without_changing_saved_note(client):
    sid = upload(client, project(client), "notes.txt", b"Original notes")["sources"][0]["id"]
    path = f"/api/sources/{sid}"
    client.patch(path + "/review", json={"status": "reviewed", "note": "归档前已保存的备注"})
    assert client.post(path + "/archive").status_code == 200
    archived = detail(client, sid)["source"]
    for status in ("pending", "reviewed"):
        response = client.patch(path + "/review", json={"status": status, "note": "另一个标签页的过期编辑"})
        assert response.status_code == 409
        assert "恢复已归档资料" in response.json()["detail"]
        assert detail(client, sid)["source"] == archived
    assert client.post(path + "/restore").status_code == 200
    response = client.patch(path + "/review", json={"status": "reviewed", "note": "恢复后重新核对"})
    assert response.status_code == 200 and response.json()["review_note"] == "恢复后重新核对"


def test_draft_edit_archive_restore_and_cross_project_isolation(client):
    pid, other = project(client), project(client, "另一项目")
    first = upload(client, pid, "first.txt", b"First content")["sources"][0]["id"]
    second = upload(client, pid, "second.txt", b"Second content")["sources"][0]["id"]
    foreign = upload(client, other, "foreign.txt", b"Foreign content")["sources"][0]["id"]
    image = upload(client, pid, "scan.png", make_png())["sources"][0]["id"]
    draft = client.post(f"/api/projects/{pid}/drafts", json={"question": "原问题", "source_ids": [first]}).json()
    path = f"/api/projects/{pid}/drafts/{draft['id']}"
    assert draft["archived"] is False and draft["updated_at"] == draft["created_at"]

    for ids in ([foreign], [image], [], ["missing"]):
        assert client.patch(path, json={"question": "不能保存的编辑", "source_ids": ids}).status_code == 422
    assert client.get(f"/api/projects/{pid}/drafts").json()[0]["question"] == "原问题"
    edited = client.patch(path, json={"question": " 修订后的问题 ", "source_ids": [second, second]}).json()
    assert edited["id"] == draft["id"] and edited["created_at"] == draft["created_at"]
    assert edited["question"] == "修订后的问题" and edited["source_ids"] == [second]
    assert edited["updated_at"] >= draft["updated_at"]
    foreign_path = f"/api/projects/{other}/drafts/{draft['id']}"
    assert client.patch(foreign_path, json={"question": "串项目", "source_ids": [foreign]}).status_code == 404
    assert client.post(foreign_path + "/archive").status_code == 404
    assert client.post(foreign_path + "/restore").status_code == 404

    assert client.post(path + "/archive").json() == {"ok": True}
    assert client.post(path + "/archive").status_code == 200
    assert client.get(f"/api/projects/{pid}/drafts").json() == []
    assert client.get(f"/api/projects/{pid}/overview").json()["drafts"] == []
    archived = client.get(f"/api/projects/{pid}/drafts?archived=true").json()
    assert len(archived) == 1 and archived[0]["archived"] is True
    assert client.patch(path, json={"question": "已归档不可编辑", "source_ids": [first]}).status_code == 409
    client.post(f"/api/sources/{second}/archive")
    assert client.post(path + "/restore").json() == {"ok": True}
    restored = client.get(f"/api/projects/{pid}/drafts").json()[0]
    assert restored["question"] == edited["question"] and restored["source_ids"] == [second]
    assert restored["archived"] is False
    assert client.patch(path, json={"question": "旧选源已归档", "source_ids": [second]}).status_code == 422
    repaired = client.patch(path, json={"question": "重新选择有效资料", "source_ids": [first]}).json()
    assert repaired["source_ids"] == [first] and repaired["id"] == draft["id"]


def test_additive_migration_preserves_old_data_and_new_fields_persist(tmp_path):
    runtime = tmp_path / "runtime"
    with TestClient(create_app(data_dir=runtime, enable_workers=False)) as client:
        pid = project(client)
        payload = b"Original source must not change"
        sid = upload(client, pid, "legacy.txt", payload)["sources"][0]["id"]
        draft = client.post(f"/api/projects/{pid}/drafts", json={"question": "迁移前问题", "source_ids": [sid]}).json()
    # Recreate the previous database shape, with populated original rows.
    with sqlite3.connect(runtime / "workspace.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DROP INDEX drafts_project")
        for column in ("review_status", "review_note", "reviewed_at", "reviewed_parser_version"):
            connection.execute(f"ALTER TABLE sources DROP COLUMN {column}")
        for column in ("archived", "updated_at"):
            connection.execute(f"ALTER TABLE drafts DROP COLUMN {column}")
        old_source = dict(connection.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())
        old_draft = dict(connection.execute("SELECT * FROM drafts WHERE id=?", (draft["id"],)).fetchone())
    with TestClient(create_app(data_dir=runtime, enable_workers=False)) as client:
        migrated = detail(client, sid)["source"]
        assert migrated["review_status"] == "pending" and migrated["review_note"] == ""
        assert migrated["reviewed_at"] is None and migrated["reviewed_parser_version"] is None
        assert client.get(f"/api/sources/{sid}/original").content == payload
        stored = client.app.state.service.store.get("sources", sid)
        for key, value in old_source.items():
            expected = json.loads(value) if key in {"warnings", "pages", "tables", "metadata"} else bool(value) if key == "archived" else value
            assert stored[key] == expected
        migrated_draft = client.get(f"/api/projects/{pid}/drafts").json()[0]
        for key, value in old_draft.items():
            assert migrated_draft[key] == (json.loads(value) if key == "source_ids" else value)
        assert migrated_draft["updated_at"] == old_draft["created_at"] and not migrated_draft["archived"]
        assert client.post(f"/api/projects/{pid}/drafts", json={"question": "迁移后新增", "source_ids": [sid]}).status_code == 201
        review = client.patch(f"/api/sources/{sid}/review", json={"status": "reviewed", "note": "原件已核对"}).json()
        client.post(f"/api/projects/{pid}/drafts/{draft['id']}/archive")
    with TestClient(create_app(data_dir=runtime, enable_workers=False)) as client:
        assert detail(client, sid)["source"]["reviewed_at"] == review["reviewed_at"]
        assert detail(client, sid)["source"]["review_note"] == "原件已核对"
        assert client.get(f"/api/projects/{pid}/drafts?archived=true").json()[0]["id"] == draft["id"]
        assert len(client.get(f"/api/projects/{pid}/drafts").json()) == 1


def test_public_source_provenance_is_real_metadata_only(client, tmp_path):
    pid = project(client)
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "source.txt").write_text("A real stored source", encoding="utf-8")
    (samples / "manifest.json").write_text(json.dumps([{
        "file": "source.txt", "origin": "https://example.org/document", "source_group": "official-publication", "derived_from": "original.pdf"
    }]), encoding="utf-8")
    imported = client.post(f"/api/projects/{pid}/demo").json()["sources"][0]
    assert imported["origin"] == "https://example.org/document"
    assert imported["source_group"] == "official-publication" and imported["derived_from"] == "original.pdf"
    plain = upload(client, pid, "plain.txt", b"No provenance metadata")["sources"][0]
    assert plain["origin"] is None and plain["source_group"] is None and plain["derived_from"] is None
    with client.app.state.service.store.transaction() as connection:
        connection.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps({"origin": ["not-a-url"], "source_group": " ", "derived_from": {"file": "name"}}), plain["id"]))
    invalid = detail(client, plain["id"])["source"]
    assert invalid["origin"] is None and invalid["source_group"] is None and invalid["derived_from"] is None
