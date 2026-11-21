"""质量报告兼容路由：v2 快照迁移、跨版本回查、签收和事件回查。

固定路径必须注册在资源路由 `/api/{module}` 之前，否则会被动态模块名抢先匹配；
这里单独一个 router，并在 `app/routers/__init__.py` 里排在 resource 前面。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ReportImportPayload, ReportMigrationPayload, ReportRecomputePayload, ReportSignPayload
from app.services.report_snapshot import METRIC_NAMES, report_compat


router = APIRouter(prefix="/api/report", tags=["质量报告兼容"])


@router.get("/capabilities", response_model=dict)
def capabilities() -> dict[str, Any]:
    return report_compat.capabilities()


@router.post("/migrate", response_model=dict)
def migrate(payload: ReportMigrationPayload) -> dict[str, Any]:
    return report_compat.start_migration(
        payload.report_numbers,
        stop_after=payload.stop_after,
        timepoint=payload.timepoint,
    )


@router.get("/migrations", response_model=dict)
def migrations() -> dict[str, Any]:
    items = report_compat.list_migrations()
    return {"items": items, "total": len(items)}


@router.post("/migrations/{migration_id}/resume", response_model=dict)
def resume_migration(migration_id: str) -> dict[str, Any]:
    detail, message = report_compat.resume_migration(migration_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "migration": detail}


@router.post("/migrations/{migration_id}/rollback", response_model=dict)
def rollback_migration(migration_id: str) -> dict[str, Any]:
    detail, message = report_compat.rollback_migration(migration_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "migration": detail}


@router.get("/snapshots", response_model=dict)
def snapshots(
    report_no: str | None = Query(default=None),
    day: str | None = Query(default=None),
    structure_version: str | None = Query(default=None),
    sign_status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    items, total = report_compat.list_snapshots(
        report_no=report_no,
        day=day,
        structure_version=structure_version,
        sign_status=sign_status,
        page=page,
        size=size,
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def snapshot_detail(snapshot_id: int) -> dict[str, Any]:
    snapshot = report_compat.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"报告快照 {snapshot_id} 不存在或已回滚")
    return snapshot


@router.post("/snapshots/{snapshot_id}/recompute", response_model=dict)
def recompute_snapshot(snapshot_id: int, payload: ReportRecomputePayload) -> dict[str, Any]:
    snapshot, message = report_compat.recompute_snapshot(
        snapshot_id, payload.metrics, reviewer=payload.reviewer)
    if snapshot is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "snapshot": snapshot, "指标": METRIC_NAMES}


@router.post("/snapshots/{snapshot_id}/sign", response_model=dict)
def sign_snapshot(snapshot_id: int, payload: ReportSignPayload) -> dict[str, Any]:
    snapshot, message = report_compat.sign_snapshot(
        snapshot_id, result=payload.result, reviewer=payload.reviewer, remark=payload.remark)
    if snapshot is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "snapshot": snapshot}


@router.get("/numbers/{report_no}", response_model=dict)
def number_versions(report_no: str) -> dict[str, Any]:
    result = report_compat.versions_for_number(report_no)
    if not result["历史"]:
        raise HTTPException(status_code=404, detail=f"报告编号 {report_no} 无任何版本")
    return result


@router.get("/numbers/{report_no}/effective", response_model=dict)
def number_effective(report_no: str) -> dict[str, Any]:
    result = report_compat.effective_report(report_no)
    if not result.get("可取得"):
        raise HTTPException(status_code=404, detail=result.get("说明", "报告不存在"))
    return result


@router.get("/incident-review", response_model=dict)
def incident_review(
    incident_no: str | None = Query(default=None),
    report_no: str | None = Query(default=None),
    result: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    items, total = report_compat.review_items(
        incident_no=incident_no, report_no=report_no, result=result, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/incidents/{incident_id}/evidence", response_model=dict)
def incident_evidence(incident_id: int) -> dict[str, Any]:
    return report_compat.incident_evidence(incident_id)


@router.get("/daily/{day}", response_model=dict)
def daily(day: str) -> dict[str, Any]:
    return report_compat.daily_view(day)


@router.get("/fixtures/v1-export")
def v1_export_fixture() -> Any:
    """返回一份已经导出的 v1 日报 CSV，供跨版本读取核对。

    同时补齐该导出当年依赖的评分卡/巡检/趋势来源行（幂等），
    这样导入后可以看到 v2 快照把哪些指标补全、哪些仍是缺项。
    """
    from fastapi.responses import Response

    from app.seed import V1_EXPORT_CSV

    report_compat.ensure_fixture_sources()
    return Response(
        content=V1_EXPORT_CSV,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dq_report_v1_export.csv"'},
    )


@router.post("/fixtures/seed-sources", response_model=dict)
def seed_fixture_sources() -> dict[str, Any]:
    inserted = report_compat.ensure_fixture_sources()
    return {"ok": True, "message": f"已补齐 {inserted} 条示例来源记录", "inserted": inserted}


@router.post("/import/v1", response_model=dict)
def import_v1(payload: ReportImportPayload) -> dict[str, Any]:
    try:
        return report_compat.import_v1_export(payload.csv_text, stop_after=payload.stop_after)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
