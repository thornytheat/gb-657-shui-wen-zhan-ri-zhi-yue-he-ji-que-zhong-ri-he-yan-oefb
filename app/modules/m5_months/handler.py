"""M5 入口：从当前批次区间重算月度分摊与账单，并给出跨站复核结论。

跨站复核的入口共用同一份归档：
- `GET  /months`：月报页面，展示生效结论与作为依据的共同实测日；
- `POST /api/cross-review`（别名 `POST /api/hydro/cross-review`）：提交一批日值
  （records）或重读样例行，比对源摘要后归档、失效旧结论并按新数据重算；
- `GET  /api/cross-review`：只读当前生效结论，不写库；
- `GET  /api/cross-review/history`：查看失效与重算轨迹；
- `GET  /api/cross-review/evidence`：按两站与月份抽查共同实测日。
"""
from __future__ import annotations

import calendar

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, now_local, render
from ..m6_batches import resume as resume_mod
from . import cross_review, daily_source, review_service, service
from .hydro import monthly, reconcile, select_daily

router = APIRouter(tags=["M5 月度切分"])

MONTH_STATE_LABELS = {"complete": "完整", "partial": "不完整"}


def _stamp() -> str:
    return now_local().isoformat()


def _expected_days(stations_months: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for month in stations_months:
        year, mon = int(month[:4]), int(month[5:7])
        out[month] = calendar.monthrange(year, mon)[1]
    return out


def _page_review(ctx: Ctx) -> tuple[dict, dict]:
    """页面用：给出当前日值源的复核结论，必要时归档/失效重算。

    - 文件源（origin=file）每次重读文件，文件一改即视为源更新；
    - 接口提交源（origin=records）以库里快照为准，不退回样例文件；
    - 已有生效结论且摘要一致 → 直接展示，不写库；
    - 无结论或摘要已变 → 归档新结论，旧结论标 invalidated 后重算。
    """
    source = ctx.store.load_daily_source(review_service.DEFAULT_SCOPE)
    if source is None or source.get("origin") == "file":
        records = daily_source.load_sample(ctx.settings)
        origin = "file"
    else:
        records = source["records"]
        origin = "records"

    active = review_service.current(store=ctx.store,
                                    scope=review_service.DEFAULT_SCOPE)
    draft = review_service.evaluate(
        store=ctx.store, scope=review_service.DEFAULT_SCOPE,
        records=records, stamp=_stamp(), persist=False, origin=origin)
    current = active is not None and active["source_digest"] == draft["source_digest"]
    if not current:
        draft = review_service.evaluate(
            store=ctx.store, scope=review_service.DEFAULT_SCOPE,
            records=records, stamp=_stamp(), persist=True, origin=origin)
        active = review_service.current(store=ctx.store,
                                        scope=review_service.DEFAULT_SCOPE)

    snapshot = ctx.store.peek_daily_source(review_service.DEFAULT_SCOPE)
    state = {
        "archived": active is not None,
        "recomputed": not current,
        "active_review_id": active["review_id"] if active else None,
        "active_created_at": active["created_at"] if active else None,
        "source_updated_at": snapshot.get("updated_at") if snapshot else None,
        "source_origin": snapshot.get("origin") if snapshot else None,
        "invalidated_ids": draft.get("invalidated_ids", []),
    }
    return draft, state


@router.post("/api/hydro/monthly")
def api_hydro(payload: dict) -> dict:
    raw = payload.get("records") or []
    selected = select_daily(raw)
    result = monthly(selected["selected"], payload.get("expected_days") or {})
    accounts = reconcile(len(raw), len(selected["selected"]),
                         len(selected["duplicates"]), len(selected["conflicts"]))
    body = {"months": result, "selection": selected, "accounts": accounts}
    if raw:
        # 纯计算预览：不归档；归档走 POST /api/cross-review。
        body["cross_review"] = cross_review.review(raw, selection=selected)
    return body


def _submit_cross_review(payload: dict | None, ctx: Ctx) -> dict:
    body = payload or {}
    scope = str(body.get("scope") or review_service.DEFAULT_SCOPE)
    if "records" in body:
        # 显式传了 records 就按它算，哪怕空列表；空批直接 400，不悄悄回退样例
        records = daily_source.normalize_records(body["records"] or [])
        origin = "records"
    else:
        records = daily_source.load_sample(ctx.settings)
        origin = "file"
    result = review_service.evaluate(store=ctx.store, scope=scope, records=records,
                                     stamp=_stamp(), persist=True, origin=origin)
    return result


@router.post("/api/cross-review")
def api_cross_review(payload: dict | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    return _submit_cross_review(payload, ctx)


@router.post("/api/hydro/cross-review")
def api_hydro_cross_review(payload: dict | None = None,
                           ctx: Ctx = Depends(get_ctx)) -> dict:
    return _submit_cross_review(payload, ctx)


@router.get("/api/cross-review")
def api_cross_review_current(scope: str = review_service.DEFAULT_SCOPE,
                             ctx: Ctx = Depends(get_ctx)) -> dict:
    active = review_service.current(store=ctx.store, scope=scope)
    if active is None:
        return {"scope": scope, "exists": False, "review": None}
    snapshot = ctx.store.peek_daily_source(scope)
    payload = {**active["payload"], "review_id": active["review_id"],
               "archived_at": active["created_at"]}
    return {
        "scope": scope, "exists": True, "status": active["status"],
        "review_id": active["review_id"], "created_at": active["created_at"],
        "source_digest": active["source_digest"],
        "source_updated": bool(
            snapshot and snapshot["source_digest"] != active["source_digest"]),
        "review": payload,
    }


@router.get("/api/hydro/cross-review")
def api_hydro_cross_review_current(scope: str = review_service.DEFAULT_SCOPE,
                                   ctx: Ctx = Depends(get_ctx)) -> dict:
    return api_cross_review_current(scope=scope, ctx=ctx)


@router.get("/api/cross-review/history")
def api_cross_review_history(scope: str = review_service.DEFAULT_SCOPE,
                             limit: int = 50,
                             ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = review_service.history(store=ctx.store, scope=scope, limit=limit)
    return {"scope": scope, "count": len(rows), "rows": [{
        "review_id": item["review_id"], "status": item["status"],
        "source_digest": item["source_digest"],
        "rule_version": item["rule_version"],
        "created_at": item["created_at"],
        "invalidated_at": item["invalidated_at"],
        "invalidated_by": item["invalidated_by"],
        "summary": item["payload"].get("summary", {}),
    } for item in rows]}


@router.get("/api/cross-review/evidence")
def api_cross_review_evidence(station_a: str, station_b: str,
                              month: str | None = None,
                              scope: str = review_service.DEFAULT_SCOPE,
                              ctx: Ctx = Depends(get_ctx)) -> dict:
    active = review_service.current(store=ctx.store, scope=scope)
    if active is None:
        return {"exists": False, "pairs": []}
    pairs = [item for item in active["payload"].get("pairs", [])
             if item["station_a"] == station_a and item["station_b"] == station_b
             and (month is None or item["month"] == month)]
    return {"exists": True, "review_id": active["review_id"],
            "scope": scope, "pairs": pairs}


@router.post("/api/months/allocate")
def api_allocate(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch_id = (payload or {}).get("batch_id")
    batch = resume_mod.require_batch(
        ctx.store, batch_id or (ctx.current_batch() or {}).get("batch_id", ""))
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    body = service.payload(result["allocations"], result["bills"],
                           service.primary_map(ctx.settings))
    body["batch_id"] = result["batch_id"]
    body["stats"] = {key: str(value) if hasattr(value, "as_tuple") else value
                     for key, value in result["stats"].items()}
    return body


@router.get("/months")
def page_months(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    groups: list[dict] = []
    bills: list[dict] = []
    adjustments = 0
    if batch is not None:
        allocations = ctx.store.load_allocations(batch["batch_id"])
        bills = ctx.store.load_bills(batch["batch_id"])
        groups = service.rows_by_month(allocations, service.primary_map(ctx.settings))
        adjustments = sum(1 for item in allocations if item["tou_adjusted"])

    review, review_state = _page_review(ctx)
    selected = review["selection"]
    expected = _expected_days({str(row["date"])[:7] for row in selected["selected"]})
    hydro_months = monthly(selected["selected"], expected)
    for row in hydro_months:
        row["state_label"] = MONTH_STATE_LABELS.get(row["state"], row["state"])
    accounts = reconcile(len(selected["selected"]) + len(selected["duplicates"])
                         + len(selected["conflicts"]),
                         len(selected["selected"]), len(selected["duplicates"]),
                         len(selected["conflicts"]))
    return render(request, "months.html", page="months", page_title="月合计",
                  batch=batch, groups=groups, bills=bills, adjustments=adjustments,
                  hydro_months=hydro_months, hydro_selected=selected,
                  hydro_accounts=accounts, review=review,
                  review_state=review_state,
                  review_history=review_service.history(
                      store=ctx.store, scope=review_service.DEFAULT_SCOPE, limit=6))
