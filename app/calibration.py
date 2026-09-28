"""锚点校准：用审计员人工确认的成对事件求待校准来源的 offset 可行范围。

定位
----
双来源对账（``reconciliation``）给出"算法认为可能是同一动作"的事件对之后，
审计员可以人工勾选 1～20 对**已配对且确认是同一真实动作**的事件作为锚点，
并指定：

- **待校准来源**（calibrated）：其 ``clock_offset`` 允许调整；
- **参照来源**（reference）：作为不动的基准。

本模块是**只读**分析：复用当前含未保存调整（overrides）的时钟校正结果，
事件真实时间区间直接取 ``engine`` 在该假设下的计算值；漂移（drift_ppm）、
不确定度（uncertainty）、可靠度在求解中**固定不变**——锚点只约束
``clock_offset`` 这一个平移量。

数学
----
设待校准事件 c 当前（有效）闭区间 ``[lo_c, hi_c]``、参照事件 r 的闭区间
``[lo_r, hi_r]``。给待校准来源的 clock_offset 增加增量 δ 后，其全部事件
区间整体平移为 ``[lo_c − δ, hi_c − δ]``（clock_offset 增大 ⇒ 估计时刻前移）。

要求两个闭区间相交（同一真实动作）：

    lo_c − δ ≤ hi_r  且  lo_r ≤ hi_c − δ

⟹  **lo_c − hi_r ≤ δ ≤ hi_c − lo_r**

每对锚点给出 δ 的一个闭区间；所有锚点区间取交集即同时满足全部人工锚点的
offset 增量范围：

    δ_lo = max_i (lo_ci − hi_ri)
    δ_hi = min_i (hi_ci − lo_ri)

- ``δ_lo ≤ δ_hi``：交集非空，允许的**绝对 offset** 为
  ``[offset_当前 + δ_lo, offset_当前 + δ_hi]``，中点一键写入左栏**未保存**
  假设（与手动改值完全等价，仍可放弃调整）；
- ``δ_lo > δ_hi``：交集为空，分别列出决定冲突下界（取到 δ_lo）与冲突上界
  （取到 δ_hi）的两对锚点及其各自范围（含并列时的全部决定者）。

整数毫秒
--------
与对账模块一致：进入判定的区间端点先 half-up 四舍五入到整数毫秒
（复用 ``reconciliation.round_half_up_ms``，非银行家舍入），δ 的上下界与
交集全部为整数毫秒运算，浮点误差不影响边界结论。
"""
from __future__ import annotations

from typing import Any

from . import engine
from .reconciliation import _event_record, round_half_up_ms
from .timeutil import fmt_delta

MIN_ANCHORS = 1
MAX_ANCHORS = 20  # 锚点对数硬上限：人工确认动作，过多即失去审计意义


class CalibrationError(ValueError):
    """可直接映射为 HTTP 4xx 的锚点校准请求错误。"""


# ------------------------------------------------------------ 输入清洗

def _clean_side(raw: Any, role: str) -> str:
    sid = str(raw if raw is not None else "").strip()
    if not sid:
        raise CalibrationError(f"必须选择{role}来源")
    return sid


def _clean_anchors(raw: Any) -> list[tuple[str, str]]:
    """校验并返回 [(cal_event_id, ref_event_id), ...]（保持请求顺序）。"""
    if not isinstance(raw, list):
        raise CalibrationError("锚点列表 anchors 必须是数组，每元素含 "
                               "cal_event_id / ref_event_id")
    n = len(raw)
    if not (MIN_ANCHORS <= n <= MAX_ANCHORS):
        raise CalibrationError(
            f"锚点数量必须在 {MIN_ANCHORS}～{MAX_ANCHORS} 对之间，收到 {n} 对")
    pairs: list[tuple[str, str]] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise CalibrationError(f"第 {i} 个锚点必须是对象 "
                                   f"（cal_event_id / ref_event_id），收到: {item!r}")
        cid = item.get("cal_event_id")
        rid = item.get("ref_event_id")
        cid = str(cid).strip() if cid is not None else ""
        rid = str(rid).strip() if rid is not None else ""
        if not cid:
            raise CalibrationError(f"第 {i} 对锚点缺少待校准事件 ID（cal_event_id）")
        if not rid:
            raise CalibrationError(f"第 {i} 对锚点缺少参照事件 ID（ref_event_id）")
        pairs.append((cid, rid))
    return pairs


