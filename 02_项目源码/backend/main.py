"""Local-only source workbench and versioned multimodal answer API."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import parsers
from .service import Service, filename, file_format
from .store import identity, public_source
from .answer_service import AnswerService
from .model_gateway import ModelGateway, ModelError
from .source_scope import MAX_ANSWER_SOURCES
from .export_service import ExportError, export_answer
from .product_api import ProductServices, register_product_routes
from .library_routes import LibrarySearchJobs, register_library_routes

WORKSPACE = Path(__file__).resolve().parents[1]


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    source_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入准备提问的问题")
        return value

    @field_validator("source_ids")
    @classmethod
    def valid_ids(cls, value):
        if any(not item.strip() or len(item) > 80 for item in value):
            raise ValueError("资料标识无效")
        return list(dict.fromkeys(value))


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "reviewed"]
    note: str = Field(max_length=2000)

    @field_validator("note")
    @classmethod
    def valid_note(cls, value):
        return value.strip()


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    source_id: str = Field(min_length=1, max_length=80)
    mode: Literal["auto", "table", "document", "database", "multi_source"] = 'auto'
    source_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_ANSWER_SOURCES)
    table_name: str | None = Field(default=None, max_length=500)
    request_id: UUID
    parent_answer_id: str | None = Field(default=None, min_length=1, max_length=80)
    context_patch: list[dict] | None = Field(default=None, max_length=24)
    expected_context_revision: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入问题")
        return value


class ModelsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: dict[str, str]


class SemanticPatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0,strict=True)
    expected_source_revision: str = Field(min_length=64,max_length=64)
    action: Literal['confirm','save_candidate','revoke']
    definition: dict | None = None
    definition_id: str | None = Field(default=None,max_length=100)
    confirmed: bool | None = None


def create_app(data_dir=None, samples_dir=None, enable_workers=True, answer_gateway_factory=None, retrieval_factory=None):
    root = Path(data_dir or os.environ.get("ZHIHENG_DATA_DIR", WORKSPACE / ".runtime"))
    service = Service(root, samples_dir or WORKSPACE / "samples", workers=enable_workers)
    answers = AnswerService(service, answer_gateway_factory or ModelGateway,retrieval_factory)
    products = ProductServices(service,answers)
    library = LibrarySearchJobs(service,answers)

    @asynccontextmanager
    async def lifespan(application):
        service.start()
        application.state.service = service
        try:
            answers.start()
            products.start()
            yield
        finally:
            library.close()
            products.close()
            answers.close()
            service.close()

    application = FastAPI(title="知衡", version="1.0.0-rc1", lifespan=lifespan, docs_url=None, redoc_url=None)
    application.state.service = service
    application.state.answers = answers
    application.state.products = products
    register_product_routes(application,products)
    application.state.library_search = library
    register_library_routes(application,library)

    @application.middleware("http")
    async def local_origin(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=421)
        if request.url.path.startswith("/api"):
            origin = request.headers.get("origin")
            allowed = {f"http://{request.headers.get('host', '')}", "http://127.0.0.1:5173", "http://localhost:5173"}
            if origin and origin not in allowed:
                return JSONResponse({"detail": "已拒绝非同源请求"}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "已拒绝跨站修改请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        messages = [str(error.get("msg", "输入有误")).removeprefix("Value error, ") for error in exc.errors()]
        return JSONResponse({"detail": "输入有误：" + "；".join(messages[:3])}, status_code=422)

    def project_or_404(project_id):
        project = service.store.get("projects", project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        return project

    def source_or_404(source_id):
        source = service.store.get("sources", source_id)
        if not source:
            raise HTTPException(404, "资料不存在")
        return source

    def draft_or_404(project_id, draft_id):
        project_or_404(project_id)
        draft = service.store.draft(project_id, draft_id)
        if not draft:
            raise HTTPException(404, "问题不存在或不属于当前项目")
        return draft

    @application.get("/api/health")
    def health():
        return dict(service="zhiheng", version="1.0.0-rc1", stage=10, release_status="competition-candidate", workspace=str(WORKSPACE), pid=os.getpid())

    @application.get("/api/model")
    def model_config():
        return answers.config.public()

    @application.patch("/api/model")
    def update_model(body: ModelsInput):
        try:
            return answers.config.update(body.models)
        except ModelError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.post("/api/model/test")
    def test_model():
        try:
            gateway = answers.gateway_factory(answers.config.load(), lambda: False)
            result = gateway([{"role": "user", "content": "仅返回JSON对象 {\"ok\":true}"}], "verify")
            ok = result.get("ok") is True
            return dict(ok=ok, message="复核模型连接正常" if ok else "服务已响应，但返回格式不符合约定", model=gateway.models.get("verify", ""))
        except ModelError as exc:
            return dict(ok=False, message=str(exc), model="")

    @application.post("/api/projects/{project_id}/answers", status_code=202)
    def create_answer(project_id: str, body: AnswerInput):
        project_or_404(project_id)
        try:
            item = answers.submit(project_id, body.question, body.source_id, body.mode, body.table_name, str(body.request_id), body.parent_answer_id, body.source_ids, body.context_patch, body.expected_context_revision)
            return answers.public(item)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @application.get("/api/projects/{project_id}/answers")
    def list_answers(project_id: str):
        project_or_404(project_id)
        return [answers.public(item) for item in answers.store.listing(project_id)]

    @application.get("/api/projects/{project_id}/answer-index")
    def answer_index(project_id: str, before: int | None = Query(default=None,ge=1),
                     limit: int = Query(default=50,ge=1,le=100),
                     conversation_id: str | None = Query(default=None,max_length=80),
                     search: str = Query(default='',max_length=200)):
        project_or_404(project_id)
        return answers.store.index(project_id,before,limit,conversation_id,search)

    @application.get("/api/projects/{project_id}/answers/{answer_id}")
    def get_answer(project_id: str, answer_id: str):
        project_or_404(project_id)
        item = answers.store.get(project_id, answer_id)
        if not item:
            raise HTTPException(404, "回答不存在或不属于当前项目")
        return answers.public(item)

    @application.get("/api/projects/{project_id}/answers/{answer_id}/conversation")
    def answer_conversation(project_id: str, answer_id: str):
        if not answers.store.get(project_id, answer_id):
            raise HTTPException(404, "回答不存在")
        try:
            return [answers.public(item) for item in answers.store.conversation(project_id, answer_id)]
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @application.post("/api/projects/{project_id}/answers/{answer_id}/cancel")
    def cancel_answer(project_id: str, answer_id: str):
        project_or_404(project_id)
        if not answers.store.get(project_id, answer_id):
            raise HTTPException(404, "回答不存在或不属于当前项目")
        return answers.public(answers.cancel(project_id, answer_id))

    @application.get("/api/projects/{project_id}/answers/{answer_id}/export")
    def answer_export(project_id: str, answer_id: str, format: str = "markdown", scope: str = "answer"):
        try:
            exported = export_answer(service.store.path, project_id, answer_id, format, scope)
        except ExportError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        return Response(exported.content, media_type=exported.media_type, headers=exported.headers)

    @application.get("/api/projects")
    def list_projects():
        return service.store.projects()

    @application.post("/api/projects", status_code=201)
    def create_project(body: ProjectInput):
        return service.store.create_project(body.name, body.description or "")

    @application.patch("/api/projects/{project_id}")
    def update_project(project_id: str, body: ProjectInput):
        project_or_404(project_id)
        return service.store.update_project(project_id, body.name, body.description)

    @application.get("/api/projects/{project_id}/overview")
    def overview(project_id: str):
        project = project_or_404(project_id)
        sources = service.store.sources(project_id)
        stats = dict(sources=len(sources), ready=sum(s["status"] == "ready" for s in sources), attention=sum(s["status"] == "attention" for s in sources), failed=sum(s["status"] == "failed" for s in sources), processing=sum(s["status"] in {"queued", "processing"} for s in sources), documents=sum(s["kind"] in {"document", "image"} for s in sources), tables=sum(s["table_count"] for s in sources), rows=sum(s["row_count"] for s in sources))
        return dict(project=project, stats=stats, recent_sources=[public_source(s) for s in sources[:8]], jobs=service.store.jobs(project_id)[:10], drafts=service.store.drafts(project_id)[:20])

    @application.get("/api/projects/{project_id}/sources")
    def sources(project_id: str, archived: bool = False):
        project_or_404(project_id)
        return [public_source(source) for source in service.store.sources(project_id, archived)]

    @application.post("/api/projects/{project_id}/import")
    async def import_files(project_id: str, files: list[UploadFile] = File(...)):
        project_or_404(project_id)
        if not files or len(files) > 20:
            raise HTTPException(422, "每批请选择 1 至 20 个文件")
        staged, incomplete = [], None
        try:
            for upload in files:
                name = filename(upload.filename)
                target = service.root / "incoming" / identity()
                incomplete = target
                digest, size = hashlib.sha256(), 0
                with target.open("wb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > parsers.MAX_BYTES:
                            raise HTTPException(413, f"「{name}」超过 80 MB 上限，请拆分后导入")
                        output.write(chunk)
                        digest.update(chunk)
                staged.append(dict(path=target, name=name, format=file_format(name), bytes=size, sha256=digest.hexdigest(), metadata={"import_method": "upload"}))
                incomplete = None
            return service.import_staged(project_id, staged)
        finally:
            if incomplete:
                incomplete.unlink(missing_ok=True)
            for item in staged:
                item["path"].unlink(missing_ok=True)
            for upload in files:
                await upload.close()

    @application.post("/api/projects/{project_id}/demo")
    def import_demo(project_id: str):
        project_or_404(project_id)
        try:
            return service.demo(project_id)
        except (ValueError, KeyError, OSError) as exc:
            raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "示例资料清单不完整，请检查 samples/manifest.json") from exc

    @application.get("/api/sources/{source_id}")
    def source_detail(source_id: str):
        source = source_or_404(source_id)
        return dict(source=public_source(source), pages=source["pages"], tables=source["tables"], metadata=source["metadata"])

    @application.get("/api/sources/{source_id}/tables/{table_name:path}")
    def get_table(source_id: str, table_name: str, offset: int = Query(0, ge=0, le=10_000_000), limit: int = Query(50, ge=1, le=200)):
        source = source_or_404(source_id)
        if source["status"] not in {"ready", "attention"} or not source["data_path"]:
            raise HTTPException(409, "资料尚无可用数据表")
        try:
            return parsers.table_page(source["data_path"], table_name, {t["name"]: t["columns"] for t in source["tables"]}, offset, limit)
        except KeyError as exc:
            raise HTTPException(404, "该资料中不存在此表") from exc
        except sqlite3.DatabaseError as exc:
            raise HTTPException(422, "表格读取失败或查询超时，请检查原件并重试导入") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.get("/api/sources/{source_id}/original")
    def original(source_id: str, download: bool = True):
        source = source_or_404(source_id)
        # Unsupported/executable content is retained for audit, never served for download.
        if source["format"] not in parsers.SUPPORTED:
            raise HTTPException(415, "此格式不受支持，原件已保留在本机但不提供下载或执行")
        path = Path(source["original_path"])
        if not path.is_file():
            raise HTTPException(404, "原件缺失，请重新导入")
        media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "txt": "text/plain", "md": "text/plain", "csv": "text/csv", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(source["format"], "application/octet-stream")
        return FileResponse(path, media_type=media, filename=source["name"], content_disposition_type="attachment" if download else "inline", headers={"Content-Security-Policy": "sandbox"})

    @application.get("/api/sources/{source_id}/pages/{number}/image")
    def page_image(source_id: str, number: int):
        source = source_or_404(source_id)
        if number < 1:
            raise HTTPException(404, "页面不存在")
        if source["format"] not in {"pdf", "png", "jpg", "jpeg"}:
            raise HTTPException(415, "该格式不提供图片预览，可查看文字或表格")
        if source["status"] == "failed":
            raise HTTPException(409, "文件解析失败，请先查看错误信息")
        target = service.root / "previews" / f"{source_id}-{number}.png"
        temporary = target.with_name(f"{source_id}-{identity()}.png")
        if not target.exists():
            try:
                parsers.render_preview(Path(source["original_path"]), source["format"], number, temporary)
                temporary.replace(target)
            except IndexError as exc:
                raise HTTPException(404, "页面不存在") from exc
            except Exception as exc:
                raise HTTPException(422, "无法生成此页预览，请下载原件查看") from exc
            finally:
                temporary.unlink(missing_ok=True)
        return FileResponse(target, media_type="image/png")

    @application.post("/api/sources/{source_id}/retry")
    def retry(source_id: str):
        source_or_404(source_id)
        try:
            source, job = service.store.retry(source_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        answers.invalidate_source(source)
        service.schedule(job["id"])
        return dict(source=public_source(source), job=job)

    @application.get("/api/projects/{project_id}/answers/{answer_id}/events")
    def answer_event_history(project_id: str, answer_id: str,
                             after: int = Query(default=0, ge=0),
                             limit: int = Query(default=100, ge=1, le=200)):
        project_or_404(project_id)
        if not answers.store.get(project_id, answer_id):
            raise HTTPException(404, "回答不存在")
        items = answers.store.events(project_id, answer_id, after, limit)
        return dict(items=items, next_after=items[-1]['sequence'] if items else after,
                    has_more=bool(items and answers.store.events(project_id,answer_id,items[-1]['sequence'],1)))

    @application.post("/api/sources/{source_id}/archive")
    def archive(source_id: str):
        source=source_or_404(source_id)
        service.store.archive(source_id, True)
        answers.invalidate_source(source)
        return dict(ok=True)

    @application.patch("/api/sources/{source_id}/review")
    def review_source(source_id: str, body: ReviewInput):
        source_or_404(source_id)
        try:
            return public_source(service.store.review(source_id, body.status, body.note))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @application.post("/api/sources/{source_id}/restore")
    def restore(source_id: str):
        source_or_404(source_id)
        try:
            service.store.archive(source_id, False)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "项目中已有相同原件，无法重复恢复；请先归档重复项") from exc
        return dict(ok=True)

    @application.get('/api/sources/{source_id}/semantics')
    def source_semantics(source_id: str):
        source=source_or_404(source_id)
        return answers.semantics.get(source['project_id'],source_id)

    @application.patch('/api/sources/{source_id}/semantics')
    def update_source_semantics(source_id: str,body: SemanticPatchInput):
        source=source_or_404(source_id)
        try:
            return answers.semantics.update(source['project_id'],source_id,body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @application.get('/api/sources/{source_id}/semantics/events')
    def source_semantic_events(source_id: str,after: int = Query(default=0,ge=0),limit: int = Query(default=100,ge=1,le=100)):
        source=source_or_404(source_id)
        return answers.semantics.events(source['project_id'],source_id,after,limit)

    @application.get("/api/projects/{project_id}/jobs")
    def jobs(project_id: str):
        project_or_404(project_id)
        return service.store.jobs(project_id)

    @application.get("/api/projects/{project_id}/drafts")
    def drafts(project_id: str, archived: bool = False):
        project_or_404(project_id)
        return service.store.drafts(project_id, archived)

    @application.post("/api/projects/{project_id}/drafts", status_code=201)
    def save_draft(project_id: str, body: DraftInput):
        project_or_404(project_id)
        try:
            return service.store.save_draft(project_id, body.question, body.source_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.patch("/api/projects/{project_id}/drafts/{draft_id}")
    def update_draft(project_id: str, draft_id: str, body: DraftInput):
        draft = draft_or_404(project_id, draft_id)
        if draft["archived"]:
            raise HTTPException(409, "请先恢复已归档问题")
        try:
            return service.store.update_draft(project_id, draft_id, body.question, body.source_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.post("/api/projects/{project_id}/drafts/{draft_id}/archive")
    def archive_draft(project_id: str, draft_id: str):
        draft_or_404(project_id, draft_id)
        service.store.archive_draft(project_id, draft_id, True)
        return dict(ok=True)

    @application.post("/api/projects/{project_id}/drafts/{draft_id}/restore")
    def restore_draft(project_id: str, draft_id: str):
        draft_or_404(project_id, draft_id)
        service.store.archive_draft(project_id, draft_id, False)
        return dict(ok=True)

    @application.get("/{resource:path}")
    def frontend(resource: str):
        if resource.startswith("api/") or resource == "api":
            raise HTTPException(404, "接口不存在")
        dist = (WORKSPACE / "frontend" / "dist").resolve()
        target = (dist / resource).resolve()
        if not target.is_relative_to(dist):
            raise HTTPException(404, "文件不存在")
        if target.is_file():
            return FileResponse(target)
        if resource and Path(resource).suffix:
            raise HTTPException(404, "文件不存在")
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse({"detail": "前端尚未构建，请在 frontend 运行构建命令后访问"}, status_code=503)

    return application


app = create_app()
