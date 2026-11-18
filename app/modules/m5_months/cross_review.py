"""跨站复核：只在两站共同的实测日上比较，零雨也算实测。

为什么不能直接比月雨量：邻站在暴雨日缺测时，整月合计天然偏小，直接相减会把
「缺测」误报成「雨量异常」，巡查线索失真；等整月补齐又耽误汛期巡查。

规则：
- 实测日 = 该站在该日有采纳的日值，`0.0` 是有效零雨，算实测；
- 共同实测日 = 两站都有实测的日期集合；
- 只在共同实测日上分别求和再比较，未共同覆盖的日子两边都不计入；
- 两站某月没有共同实测日 → 结论「证据不足」，不给差异；
- 差额绝对值 ≤ 10.0 mm 或相对差 ≤ 20% 判「一致」，两项都超才判「提示巡查」；
- 复核依据（共同实测日逐日两站雨量）随结论一起返回，页面可下钻。

结论按日值源的摘要（source_digest）归档：同一批日值重跑命中同一摘要，
不新增记录；摘要变化说明源数据已更新，旧结论标记失效并重算。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from ... import config
from .hydro import select_daily

STATUS_LABELS = {
    "consistent": "一致",
    "divergent": "提示巡查",
    "insufficient": "证据不足",
}


def _rain(value) -> Decimal | None:
    """日值 → Decimal；空值与哨兵是缺测，`0` / `0.0` 是有效实测。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in config.SENTINELS:
        return None
    try:
        number = Decimal(text)
    except Exception:
        return None
    return number


def measured_days(selected: list[dict]) -> dict[str, dict[str, Decimal]]:
    """{站号: {日期: 雨量}}，只收有实测值的日。"""
    out: dict[str, dict[str, Decimal]] = {}
    for row in selected:
        value = _rain(row.get("rainfall_mm"))
        if value is None:
            continue
        station = str(row["station"])
        date = str(row["date"])[:10]
        out.setdefault(station, {})[date] = value
    return out


def source_digest(records: list[dict]) -> str:
    """按「站号 + 日期 + 雨量 + 版本」取摘要，与输入顺序、数值写法无关。

    摘要覆盖**原始日值源**（含低版本重复与同版本冲突行），因此源里任何一行
    变化都会让旧结论失效；同样的源重跑摘要不变，命中已有结论。
    """
    parts = [config.CROSS_REVIEW_RULE_VERSION]
    normalized = []
    for row in records:
        value = _rain(row.get("rainfall_mm"))
        rain = "" if value is None else format(value.normalize(), "f")
        normalized.append((str(row["station"]), str(row["date"])[:10], rain,
                           str(row.get("version", ""))))
    for station, date, rain, version in sorted(normalized):
        parts.append(f"{station}|{date}|{rain}|{version}")
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _verdict(rain_a: Decimal, rain_b: Decimal) -> tuple[str, Decimal, Decimal]:
    """按未量化的差额判定，返回值供展示时才量化到 0.1 mm。"""
    raw_diff = rain_a - rain_b
    scale = max(abs(rain_a), abs(rain_b))
    relative = (abs(raw_diff) / scale) if scale > 0 else Decimal("0")
    status = ("divergent"
              if abs(raw_diff) > config.CROSS_REVIEW_ABS_TOLERANCE_MM
              and relative > config.CROSS_REVIEW_REL_TOLERANCE
              else "consistent")
    return status, raw_diff, relative


def _insufficient(station_a: str, station_b: str, month: str,
                  days_a: dict[str, Decimal], days_b: dict[str, Decimal]) -> dict:
    return {
        "month": month, "station_a": station_a, "station_b": station_b,
        "month_days_a": len(days_a), "month_days_b": len(days_b),
        "month_rainfall_a": str(sum(days_a.values(), Decimal("0")).quantize(
            config.CROSS_REVIEW_RAIN_QUANT)),
        "month_rainfall_b": str(sum(days_b.values(), Decimal("0")).quantize(
            config.CROSS_REVIEW_RAIN_QUANT)),
        "naive_diff": "", "common_days": 0, "evidence": [],
        "status": "insufficient", "status_label": STATUS_LABELS["insufficient"],
        "rainfall_a": "", "rainfall_b": "", "diff": "", "relative": "",
        "reason": (f"两站无共同实测日（{station_a} 实测 {len(days_a)} 日，"
                   f"{station_b} 实测 {len(days_b)} 日），证据不足"),
    }


