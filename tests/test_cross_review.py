"""跨站复核：共同实测日、零雨算实测、证据不足、源更新失效重算、同源幂等。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.main import create_app
from app.modules.m5_months import cross_review, review_service


def _records(*rows):
    return [{"station": station, "date": date, "rainfall_mm": rain, "version": version}
            for station, date, rain, version in rows]


def test_zero_rain_counts_as_measured_day():
    selected = cross_review.select_daily(_records(
        ("A", "2024-02-01", "0.0", 1),
        ("B", "2024-02-01", "0.0", 1)))
    days = cross_review.measured_days(selected["selected"])
    assert days["A"] == {"2024-02-01": 0}
    assert days["B"] == {"2024-02-01": 0}


def test_missing_value_is_not_a_measured_day():
    records = _records(
        ("A", "2024-02-01", "0.0", 1),
        ("A", "2024-02-02", "", 1),
        ("A", "2024-02-03", "N/A", 1),
        ("A", "2024-02-04", "—", 1))
    selected = cross_review.select_daily(records)
    days = cross_review.measured_days(selected["selected"])
    assert sorted(days["A"]) == ["2024-02-01"]


def test_common_days_only_shared_measured_dates():
    """邻站暴雨日缺测：缺测日不计入任何一侧，结论不会被缺测拉偏。"""
    records = _records(
        ("A", "2024-06-01", "80.0", 1),
        ("A", "2024-06-02", "5.0", 1),
        ("A", "2024-06-03", "0.0", 1),
        # B 在 06-01 暴雨日缺测，只有 06-02、06-03 实测
        ("B", "2024-06-02", "5.0", 1),
        ("B", "2024-06-03", "0.0", 1))
    result = cross_review.review(records)
    pair = next(item for item in result["pairs"] if item["station_b"] == "B")
    assert pair["month"] == "2024-06"
    assert pair["common_days"] == 2
    assert pair["rainfall_a"] == "5.0" and pair["rainfall_b"] == "5.0"
    assert pair["status"] == "consistent"
    # 整月总量直接相减会差 80 mm，这里显式保留朴素值作对照
    assert pair["naive_diff"] == "80.0"


def test_no_common_measured_day_reports_insufficient():
    records = _records(
        ("A", "2024-06-01", "80.0", 1),
        ("B", "2024-06-02", "5.0", 1))
    result = cross_review.review(records)
    assert result["pairs"][0]["status"] == "insufficient"
    assert result["pairs"][0]["diff"] == ""
    assert "证据不足" in result["pairs"][0]["reason"]


def test_both_stations_present_but_all_missing_is_insufficient():
    """两站都有记录但同月全部缺测：仍要给「证据不足」，不能静默消失。"""
    result = cross_review.review(_records(
        ("A", "2024-06-01", "", 1),
        ("B", "2024-06-01", "", 1)))
    assert result["pairs"][0]["status"] == "insufficient"
    assert result["summary"]["station_count"] == 2
    assert result["summary"]["insufficient"] == 1


def test_station_without_overlapping_month_is_insufficient_pair():
    result = cross_review.review(_records(
        ("A", "2024-06-01", "1.0", 1),
        ("B", "2024-07-01", "1.0", 1)))
    assert result["pairs"][0]["status"] == "insufficient"
    assert "没有共同月份" in result["pairs"][0]["reason"]


def test_single_station_has_no_pairs():
    result = cross_review.review(_records(("A", "2024-06-01", "1.0", 1)))
    assert result["pairs"] == []
    assert result["summary"]["station_count"] == 1


def test_only_common_months_get_pairs():
    records = _records(
        ("A", "2024-02-01", "1.0", 1),
        ("A", "2024-03-01", "2.0", 1),
        ("B", "2024-02-01", "1.0", 1))
    result = cross_review.review(records)
    assert [item["month"] for item in result["pairs"]] == ["2024-02"]


def test_divergent_needs_both_absolute_and_relative():
    records = _records(
        ("A", "2024-06-01", "100.0", 1),
        ("B", "2024-06-01", "80.0", 1))
    result = cross_review.review(records)
    pair = result["pairs"][0]
    assert pair["diff"] == "20.0"
    # 20 mm 超绝对阈值，但相对差正好 20%，不超相对阈值 → 一致
    assert pair["status"] == "consistent"


def test_large_relative_difference_is_flagged():
    records = _records(
        ("A", "2024-06-01", "90.0", 1),
        ("B", "2024-06-01", "10.0", 1))
    assert cross_review.review(records)["pairs"][0]["status"] == "divergent"


def test_source_digest_is_order_independent():
    first = _records(("A", "2024-06-01", "1.0", 1), ("B", "2024-06-01", "2.0", 1))
    second = list(reversed(first))
    assert cross_review.source_digest(first) == \
        cross_review.source_digest(second)


def test_source_digest_covers_rows_excluded_from_selection():
    """低版本重复行也在源摘要里：源里任何一行变了，旧结论都应失效。"""
    base = _records(("A", "2024-06-01", "5.0", 2),
                    ("A", "2024-06-01", "3.0", 1),
                    ("B", "2024-06-01", "5.0", 1))
    changed_low = _records(("A", "2024-06-01", "5.0", 2),
                           ("A", "2024-06-01", "4.0", 1),
                           ("B", "2024-06-01", "5.0", 1))
    # 裁决结果（高版本 5.0）没变，但源变了
    assert cross_review.select_daily(base)["selected"][0]["rainfall_mm"] == "5.0"
    assert cross_review.select_daily(changed_low)["selected"][0]["rainfall_mm"] == "5.0"
    assert cross_review.source_digest(base) != cross_review.source_digest(changed_low)


def test_version_conflict_is_not_counted_as_measured_day():
    records = _records(
        ("A", "2024-06-01", "3.0", 2),
        ("A", "2024-06-01", "8.0", 2),
        ("B", "2024-06-01", "3.0", 1))
    selection = cross_review.select_daily(records)
    assert selection["conflicts"][0]["reason"] == "VERSION_CONFLICT"
    days = cross_review.measured_days(selection["selected"])
    assert days.get("A", {}) == {}
    # 冲突日不是实测日，但该站仍出现在复核里，结论是证据不足而不是消失
    pair = cross_review.review(records)["pairs"][0]
    assert pair["status"] == "insufficient"
    assert pair["common_days"] == 0


# ---------- 归档：幂等、失效、重算 ----------

def _client(tmp_path):
    settings = config.Settings(db_path=str(tmp_path / "x.sqlite"),
                               sample_dir=str(Path(__file__).resolve().parent.parent
                                              / "sample_data"))
    return TestClient(create_app(settings))


def _submit(client, records=None):
    body = {} if records is None else {"records": records}
    response = client.post("/api/cross-review", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_same_source_rerun_does_not_add_records(tmp_path):
    client = _client(tmp_path)
    records = _records(("A", "2024-06-01", "1.0", 1), ("B", "2024-06-01", "1.0", 1))
    first = _submit(client, records)
    second = _submit(client, records)
    assert first["created"] is True
    assert second["created"] is False
    assert second["review_id"] == first["review_id"]
    assert client.get("/api/cross-review/history").json()["count"] == 1


def test_source_update_invalidates_and_recomputes(tmp_path):
    client = _client(tmp_path)
    first = _submit(client, _records(
        ("A", "2024-06-01", "1.0", 1), ("B", "2024-06-01", "1.0", 1)))
    updated = _submit(client, _records(
        ("A", "2024-06-01", "1.0", 1), ("A", "2024-06-02", "5.0", 1),
        ("B", "2024-06-01", "1.0", 1), ("B", "2024-06-02", "5.0", 1)))
    assert updated["created"] is True
    assert updated["review_id"] != first["review_id"]
    assert first["review_id"] in updated["invalidated_ids"]

    history = client.get("/api/cross-review/history").json()
    assert history["count"] == 2
    statuses = {row["review_id"]: row["status"] for row in history["rows"]}
    assert statuses[first["review_id"]] == "invalidated"
    assert statuses[updated["review_id"]] == "active"

    active = client.get("/api/cross-review").json()
    assert active["review_id"] == updated["review_id"]
    assert active["review"]["summary"]["accepted"] == 4


def test_reverting_source_reactivates_without_duplicate(tmp_path):
    client = _client(tmp_path)
    base = _records(("A", "2024-06-01", "1.0", 1), ("B", "2024-06-01", "1.0", 1))
    first = _submit(client, base)
    _submit(client, base + _records(("A", "2024-06-02", "5.0", 1)))
    reverted = _submit(client, base)
    assert reverted["review_id"] == first["review_id"]
    assert client.get("/api/cross-review/history").json()["count"] == 2


def test_months_page_is_idempotent_on_reload(tmp_path):
    client = _client(tmp_path)
    client.get("/months")
    client.get("/months")
    assert client.get("/api/cross-review/history").json()["count"] == 1


def test_page_shows_evidence_dates(tmp_path):
    client = _client(tmp_path)
    _submit(client, _records(
        ("A", "2024-06-01", "5.0", 1), ("A", "2024-06-02", "0.0", 1),
        ("B", "2024-06-01", "5.0", 1), ("B", "2024-06-02", "0.0", 1)))
    html = client.get("/months").text
    assert "跨站复核" in html
    assert "2024-06-01" in html and "2024-06-02" in html
    assert "共同实测日" in html


def test_review_scoped_separately(tmp_path):
    client = _client(tmp_path)
    records = _records(("A", "2024-06-01", "1.0", 1), ("B", "2024-06-01", "1.0", 1))
    _submit(client, records)
    other = client.post("/api/cross-review",
                        json={"records": records, "scope": "other"}).json()
    assert other["created"] is True
    assert client.get("/api/cross-review").json()["review_id"] != other["review_id"]
    assert client.get("/api/cross-review/history").json()["count"] == 1


def test_empty_records_are_rejected(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/cross-review", json={"records": []})
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_REQUEST"


def test_hydro_monthly_preview_does_not_persist(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/hydro/monthly", json={"records": _records(
        ("A", "2024-06-01", "5.0", 1), ("B", "2024-06-01", "5.0", 1))})
    assert response.status_code == 200
    assert response.json()["cross_review"]["summary"]["consistent"] == 1
    assert client.get("/api/cross-review/history").json()["count"] == 0


def test_file_source_update_triggers_invalidation(tmp_path):
    """日值源文件一改，页面重算并失效旧结论；同源再访问不新增。"""
    import shutil
    sample = tmp_path / "sample_data"
    shutil.copytree(Path(__file__).resolve().parent.parent / "sample_data", sample)
    settings = config.Settings(db_path=str(tmp_path / "f.sqlite"),
                               sample_dir=str(sample))
    client = TestClient(create_app(settings))
    client.get("/months")
    first = client.get("/api/cross-review").json()
    assert client.get("/api/cross-review/history").json()["count"] == 1

    source = sample / "station_daily.csv"
    source.write_text(source.read_text(encoding="utf-8")
                      + "0031,2024-02-28,58.0,1\n", encoding="utf-8")
    client.get("/months")
    history = client.get("/api/cross-review/history").json()
    assert history["count"] == 2
    assert client.get("/api/cross-review").json()["review_id"] != first["review_id"]

    client.get("/months")
    assert client.get("/api/cross-review/history").json()["count"] == 2
