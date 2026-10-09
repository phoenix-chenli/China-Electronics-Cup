"""Independent re-derivation of headline numbers before they are published.

The aggregate comes from one SQL expression. This module recomputes the same
quantity from the contribution detail rows using Decimal arithmetic in Python,
which is a different execution path (different engine, different rounding).

A match raises confidence in the arithmetic; it does not prove the business
interpretation. A mismatch is reported and blocks the conclusion instead of
being averaged away.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from . import store as s

TOLERANCE = Decimal("0.01")
DETAIL_LIMIT = 20000
NET = ("net_sales", "bonus", "target")


def _dec(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _net(row):
    sales, returned = _dec(row.get("sales_amount")), _dec(row.get("returned_amount"))
    if sales is None or returned is None:
        return None
    return sales - returned


def _gross(row):
    return _dec(row.get("sales_amount"))


def _total(rows, field):
    total, missing = Decimal(0), 0
    for row in rows:
        value = _net(row) if field == "net_sales" else _gross(row)
        if value is None:
            missing += 1
            continue
        total += value
    return total, missing


def _close(left, right):
    return left is not None and right is not None and abs(left - right) <= TOLERANCE


def _money(value):
    return str(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def detail(locator, limit=DETAIL_LIMIT):
    """Read the contribution rows that produced the aggregate."""
    sql = locator.get("contribution_sql")
    if not sql:
        return None
    rows, truncated = s.read(sql, locator.get("contribution_params") or [], limit=limit)
    return rows, truncated


def verify(spec, result_rows, evidence):
    """Recompute the reported figure from detail. Returns None when not applicable."""
    locator = (evidence or {}).get("locator", {})
    loaded = detail(locator)
    if loaded is None:
        return None
    rows, truncated = loaded
    metric = spec.get("metric")
    if truncated:
        return {"method": "明细求和", "status": "unverified",
                "detail": "贡献明细超过读取上限，未完成独立复算；未据此断言数值正确。",
                "values": {}}
    if not rows:
        return {"method": "明细求和", "status": "unverified",
                "detail": "当前条件没有贡献明细，无法独立复算。", "values": {}}

    per_entity = bool(result_rows) and "product_id" in result_rows[0]
    if metric in NET or metric == "gross_sales":
        field = "gross_sales" if metric == "gross_sales" else "net_sales"
        total, missing = _total(rows, field)
        if missing:
            return {"method": "明细求和", "status": "failed",
                    "detail": f"{missing} 条明细缺少金额或退货字段，未完成复算。", "values": {}}
        if per_entity:
            reported = sum((_dec(row.get(field)) or Decimal(0)) for row in result_rows)
            if not _close(reported, total):
                return {"method": "守恒检查", "status": "failed",
                        "detail": f"分组结果合计 {_money(reported)} 与明细求和 {_money(total)} 不一致，已阻止发布该结论。",
                        "values": {"grouped": _money(reported), "detail": _money(total)}}
            return {"method": "守恒检查", "status": "passed",
                    "detail": f"{len(result_rows)} 组的分组金额合计与 {len(rows)} 条明细求和一致（{_money(total)} 元）。",
                    "values": {"grouped": _money(reported), "detail": _money(total)}}
        reported = _dec((result_rows[0] or {}).get(field))
        if not _close(reported, total):
            return {"method": "明细求和", "status": "failed",
                    "detail": f"汇总值 {_money(reported) if reported is not None else '空'} 与 {len(rows)} 条明细独立求和 "
                              f"{_money(total)} 不一致，已阻止发布该结论。",
                    "values": {"aggregate": _money(reported) if reported is not None else None, "detail": _money(total)}}
        detail_orders = len({row.get("order_id") for row in rows})
        reported_orders = (result_rows[0] or {}).get("order_count")
        grain = ""
        if isinstance(reported_orders, int) and reported_orders != detail_orders:
            return {"method": "明细求和", "status": "failed",
                    "detail": f"金额一致，但订单数 {reported_orders} 与明细去重后 {detail_orders} 不一致。",
                    "values": {"orders": reported_orders, "detail_orders": detail_orders}}
        if isinstance(reported_orders, int):
            grain = f"，订单去重 {detail_orders} 笔一致"
        return {"method": "明细求和", "status": "passed",
                "detail": f"{len(rows)} 条明细按 Decimal 独立求和与汇总值一致（{_money(total)} 元）{grain}。",
                "values": {"aggregate": _money(reported) if reported is not None else None, "detail": _money(total)}}

    if metric == "count":
        if not result_rows:
            return None
        customers = {row.get("customer_id") for row in rows if row.get("customer_id") is not None}
        orders = {row.get("order_id") for row in rows if row.get("order_id") is not None}
        row = result_rows[0]
        if row.get("customer_count") != len(customers) or row.get("order_count") != len(orders):
            return {"method": "明细去重", "status": "failed",
                    "detail": f"汇总客户 {row.get('customer_count')} / 订单 {row.get('order_count')} 与明细去重 "
                              f"{len(customers)} / {len(orders)} 不一致，已阻止发布该结论。",
                    "values": {"customers": len(customers), "orders": len(orders)}}
        return {"method": "明细去重", "status": "passed",
                "detail": f"明细去重得到 {len(customers)} 个客户、{len(orders)} 笔订单，与汇总一致。",
                "values": {"customers": len(customers), "orders": len(orders)}}

    if metric == "growth_rate" and result_rows:
        years = {str(row.get("order_date", ""))[:4] for row in rows}
        current, compare = str(spec.get("year")), str(spec.get("compare_year"))
        if current not in years or compare not in years:
            return {"method": "分期求和", "status": "unverified",
                    "detail": "贡献明细未覆盖两个对比期，未完成独立复算。", "values": {}}
        buckets = {current: Decimal(0), compare: Decimal(0)}
        for row in rows:
            key = str(row.get("order_date", ""))[:4]
            value = _net(row)
            if key in buckets and value is not None:
                buckets[key] += value
        row = result_rows[0]
        current_ok = _close(_dec(row.get("current_net_sales")), buckets[current])
        compare_ok = _close(_dec(row.get("compare_net_sales")), buckets[compare])
        if not (current_ok and compare_ok):
            return {"method": "分期求和", "status": "failed",
                    "detail": f"分期明细求和（{current} 年 {_money(buckets[current])} 元、{compare} 年 "
                              f"{_money(buckets[compare])} 元）与汇总值不一致，已阻止发布该结论。",
                    "values": {"current": _money(buckets[current]), "compare": _money(buckets[compare])}}
        return {"method": "分期求和", "status": "passed",
                "detail": f"两期明细分别独立求和与汇总一致（{current} 年 {_money(buckets[current])} 元、"
                          f"{compare} 年 {_money(buckets[compare])} 元）。",
                "values": {"current": _money(buckets[current]), "compare": _money(buckets[compare])}}

    return None
