"""日值源：读取站点逐日雨量，作为跨站复核的输入。

样例用 `sample_data/station_daily.csv` 播种；页面的「重新复核」也接受
调用方直接传入记录（records）。字段：station / date / rainfall_mm / version。
空值与哨兵表示缺测，`0` / `0.0` 是有效零雨实测。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ... import config, errors

FIELDS = ("station", "date", "rainfall_mm", "version")


def sample_path(settings) -> Path:
    return Path(settings.sample_dir) / config.STATION_DAILY_FILE


def load_sample(settings) -> list[dict]:
    path = sample_path(settings)
    if not path.is_file():
        return []
    return parse_csv(path.read_text(encoding="utf-8-sig"))


def parse_csv(text: str) -> list[dict]:
    rows: list[dict] = []
    for lineno, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        station = (row.get("station") or "").strip()
        date = (row.get("date") or "").strip()
        if not station or not date:
            continue
        rows.append({
            "lineno": lineno,
            "station": station,
            "date": date,
            "rainfall_mm": (row.get("rainfall_mm") or "").strip(),
            "version": (row.get("version") or "1").strip() or "1",
        })
    return rows


def normalize_records(raw: list[dict]) -> list[dict]:
    """接口入参归一：空批直接拒绝，缺 station/date 的记录也拒绝，不猜。"""
    if not raw:
        raise errors.BadRequest(
            "records 为空；留空请从样例 station_daily.csv 重读",
            code="E_BAD_REQUEST")
    out: list[dict] = []
    for index, row in enumerate(raw, start=1):
        station = str(row.get("station") or "").strip()
        date = str(row.get("date") or "").strip()
        if not station or not date:
            raise errors.BadRequest(
                "日值记录缺少 station 或 date",
                code="E_BAD_REQUEST",
                detail={"index": index})
        out.append({
            "station": station,
            "date": date,
            "rainfall_mm": str(row.get("rainfall_mm", "")).strip(),
            "version": str(row.get("version", 1)).strip() or "1",
        })
    return out
