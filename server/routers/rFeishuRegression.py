# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书用例回归 HTTP API。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from server.core.database import get_db
from server.models.project import App
from server.services import feishu_regression_service as frs
from server.services.system_settings_service import list_feishu_bots
from server.services.feishu_service import parse_feishu_sheet_url
from script.log import SLog

router = APIRouter(prefix="/feishu", tags=["Feishu Regression"])

TAG = "FeishuRegressionRouter"


class FeishuConfigUpdate(BaseModel):
    doc_url: str = ""
    spreadsheet_token: str = ""
    sheet_id: str = ""
    data_range: str = "A1:O500"
    enabled: bool = True
    bot_id: str = ""
    env_profile: str = "test"


class FeishuRunRequest(BaseModel):
    app_id: str
    sn: str
    platform: str = "android"
    case_ids: Optional[List[str]] = None
    start_index: int = 0


class FeishuClarifyRequest(BaseModel):
    option_id: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    note: str = ""
    name: str = ""
    aliases: Optional[List[str]] = None


def _get_app(db: Session, app_id: str) -> App:
    app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app


@router.get("/bots")
def list_available_feishu_bots():
    bots = list_feishu_bots()
    return {"code": 200, "data": {"bots": bots}}


@router.get("/credentials/status")
def feishu_credentials_status():
    bots = list_feishu_bots()
    configured = any(b.get("configured") for b in bots)
    return {
        "code": 200,
        "data": {
            "configured": configured,
            "bot_count": len(bots),
            "bots": bots,
        },
    }


@router.get("/config/{app_id}")
def get_feishu_config(app_id: str, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    cfg = frs._get_app_feishu_config(app)
    return {
        "code": 200,
        "data": {
            "app_id": app.id,
            "app_name": app.name,
            "project_name": app.project.name if app.project else "",
            "feishu": cfg,
        },
    }


@router.put("/config/{app_id}")
def update_feishu_config(app_id: str, body: FeishuConfigUpdate, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    cfg = frs.save_app_feishu_config(app, body.model_dump())
    db.commit()
    db.refresh(app)
    return {"code": 200, "msg": "飞书配置已保存", "data": {"feishu": cfg}}


@router.post("/fetch/{app_id}")
def fetch_feishu_cases(app_id: str, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    try:
        data = frs.fetch_cases_for_app(app, persist=True)
        db.commit()
        return {"code": 200, "data": data}
    except Exception as e:
        SLog.e("FeishuRegression", f"fetch failed app={app_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/cases/{app_id}")
def get_feishu_cases_cached(app_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    try:
        if refresh:
            data = frs.fetch_cases_for_app(app, persist=True)
            db.commit()
        else:
            data = frs.list_cases_for_app(app, refresh=False)
        return {"code": 200, "data": data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/parse-url")
def parse_url(body: Dict[str, Any]):
    url = (body.get("doc_url") or body.get("url") or "").strip()
    parsed = parse_feishu_sheet_url(url)
    return {"code": 200, "data": parsed}


@router.post("/run")
def run_feishu_regression(body: FeishuRunRequest, db: Session = Depends(get_db)):
    app = _get_app(db, body.app_id)
    if not body.sn:
        raise HTTPException(status_code=400, detail="请选择执行设备")
    try:
        run_doc = frs.run_cases(
            app,
            sn=body.sn,
            platform=(body.platform or "android").lower(),
            case_ids=body.case_ids,
            start_index=body.start_index or 0,
            db=db,
        )
        db.commit()
        return {"code": 200, "data": run_doc}
    except Exception as e:
        SLog.e(TAG, f"run failed app={body.app_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/run/{run_id}")
def get_feishu_run(run_id: str, db: Session = Depends(get_db)):
    doc = frs.get_run(run_id, db=db)
    if not doc:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"code": 200, "data": doc}


@router.post("/run/{run_id}/clarify")
def clarify_feishu_run(run_id: str, body: FeishuClarifyRequest, db: Session = Depends(get_db)):
    try:
        run_doc = frs.clarify_and_resume_run(run_id, body.model_dump(), db)
        db.commit()
        return {"code": 200, "msg": "已确认并继续执行", "data": run_doc}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        SLog.e(TAG, f"clarify failed run={run_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/runs/{app_id}")
def list_feishu_runs(app_id: str, limit: int = 30, db: Session = Depends(get_db)):
    from server.services import app_automation_service as aas

    _get_app(db, app_id)
    return {"code": 200, "data": {"runs": aas.list_runs_for_app(db, app_id, limit=limit)}}