def _event_brief(rec: dict[str, Any]) -> dict[str, Any]:
    """冲突说明中的单侧事件摘要（可点击在主时间线定位）。"""
    return {
        "id": rec["id"],
        "source_id": rec["source_id"],
        "source_name": rec["source_name"],
        "event_type": rec["event_type"],
        "actor": rec["actor"],
        "summary": rec["summary"],
        "mid_text": rec["mid_text"],
        "lo_text": rec["lo_text"],
        "hi_text": rec["hi_text"],
    }


def _delta_range_text(lo_ms: int, hi_ms: int) -> str:
    return f"[{fmt_delta(lo_ms / 1000.0)}, {fmt_delta(hi_ms / 1000.0)}]"


# ------------------------------------------------------------ 主入口

def calibrate_offset(calibrated_source: Any, reference_source: Any,
                     anchors: Any, overrides: dict | None = None) -> dict[str, Any]:
    """基于人工锚点求待校准来源 clock_offset 的可行区间（只读，不写数据库）。

    overrides 为页面当前（可能未保存）的时钟假设；事件区间完全复用
    ``engine`` 在该假设下的时钟校正结果。
    """
    sid_c = _clean_side(calibrated_source, "待校准方")
    sid_r = _clean_side(reference_source, "参照方")
    if sid_c == sid_r:
        raise CalibrationError(
            f"待校准来源与参照来源必须不同（收到的两侧都是 {sid_c}）")
    raw_pairs = _clean_anchors(anchors)

    stored_sources, raw_events, _ = engine.load_model()
    if sid_c not in stored_sources:
        raise CalibrationError(f"来源不存在: {sid_c}")
    if sid_r not in stored_sources:
        raise CalibrationError(f"来源不存在: {sid_r}")

    sources = engine._effective_sources(stored_sources, overrides)
    nodes = engine._build_nodes(raw_events, sources)
    node_by_id = {n.raw["id"]: n for n in nodes}

    eff_c = sources[sid_c]
    eff_r = sources[sid_r]
    current_offset = float(eff_c["clock_offset"])

    # ------------------------------------------------ 逐条锚点校验
    seen_cal: dict[str, int] = {}
    seen_ref: dict[str, int] = {}
    checked: list[dict[str, Any]] = []
    for i, (cid, rid) in enumerate(raw_pairs, start=1):
        nc = node_by_id.get(cid)
        if nc is None:
            raise CalibrationError(f"第 {i} 对锚点的待校准事件不存在（事件已失效或被删除）: {cid}")
        nr = node_by_id.get(rid)
        if nr is None:
            raise CalibrationError(f"第 {i} 对锚点的参照事件不存在（事件已失效或被删除）: {rid}")
        if nc.raw["source_id"] != sid_c:
            raise CalibrationError(
                f"第 {i} 对锚点来源不一致：待校准事件 {cid} 属于来源 "
                f"{nc.raw['source_id']}，不是待校准来源 {sid_c}")
        if nr.raw["source_id"] != sid_r:
            raise CalibrationError(
                f"第 {i} 对锚点来源不一致：参照事件 {rid} 属于来源 "
                f"{nr.raw['source_id']}，不是参照来源 {sid_r}")
        if cid in seen_cal:
            raise CalibrationError(
                f"锚点重复：待校准事件 {cid} 同时出现在第 {seen_cal[cid]} 对与第 {i} 对；"
                "每个事件最多参与一对锚点")
        if rid in seen_ref:
            raise CalibrationError(
                f"锚点重复：参照事件 {rid} 同时出现在第 {seen_ref[rid]} 对与第 {i} 对；"
                "每个事件最多参与一对锚点")
        seen_cal[cid] = i
        seen_ref[rid] = i

        rec_c = _event_record(nc)
        rec_r = _event_record(nr)
        act_c = (rec_c.get("actor") or "").strip()
        act_r = (rec_r.get("actor") or "").strip()
        if not act_c or not act_r:
            raise CalibrationError(
                f"第 {i} 对锚点无法确认是同一动作：事件 {cid} / {rid} 的 actor 为空；"
                "锚点须取自双来源对账中已配对的事件")
        if act_c != act_r:
            raise CalibrationError(
                f"第 {i} 对锚点不是同一动作：actor 不一致 "
                f"（{cid}={act_c!r}，{rid}={act_r!r}）")
        if rec_c["event_type"] != rec_r["event_type"]:
            raise CalibrationError(
                f"第 {i} 对锚点不是同一动作：event_type 不一致 "
                f"（{cid}={rec_c['event_type']!r}，{rid}={rec_r['event_type']!r}）")
        checked.append({"req_index": i, "rec_c": rec_c, "rec_r": rec_r})

    # 稳定输出：按待校准事件 ID（并列再按参照事件 ID）排序
    checked.sort(key=lambda a: (a["rec_c"]["id"], a["rec_r"]["id"]))

    # ------------------------------------------------ 每对锚点的 δ 范围
    anchor_items: list[dict[str, Any]] = []
    for out_idx, item in enumerate(checked, start=1):
        rec_c, rec_r = item["rec_c"], item["rec_r"]
        # 端点统一 half-up 到整数毫秒后做整数运算
        lo_c, hi_c = rec_c["lo_ms"], rec_c["hi_ms"]
        lo_r, hi_r = rec_r["lo_ms"], rec_r["hi_ms"]
        d_lo = lo_c - hi_r
        d_hi = hi_c - lo_r
        closed_gap = max(0, lo_r - hi_c, lo_c - hi_r)
        mid_delta = rec_c["mid_ms"] - rec_r["mid_ms"]
        anchor_items.append({
            "index": out_idx,
            "request_index": item["req_index"],
            "calibrated": rec_c,
            "reference": rec_r,
            "actor": act_c,
            "event_type": rec_c["event_type"],
            "delta_lo_ms": d_lo,
            "delta_hi_ms": d_hi,
            "delta_lo_text": fmt_delta(d_lo / 1000.0),
            "delta_hi_text": fmt_delta(d_hi / 1000.0),
            "delta_range_ms": [d_lo, d_hi],
            "delta_range_text": _delta_range_text(d_lo, d_hi),
            # 当前（未再校准前）两区间的关系，供审计员核对锚点质量
            "current_closed_gap_ms": closed_gap,
            "current_mid_delta_ms": mid_delta,
            "current_mid_delta_text": fmt_delta(mid_delta / 1000.0),
        })

    # ------------------------------------------------ 交集
    total_lo = max(a["delta_lo_ms"] for a in anchor_items)
    total_hi = min(a["delta_hi_ms"] for a in anchor_items)
    feasible = total_lo <= total_hi

    lower_items = [a for a in anchor_items if a["delta_lo_ms"] == total_lo]
    upper_items = [a for a in anchor_items if a["delta_hi_ms"] == total_hi]
    for a in anchor_items:
        if not feasible:
            a["determines"] = ("lower" if a["delta_lo_ms"] == total_lo
                               else "upper" if a["delta_hi_ms"] == total_hi
                               else None)
        else:
            a["determines"] = None

    abs_lo_ms = round_half_up_ms(current_offset) + total_lo
    abs_hi_ms = round_half_up_ms(current_offset) + total_hi

    name_c = eff_c.get("name", sid_c)
    name_r = eff_r.get("name", sid_r)

    result: dict[str, Any] = {
        "calibration_type": "anchor-offset-calibration",
        "feasible": feasible,
        "calibrated_source": {"id": sid_c, "name": name_c},
        "reference_source": {"id": sid_r, "name": name_r},
        "anchor_count": len(anchor_items),
        "current_offset_seconds": round(current_offset, 6),
        "current_offset_text": fmt_delta(current_offset),
        "fixed_assumptions": {
            "drift_ppm": round(float(eff_c["drift_ppm"]), 6),
            "uncertainty": round(float(eff_c["uncertainty"]), 6),
            "reliability": round(float(eff_c["reliability"]), 6),
            "note": "锚点校准只调整 clock_offset（整体平移）；漂移与不确定度固定为当前生效值（含未保存调整）",
        },
        "offset_delta_ms": [total_lo, total_hi],
        "offset_delta_text": _delta_range_text(total_lo, total_hi),
        "absolute_offset_ms": [abs_lo_ms, abs_hi_ms] if feasible else None,
        "absolute_offset_seconds": (
            [round(abs_lo_ms / 1000.0, 6), round(abs_hi_ms / 1000.0, 6)]
            if feasible else None),
        "absolute_offset_text": (
            _delta_range_text(abs_lo_ms, abs_hi_ms) if feasible else None),
        "midpoint_ms": None,
        "midpoint_seconds": None,
        "conflict": None,
        "anchors": anchor_items,
        "stats": {
            "anchor_count": len(anchor_items),
            "lower_determiners": len(lower_items),
            "upper_determiners": len(upper_items),
        },
        "calibration": {
            "rule": "对每对锚点求使两个闭区间相交的待校准方 clock_offset 增量范围 "
                    "δ ∈ [lo_cal − hi_ref, hi_cal − lo_ref]，再取所有锚点范围的交集",
            "interval_reuse": "复用当前含未保存调整的事件时间区间（engine 时钟校正），只读不写库",
            "fixed_params": ["drift_ppm", "uncertainty", "reliability"],
            "rounding": "区间端点 / offset 增量 / 绝对 offset 均四舍五入到整数毫秒（half-up）",
            "adopt": "采用中点仅更新左栏未保存 clock_offset 假设并重算时间线，仍可放弃调整",
            "max_anchors": MAX_ANCHORS,
        },
    }

    if feasible:
        mid_ms = round_half_up_ms((abs_lo_ms + abs_hi_ms) / 2000.0)
        result["midpoint_ms"] = mid_ms
        result["midpoint_seconds"] = round(mid_ms / 1000.0, 6)
        result["summary"] = (
            f"{len(anchor_items)} 对人工锚点（参照 {name_r}）给出的 offset 增量范围交集非空："
            f"待校准来源 {name_c}（{sid_c}）的 clock_offset 允许落在 "
            f"{_delta_range_text(abs_lo_ms, abs_hi_ms)}（绝对 offset），"
            f"即相对当前值 {fmt_delta(current_offset)} 的增量 "
            f"{_delta_range_text(total_lo, total_hi)}；漂移与不确定度固定不变。"
            f"点「采用中点」把 {round(mid_ms / 1000.0, 6)} s 写入左栏未保存假设并重算时间线，"
            "仍可在左栏放弃调整。"
        )
    else:
        lo_anchor = lower_items[0]
        hi_anchor = upper_items[0]

        def _conflict_side(a: dict[str, Any]) -> dict[str, Any]:
            return {
                "anchor_index": a["index"],
                "request_index": a["request_index"],
                "calibrated": _event_brief(a["calibrated"]),
                "reference": _event_brief(a["reference"]),
                "actor": a["actor"],
                "event_type": a["event_type"],
                "delta_range_ms": a["delta_range_ms"],
                "delta_range_text": a["delta_range_text"],
            }

        tie_lo = len(lower_items) > 1
        tie_hi = len(upper_items) > 1
        result["conflict"] = {
            "lower_bound_ms": total_lo,
            "lower_bound_text": fmt_delta(total_lo / 1000.0),
            "upper_bound_ms": total_hi,
            "upper_bound_text": fmt_delta(total_hi / 1000.0),
            "gap_ms": total_lo - total_hi,
            "gap_text": fmt_delta((total_lo - total_hi) / 1000.0),
            "lower_anchor": _conflict_side(lo_anchor),
            "upper_anchor": _conflict_side(hi_anchor),
            "lower_determiner_indices": [a["index"] for a in lower_items],
            "upper_determiner_indices": [a["index"] for a in upper_items],
            "lower_tie": tie_lo,
            "upper_tie": tie_hi,
            "explanation": (
                f"{len(anchor_items)} 对锚点的 offset 增量范围交集为空："
                f"冲突下界 {fmt_delta(total_lo / 1000.0)} 由第 {lo_anchor['index']} 对锚点"
                f"（{lo_anchor['calibrated']['id']} / {lo_anchor['reference']['id']}，"
                f"范围 {lo_anchor['delta_range_text']}）决定；"
                f"冲突上界 {fmt_delta(total_hi / 1000.0)} 由第 {hi_anchor['index']} 对锚点"
                f"（{hi_anchor['calibrated']['id']} / {hi_anchor['reference']['id']}，"
                f"范围 {hi_anchor['delta_range_text']}）决定；"
                f"下界高出上界 {fmt_delta((total_lo - total_hi) / 1000.0)}。"
                "不存在任何单一 clock_offset 能让全部锚点的闭区间同时相交——"
                "请复核锚点配对是否确属同一动作，或待校准来源的漂移 / 不确定度假设。"
            ),
        }
        result["summary"] = (
            f"{len(anchor_items)} 对人工锚点的 offset 增量范围交集为空："
            f"下界 {fmt_delta(total_lo / 1000.0)}（锚点 {lo_anchor['calibrated']['id']}/"
            f"{lo_anchor['reference']['id']}）高于上界 {fmt_delta(total_hi / 1000.0)}"
            f"（锚点 {hi_anchor['calibrated']['id']}/{hi_anchor['reference']['id']}），"
            f"差 {fmt_delta((total_lo - total_hi) / 1000.0)}；详见下方冲突锚点。"
        )
    return result
