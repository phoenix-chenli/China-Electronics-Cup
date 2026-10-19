"""Local product actions over versioned answers and immutable exports."""
from __future__ import annotations
from fastapi import HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from .brief_service import BriefService
from .product_store import ProductStore
from .scenario_service import ScenarioService
from .suggestion_service import SuggestionService
from .source_snapshot import snapshot, scope_matches
from .export_service import ExportError


class StrictInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
class FavoriteInput(StrictInput):
    enabled:bool=Field(strict=True)
class WorkspaceInput(StrictInput):
    expected_revision:int=Field(ge=0,strict=True)
    state:dict
class BriefInput(StrictInput):
    title:str=Field(min_length=1,max_length=120)
    answer_ids:list[str]=Field(min_length=1,max_length=20)
class ScenarioInput(StrictInput):
    calculation_id:str=Field(min_length=1,max_length=80)
    overrides:dict
    expected_context_revision:str=Field(min_length=64,max_length=64)
class SuggestInput(StrictInput):
    branch_id:str|None=Field(default=None,min_length=1,max_length=80)
    force:bool=Field(default=False,strict=True)
class SuggestAcceptInput(StrictInput):
    suggestion_id:str=Field(min_length=1,max_length=80)
    expected_scope:dict
class SuggestDismissInput(StrictInput):
    expected_scope:dict|None=None


class ProductServices:
    def __init__(self,service,answers):
        self.service=service;self.answers=answers
        self.product=ProductStore(service.store);self.briefs=BriefService(service.store);self.scenarios=ScenarioService(service.store)
        self.suggestions=SuggestionService(service.store,is_current=self.scope_current)
    def start(self):
        self.product.initialize();self.briefs.initialize();self.scenarios.initialize();self.suggestions.start()
    def close(self):self.suggestions.close()
    def scope_current(self,scope):
        if not isinstance(scope,dict):return False
        item=self.answers.store.get(scope.get('project_id'),scope.get('answer_id'))
        if not item or item.get('context_revision')!=scope.get('context_revision'):return False
        current=[]
        for old in item.get('sources',[]):
            source=self.service.store.get('sources',old['id'])
            if not source or source['project_id']!=item['project_id'] or source['archived'] or source['status'] not in ('ready','attention'):return False
            current.append(snapshot(self.answers.semantics.inject(source)))
        return scope_matches(scope.get('source_revisions',[]),current) and scope_matches(item.get('sources',[]),current)
    def suggest(self,project_id,answer_id,branch_id=None,force=False):
        item=self.answers.store.get(project_id,answer_id)
        if not item:raise LookupError('回答不存在或不属于当前项目')
        if branch_id and not self.answers.store.get(project_id,branch_id):raise ValueError('分支不属于当前项目')
        capabilities={'local_actions':['explain_scope','verify_sources','visualize_result','scenario_setup','add_sources'],'catalogs':[]}
        from .schema_catalog import build_catalog
        for old in item.get('sources',[]):
            source=self.service.store.get('sources',old['id'])
            if source and source.get('tables'):
                source=self.answers.semantics.inject(source)
                capabilities['catalogs'].append(dict(build_catalog(source),source_snapshot=snapshot(source)))
        questions=[h['question'] for h in self.answers.store.conversation(project_id,answer_id)]
        return self.suggestions.submit(item,branch_id,capabilities,questions,force=force)


