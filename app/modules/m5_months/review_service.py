"""跨站复核编排：日值源快照 → 复核 → 归档结论（幂等、失效重算）。

归档键是 (scope, 日值源摘要)：
- 同一批日值重跑，摘要相同 → 命中已有结论，不新增记录；
- 日值源更新后摘要变化 → 旧结论标 `invalidated`，按新数据显示重算；
- `review_id` 由 scope + 摘要派生，重放不会产生第二条同源记录。
"""
from __future__ import annotations

from ... import config, manifest
from . import cross_review

DEFAULT_SCOPE = "months"


def review_id_for(scope: str, source_digest: str) -> str:
    return "cr-" + manifest.digest_parts(
        [config.CROSS_REVIEW_RULE_VERSION, scope, source_digest])[:16]


def evaluate(*, store, scope: str, records: list[dict], stamp: str,
             persist: bool = True, origin: str = "records") -> dict:
    """跑一次复核并按需归档；返回当前结论与归档状态。

    `origin` 记录这份日值从哪来：`file` 表示样例文件（下次读取时以文件为准，
    改文件即视为源更新）；`records` 表示接口提交的快照。
    """
    selection = cross_review.select_daily(records)
    result = cross_review.review(records, selection=selection)
    result["selection"] = {
        "selected": selection["selected"],
        "duplicates": selection["duplicates"],
        "conflicts": selection["conflicts"],
    }
    result["scope"] = scope
    result["review_id"] = review_id_for(scope, result["source_digest"])

    if not persist:
        result.update({"persisted": False, "created": False,
                       "invalidated_ids": [], "history_count": 0})
        return result

    source_updated = store.save_daily_source(
        scope=scope, source_digest=result["source_digest"],
        records=records, updated_at=stamp, origin=origin)
    saved = store.record_cross_review(
        review_id=result["review_id"], scope=scope,
        source_digest=result["source_digest"],
        rule_version=config.CROSS_REVIEW_RULE_VERSION,
        status="active", payload=_storable(result), created_at=stamp)
    result.update({
        "persisted": True,
        "created": saved["created"],
        "reactivated": saved.get("reactivated", False),
        "source_updated": source_updated,
        "invalidated_ids": saved["invalidated_ids"],
        "created_at": saved["record"]["created_at"],
        "history_count": store.count_cross_reviews(scope),
    })
    return result


def _storable(result: dict) -> dict:
    """落库时去掉归档元数据本身，避免自引用。"""
    return {key: value for key, value in result.items()
            if key not in ("persisted", "created", "source_updated",
                           "invalidated_ids", "created_at", "history_count",
                           "reactivated")}


def current(*, store, scope: str) -> dict | None:
    return store.latest_cross_review(scope)


def history(*, store, scope: str, limit: int = 50) -> list[dict]:
    return store.list_cross_reviews(scope, limit=limit)