def _pair_month(station_a: str, station_b: str, month: str,
                days_a: dict[str, Decimal], days_b: dict[str, Decimal]) -> dict:
    in_month_a = {date: value for date, value in days_a.items()
                  if date[:7] == month}
    in_month_b = {date: value for date, value in days_b.items()
                  if date[:7] == month}
    common = sorted(set(in_month_a) & set(in_month_b))
    if not common:
        row = _insufficient(station_a, station_b, month, in_month_a, in_month_b)
        row["naive_diff"] = str(
            (sum(in_month_a.values(), Decimal("0"))
             - sum(in_month_b.values(), Decimal("0"))).quantize(
                 config.CROSS_REVIEW_RAIN_QUANT))
        return row
    evidence = [{"date": date, "rainfall_a": str(in_month_a[date]),
                 "rainfall_b": str(in_month_b[date])} for date in common]
    total_a = sum(in_month_a.values(), Decimal("0"))
    total_b = sum(in_month_b.values(), Decimal("0"))
    rain_a = sum((in_month_a[date] for date in common), Decimal("0"))
    rain_b = sum((in_month_b[date] for date in common), Decimal("0"))
    status, diff, relative = _verdict(rain_a, rain_b)
    diff_display = diff.quantize(config.CROSS_REVIEW_RAIN_QUANT)
    return {
        "month": month, "station_a": station_a, "station_b": station_b,
        "month_days_a": len(in_month_a), "month_days_b": len(in_month_b),
        "month_rainfall_a": str(total_a.quantize(config.CROSS_REVIEW_RAIN_QUANT)),
        "month_rainfall_b": str(total_b.quantize(config.CROSS_REVIEW_RAIN_QUANT)),
        "naive_diff": str((total_a - total_b).quantize(
            config.CROSS_REVIEW_RAIN_QUANT)),
        "common_days": len(common), "evidence": evidence,
        "status": status, "status_label": STATUS_LABELS[status],
        "rainfall_a": str(rain_a.quantize(config.CROSS_REVIEW_RAIN_QUANT)),
        "rainfall_b": str(rain_b.quantize(config.CROSS_REVIEW_RAIN_QUANT)),
        "diff": str(diff_display), "relative": str((relative * 100).quantize(
            config.CROSS_REVIEW_RAIN_QUANT)),
        "reason": (f"共同实测日 {len(common)} 天，差 {diff_display} mm"
                   f"（相对 {float(relative) * 100:.1f}%）；"
                   "未采用整月总量直接比较"),
    }


def review(records: list[dict], *, selection: dict | None = None) -> dict:
    """对一批日值做跨站复核，返回结论、依据与摘要。

    站点与月份按「源里有日值行」确定，而不是只看采纳行：
    某站某日因同版本冲突被隔离、或整月都缺测时，仍作为「证据不足」出现，
    不能因为裁决后没有行就悄悄从复核里消失。
    """
    selected = selection["selected"] if selection is not None else select_daily(records)["selected"]
    days = measured_days(selected)
    empty: dict[str, Decimal] = {}

    # 站点与月份按「源里有日值行」确定，而不是只看采纳行：
    # 某站某日因同版本冲突被隔离、或整月都缺测时，仍应作为证据不足出现，
    # 不能因为裁决后没有行就悄悄从复核里消失。
    stations: set[str] = set()
    rows_by_station: dict[str, set[str]] = {}
    for row in records:
        station = str(row["station"])
        stations.add(station)
        rows_by_station.setdefault(station, set()).add(str(row["date"])[:7])

    pairs: list[dict] = []
    months: set[str] = set()
    ordered = sorted(stations)
    for index, station_a in enumerate(ordered):
        months_a = rows_by_station.get(station_a, set())
        for station_b in ordered[index + 1:]:
            months_b = rows_by_station.get(station_b, set())
            shared = sorted(months_a & months_b)
            if not shared:
                pairs.append(_insufficient(station_a, station_b, "—",
                                           days.get(station_a, empty),
                                           days.get(station_b, empty)))
                pairs[-1]["reason"] = (
                    f"两站没有共同月份（{station_a}：{'、'.join(sorted(months_a)) or '无记录'}；"
                    f"{station_b}：{'、'.join(sorted(months_b)) or '无记录'}），"
                    "证据不足")
                continue
            for month in shared:
                pairs.append(_pair_month(station_a, station_b, month,
                                         days.get(station_a, empty),
                                         days.get(station_b, empty)))
                months.add(month)

    summary = {
        "pair_count": len(pairs),
        "month_count": len(months),
        "station_count": len(stations),
        "consistent": sum(1 for item in pairs if item["status"] == "consistent"),
        "divergent": sum(1 for item in pairs if item["status"] == "divergent"),
        "insufficient": sum(1 for item in pairs if item["status"] == "insufficient"),
    }
    if selection is not None:
        summary["input_records"] = len(records)
        summary["accepted"] = len(selection["selected"])
        summary["duplicates"] = len(selection["duplicates"])
        summary["conflicts"] = len(selection["conflicts"])
    return {
        "source_digest": source_digest(records),
        "rule_version": config.CROSS_REVIEW_RULE_VERSION,
        "abs_tolerance_mm": str(config.CROSS_REVIEW_ABS_TOLERANCE_MM),
        "rel_tolerance_pct": str(config.CROSS_REVIEW_REL_TOLERANCE * 100),
        "stations": ordered,
        "months": sorted(months),
        "pairs": pairs,
        "summary": summary,
    }