def register_product_routes(app,hub):
    def project(pid):
        if not hub.service.store.get('projects',pid):raise HTTPException(404,'项目不存在')
    def call(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except ExportError as exc:raise HTTPException(exc.status_code,exc.detail) from None
        except LookupError as exc:raise HTTPException(404,str(exc)) from None
        except (ValueError,TypeError) as exc:raise HTTPException(409,str(exc) if isinstance(exc,ValueError) else '输入格式无效') from None

    @app.get('/api/diagnostics')
    def diagnostics():
        from .reliability import runtime_diagnostics
        return runtime_diagnostics(hub.service,hub.answers)
    @app.get('/api/sources/{sid}/diagnostics')
    def source_diagnostics(sid:str):
        from .reliability import source_diagnostics as diagnose
        source=hub.service.store.get('sources',sid)
        if not source:raise HTTPException(404,'资料不存在')
        return diagnose(source)
    @app.get('/api/projects/{pid}/answers/{aid}/source-versions')
    def answer_source_versions(pid:str,aid:str):
        from .source_versions import compare_answer_sources
        project(pid)
        return call(compare_answer_sources,hub.answers.store.get(pid,aid),pid,
                    hub.service.store,hub.answers.semantics)
    @app.post('/api/projects/{pid}/sources/{sid}/cancel')
    def cancel_import(pid:str,sid:str):
        from .reliability import cancel_import as cancel
        project(pid);return {'cancelled':call(cancel,hub.service,sid,pid)}
    @app.post('/api/demo-workspace',status_code=201)
    def demo_workspace():
        from .demo_workspace import load_demo
        try:return call(load_demo,hub.service)
        except OSError:raise HTTPException(409,'演示资料未完整安装，请检查随包样本') from None

    @app.get('/api/projects/{pid}/favorites')
    def favorites(pid:str):project(pid);return hub.product.favorites(pid)
    @app.put('/api/projects/{pid}/answers/{aid}/favorite')
    def favorite(pid:str,aid:str,body:FavoriteInput):project(pid);return call(hub.product.favorite,pid,aid,body.enabled)
    @app.get('/api/projects/{pid}/workspace/{client_id}')
    def workspace(pid:str,client_id:str):project(pid);return call(hub.product.workspace,pid,client_id)
    @app.put('/api/projects/{pid}/workspace/{client_id}')
    def save_workspace(pid:str,client_id:str,body:WorkspaceInput):project(pid);return call(hub.product.save_workspace,pid,client_id,body.state,body.expected_revision)
    @app.post('/api/projects/{pid}/briefs',status_code=201)
    def create_brief(pid:str,body:BriefInput):project(pid);return call(hub.briefs.create,pid,body.title,body.answer_ids)
    @app.get('/api/projects/{pid}/briefs')
    def briefs(pid:str):project(pid);return hub.briefs.listing(pid)
    @app.get('/api/projects/{pid}/briefs/{bid}')
    def brief(pid:str,bid:str,format:str='html'):
        project(pid);data,media=call(hub.briefs.get,pid,bid,format)
        return Response(data,media_type=media,headers={'Content-Disposition':f'attachment; filename="analysis-brief.{"json" if format=="json" else "html"}"'})
    @app.post('/api/projects/{pid}/answers/{aid}/scenarios',status_code=201)
    def scenario(pid:str,aid:str,body:ScenarioInput):
        project(pid);return call(hub.scenarios.create,pid,aid,body.calculation_id,body.overrides,body.expected_context_revision)
    @app.get('/api/projects/{pid}/scenarios')
    def scenarios(pid:str,answer_id:str|None=None):project(pid);return hub.scenarios.listing(pid,answer_id)
    @app.post('/api/projects/{pid}/answers/{aid}/suggestions',status_code=202)
    def suggestions(pid:str,aid:str,body:SuggestInput):project(pid);return call(hub.suggest,pid,aid,body.branch_id,body.force)
    @app.get('/api/projects/{pid}/suggestions/{key}')
    def suggestion(pid:str,key:str):
        project(pid);record=call(hub.suggestions.get,pid,key)
        if record is None:raise HTTPException(404,'建议不存在或不属于当前项目')
        return record
    @app.post('/api/projects/{pid}/suggestions/{key}/accept')
    def accept(pid:str,key:str,body:SuggestAcceptInput):project(pid);return call(hub.suggestions.accept,pid,key,body.suggestion_id,body.expected_scope)
    @app.post('/api/projects/{pid}/suggestions/{key}/dismiss')
    def dismiss(pid:str,key:str,body:SuggestDismissInput):project(pid);return call(hub.suggestions.dismiss,pid,key,body.expected_scope)
    @app.get('/api/projects/{pid}/suggestion-preference')
    def preference(pid:str):project(pid);return hub.suggestions.preference(pid)
    @app.put('/api/projects/{pid}/suggestion-preference')
    def set_preference(pid:str,body:FavoriteInput):project(pid);return call(hub.suggestions.set_enabled,pid,body.enabled)
