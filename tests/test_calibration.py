"""锚点校准的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_calibration.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-calib-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app import calibration as cb  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)


def ev(eid, src, t, etype="t", actor="u", uncertainty=None, summary=""):
    d = {"id": eid, "source": src, "time": f"2026-01-01T00:00:{t}Z",
         "type": etype, "actor": actor, "summary": summary or eid}
    if uncertainty is not None:
        d["error_ms"] = round(uncertainty * 1000)
    return d


def make(events, sources=None, **extra):
    sources = sources or [
        {"id": "s1", "name": "S1", "uncertainty": 0, "reliability": 0.9},
        {"id": "s2", "name": "S2", "uncertainty": 0, "reliability": 0.9},
    ]
    payload = {"sources": sources, "events": events}
    payload.update(extra)
    return import_payload(payload)


def anchors(*pairs):
    return [{"cal_event_id": c, "ref_event_id": r} for c, r in pairs]


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        reset_all()

    # --------------------------------------------------------- 基本数学

    def test_single_anchor_exact_delta(self):
        # s2 时钟慢 1s（offset=-1）：c@00 校正后落在 01，与 r@01 差 1s
        make([ev("c1", "s2", "00"), ev("r1", "s1", "01")])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertTrue(r["feasible"])
        # 当前 offset=0，δ ∈ [-1000, -1000] ms；绝对 offset ∈ [-1,-1]s
        self.assertEqual(r["offset_delta_ms"], [-1000, -1000])
        self.assertEqual(r["absolute_offset_ms"], [-1000, -1000])
        self.assertEqual(r["midpoint_ms"], -1000)
        self.assertEqual(r["midpoint_seconds"], -1.0)
        self.assertEqual(r["anchor_count"], 1)

    def test_sign_convention_positive_offset_calibrated_fast(self):
        # s2 本地 00:01 对应真实 00:00（offset=+1 才对）；参照在 00:00
        make([ev("c1", "s2", "01"), ev("r1", "s1", "00")])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["absolute_offset_ms"], [1000, 1000])
        self.assertEqual(r["midpoint_seconds"], 1.0)

    def test_uncertainty_extends_to_touching_endpoints(self):
        # c@00 ±0.5s（[−0.5, 0.5]），r@01 ±0.5s（[0.5, 1.5]）：
        # δ=0 时在 0.5 相接、δ=−2s 时在 1.5 相接 ⇒ δ ∈ [−2, 0]s
        make([ev("c1", "s2", "00", uncertainty=0.5),
              ev("r1", "s1", "01", uncertainty=0.5)])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["offset_delta_ms"], [-2000, 0])

    def test_uncertainty_widens_range(self):
        # c@00 ±1s（[−1,1]），r@01 ±1s（[0,2]）：δ ∈ [−3, 1]s，中点 −1s
        make([ev("c1", "s2", "00", uncertainty=1),
              ev("r1", "s1", "01", uncertainty=1)])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["offset_delta_ms"], [-3000, 1000])
        self.assertEqual(r["absolute_offset_ms"], [-3000, 1000])
        self.assertEqual(r["midpoint_ms"], -1000)

    def test_intersection_of_multiple_anchors(self):
        # 两对锚点都要求同一偏移 −1s（容差不同，交集取最严）
        make([
            ev("c1", "s2", "00"), ev("c2", "s2", "10"),
            ev("r1", "s1", "01"), ev("r2", "s1", "11", uncertainty=1),
        ])
        # c1/r1: δ=−1s 精确；c2/r2: r2 区间 [10,12] ⇒ δ ∈ [−2, 0]s
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1"), ("c2", "r2")))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["offset_delta_ms"], [-1000, -1000])
        self.assertEqual(r["stats"]["lower_determiners"], 1)
        self.assertEqual(r["stats"]["upper_determiners"], 1)

    def test_intersection_nonempty_range_midpoint(self):
        # 锚点1（c1 点 @0，r1 区间 [0,2]）δ ∈ [−2, 0]；
        # 锚点2（c2 点 @10，r2 区间 [9,11]）δ ∈ [−1, 1]
        # ⇒ 交集 [−1, 0]，中点 −500ms
        make([
            ev("c1", "s2", "00", uncertainty=0),
            ev("r1", "s1", "01", uncertainty=1),
            ev("c2", "s2", "10", uncertainty=0),
            ev("r2", "s1", "10", uncertainty=1),
        ])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1"), ("c2", "r2")))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["offset_delta_ms"], [-1000, 0])
        self.assertEqual(r["midpoint_ms"], -500)
        self.assertEqual(r["midpoint_seconds"], -0.5)

    def test_absolute_offset_builds_on_unsaved_current(self):
        # 当前含未保存 offset=4（s2 已假定快 4s），锚点要求绝对 offset=5
        make([ev("c1", "s2", "05"), ev("r1", "s1", "00")])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")),
                                {"s2": {"clock_offset": 4}})
        self.assertTrue(r["feasible"])
        self.assertEqual(r["current_offset_seconds"], 4.0)
        self.assertEqual(r["offset_delta_ms"], [1000, 1000])
        self.assertEqual(r["absolute_offset_ms"], [5000, 5000])
        self.assertEqual(r["midpoint_seconds"], 5.0)

    def test_unsaved_uncertainty_is_reused_and_fixed(self):
        # 未保存地把 s2 不确定度从 0 放宽到 1s：允许范围随之扩大
        make([ev("c1", "s2", "00"), ev("r1", "s1", "01")])
        r0 = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertEqual(r0["offset_delta_ms"], [-1000, -1000])
        r1 = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")),
                                 {"s2": {"uncertainty": 1}})
        self.assertTrue(r1["feasible"])
        self.assertEqual(r1["offset_delta_ms"], [-2000, 0])
        self.assertEqual(r1["fixed_assumptions"]["uncertainty"], 1.0)

    def test_anchors_sorted_stable(self):
        # 输出按待校准事件 ID 排序，与请求顺序无关
        make([
            ev("c2", "s2", "10"), ev("c1", "s2", "00"),
            ev("r2", "s1", "11"), ev("r1", "s1", "01"),
        ])
        r = cb.calibrate_offset("s2", "s1",
                                anchors(("c2", "r2"), ("c1", "r1")))
        self.assertTrue(r["feasible"])
        ids = [a["calibrated"]["id"] for a in r["anchors"]]
        self.assertEqual(ids, ["c1", "c2"])
        self.assertEqual([a["index"] for a in r["anchors"]], [1, 2])
        self.assertEqual(sorted(a["request_index"] for a in r["anchors"]), [1, 2])

    # --------------------------------------------------------- 交集为空

    def test_empty_intersection_names_two_anchors(self):
        # 锚点1 要求 δ=−1s；锚点2 要求 δ=+1s ⇒ 交集空
        make([
            ev("c1", "s2", "00"), ev("r1", "s1", "01"),    # δ = −1
            ev("c2", "s2", "12"), ev("r2", "s1", "11"),    # δ = +1
        ])
        r = cb.calibrate_offset("s2", "s1",
                                anchors(("c1", "r1"), ("c2", "r2")))
        self.assertFalse(r["feasible"])
        self.assertIsNone(r["absolute_offset_ms"])
        self.assertIsNone(r["midpoint_ms"])
        self.assertEqual(r["offset_delta_ms"], [1000, -1000])
        c = r["conflict"]
        self.assertEqual(c["lower_bound_ms"], 1000)
        self.assertEqual(c["upper_bound_ms"], -1000)
        self.assertEqual(c["gap_ms"], 2000)
        # 决定下界的是要求 δ=+1 的 c2/r2；决定上界的是要求 δ=−1 的 c1/r1
        self.assertEqual(c["lower_anchor"]["calibrated"]["id"], "c2")
        self.assertEqual(c["upper_anchor"]["calibrated"]["id"], "c1")
        self.assertEqual(c["lower_anchor"]["delta_range_ms"], [1000, 1000])
        self.assertEqual(c["upper_anchor"]["delta_range_ms"], [-1000, -1000])
        self.assertIn("交集", c["explanation"])

    def test_conflict_with_uncertainty_overlap_ranges(self):
        # 锚点1（c1 点@0，r1 区间 [0,2]）δ ∈ [−2, 0]；
        # 锚点2（c2 点@14，r2 区间 [10,12]）δ ∈ [2, 4] ⇒ 空；
        # 下界由锚点2、上界由锚点1，缺口 2000ms
        make([
            ev("c1", "s2", "00", uncertainty=0), ev("r1", "s1", "01", uncertainty=1),
            ev("c2", "s2", "14", uncertainty=0), ev("r2", "s1", "11", uncertainty=1),
        ])
        r = cb.calibrate_offset("s2", "s1",
                                anchors(("c1", "r1"), ("c2", "r2")))
        self.assertFalse(r["feasible"])
        self.assertEqual(r["conflict"]["gap_ms"], 2000)
        self.assertEqual(r["conflict"]["lower_anchor"]["delta_range_ms"], [2000, 4000])
        self.assertEqual(r["conflict"]["upper_anchor"]["delta_range_ms"], [-2000, 0])

    def test_tied_determiners_all_listed(self):
        # 三个锚点同取下界 1000：并列决定者全部列出
        make([
            ev("c1", "s2", "01"), ev("r1", "s1", "00"),
            ev("c2", "s2", "11"), ev("r2", "s1", "10"),
            ev("c3", "s2", "20"), ev("r3", "s1", "21"),    # δ=−1 决定上界
        ])
        r = cb.calibrate_offset("s2", "s1",
                                anchors(("c1", "r1"), ("c2", "r2"), ("c3", "r3")))
        self.assertFalse(r["feasible"])
        self.assertEqual(r["stats"]["lower_determiners"], 2)
        self.assertTrue(r["conflict"]["lower_tie"])
        self.assertFalse(r["conflict"]["upper_tie"])
        self.assertEqual(r["conflict"]["lower_determiner_indices"], [1, 2])

    # --------------------------------------------------------- 输入校验

    def test_same_source_rejected(self):
        make([ev("c1", "s1", "00"), ev("r1", "s1", "01")])
        with self.assertRaisesRegex(cb.CalibrationError, "必须不同"):
            cb.calibrate_offset("s1", "s1", anchors(("c1", "r1")))

    def test_missing_source_404(self):
        make([ev("c1", "s1", "00")])
        with self.assertRaisesRegex(cb.CalibrationError, "来源不存在"):
            cb.calibrate_offset("ghost", "s1", anchors(("c1", "c1")))

    def test_blank_side_rejected(self):
        with self.assertRaisesRegex(cb.CalibrationError, "待校准方"):
            cb.calibrate_offset("  ", "s1", anchors(("a", "b")))

    def test_anchor_count_out_of_range(self):
        make([ev("c1", "s2", "00"), ev("r1", "s1", "00")])
        with self.assertRaisesRegex(cb.CalibrationError, "1～20"):
            cb.calibrate_offset("s2", "s1", [])
        many = [{"cal_event_id": f"c{i}", "ref_event_id": f"r{i}"}
                for i in range(21)]
        with self.assertRaisesRegex(cb.CalibrationError, "1～20"):
            cb.calibrate_offset("s2", "s1", many)

    def test_anchors_must_be_list_and_objects(self):
        with self.assertRaisesRegex(cb.CalibrationError, "数组"):
            cb.calibrate_offset("s1", "s2", {"c1": "r1"})
        make([ev("c1", "s2", "00"), ev("r1", "s1", "00")])
        with self.assertRaisesRegex(cb.CalibrationError, "必须是对象"):
            cb.calibrate_offset("s2", "s1", [["c1", "r1"]])
        with self.assertRaisesRegex(cb.CalibrationError, "缺少待校准事件"):
            cb.calibrate_offset("s2", "s1", [{"ref_event_id": "r1"}])
        with self.assertRaisesRegex(cb.CalibrationError, "缺少参照事件"):
            cb.calibrate_offset("s2", "s1", [{"cal_event_id": "c1"}])

    def test_event_invalid_or_missing(self):
        make([ev("c1", "s2", "00"), ev("r1", "s1", "00")])
        with self.assertRaisesRegex(cb.CalibrationError, "待校准事件不存在"):
            cb.calibrate_offset("s2", "s1", anchors(("ghost", "r1")))
        with self.assertRaisesRegex(cb.CalibrationError, "参照事件不存在"):
            cb.calibrate_offset("s2", "s1", anchors(("c1", "ghost")))

    def test_event_source_mismatch(self):
        make([
            ev("x1", "s1", "00"), ev("c1", "s2", "00"),
            ev("y1", "s2", "00"), ev("r1", "s1", "00"),
        ])
        # 待校准事件实际属于 s1
        with self.assertRaisesRegex(cb.CalibrationError, "来源不一致"):
            cb.calibrate_offset("s2", "s1", anchors(("x1", "r1")))
        # 参照事件实际属于 s2
        with self.assertRaisesRegex(cb.CalibrationError, "来源不一致"):
            cb.calibrate_offset("s2", "s1", anchors(("c1", "y1")))

    def test_duplicate_anchors_rejected(self):
        make([
            ev("c1", "s2", "00"), ev("c2", "s2", "01"),
            ev("r1", "s1", "00"), ev("r2", "s1", "01"),
        ])
        # 完全重复的对
        with self.assertRaisesRegex(cb.CalibrationError, "锚点重复"):
            cb.calibrate_offset("s2", "s1",
                                anchors(("c1", "r1"), ("c1", "r1")))
        # 同一待校准事件配了两个参照
        with self.assertRaisesRegex(cb.CalibrationError, "锚点重复"):
            cb.calibrate_offset("s2", "s1",
                                anchors(("c1", "r1"), ("c1", "r2")))
        # 同一参照事件配了两个待校准事件
        with self.assertRaisesRegex(cb.CalibrationError, "锚点重复"):
            cb.calibrate_offset("s2", "s1",
                                anchors(("c1", "r1"), ("c2", "r1")))

    def test_actor_or_type_mismatch_rejected(self):
        make([
            ev("c1", "s2", "00", actor="alice", etype="login"),
            ev("r1", "s1", "00", actor="bob", etype="login"),
            ev("r2", "s1", "00", actor="alice", etype="logout"),
        ])
        with self.assertRaisesRegex(cb.CalibrationError, "同一动作"):
            cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        with self.assertRaisesRegex(cb.CalibrationError, "同一动作"):
            cb.calibrate_offset("s2", "s1", anchors(("c1", "r2")))

    def test_empty_actor_anchor_rejected(self):
        make([
            ev("c1", "s2", "00", actor=""),
            ev("r1", "s1", "00", actor="u"),
        ])
        with self.assertRaisesRegex(cb.CalibrationError, "同一动作"):
            cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))

    # --------------------------------------------------------- 只读 / 结构

    def test_read_only_no_side_effects(self):
        make([ev("c1", "s2", "00"), ev("r1", "s1", "01")])
        r1 = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        r2 = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertEqual(r1["offset_delta_ms"], r2["offset_delta_ms"])
        self.assertEqual(r1["midpoint_ms"], r2["midpoint_ms"])

    def test_response_shape(self):
        make([
            ev("c1", "s2", "00", uncertainty=0.1, summary="cal"),
            ev("r1", "s1", "01", uncertainty=0.2, summary="ref"),
        ])
        r = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        self.assertEqual(r["calibration_type"], "anchor-offset-calibration")
        self.assertEqual(r["calibrated_source"]["id"], "s2")
        self.assertEqual(r["reference_source"]["id"], "s1")
        self.assertEqual(r["conflict"], None)
        for key in ("rule", "interval_reuse", "fixed_params", "rounding",
                    "adopt", "max_anchors"):
            self.assertIn(key, r["calibration"])
        a = r["anchors"][0]
        for key in ("index", "calibrated", "reference", "actor", "event_type",
                    "delta_lo_ms", "delta_hi_ms", "delta_range_ms",
                    "delta_range_text", "current_closed_gap_ms",
                    "current_mid_delta_ms", "determines"):
            self.assertIn(key, a)
        for rec in (a["calibrated"], a["reference"]):
            for key in ("id", "source_id", "source_name", "event_type", "actor",
                        "summary", "lo_ms", "hi_ms", "mid_ms",
                        "lo_text", "hi_text", "mid_text"):
                self.assertIn(key, rec)
        self.assertIsInstance(a["delta_lo_ms"], int)
        self.assertIsInstance(a["delta_hi_ms"], int)

    def test_20_anchors_accepted(self):
        events = []
        pairs = []
        for i in range(20):
            events.append(ev(f"c{i:02d}", "s2", f"{i:02d}.000"))
            events.append(ev(f"r{i:02d}", "s1", f"{i:02d}.000"))
            pairs.append((f"c{i:02d}", f"r{i:02d}"))
        make(events)
        r = cb.calibrate_offset("s2", "s1", anchors(*pairs))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["anchor_count"], 20)
        self.assertEqual(r["offset_delta_ms"], [0, 0])

    def test_reference_direction_swapped(self):
        # 交换待校准/参照方向：δ 范围取反
        make([ev("c1", "s2", "00"), ev("r1", "s1", "01", uncertainty=1)])
        r1 = cb.calibrate_offset("s2", "s1", anchors(("c1", "r1")))
        # s2 作待校准：r1 区间 [0,2] ⇒ δ ∈ [−2, 0]
        self.assertEqual(r1["offset_delta_ms"], [-2000, 0])
        r2 = cb.calibrate_offset("s1", "s2", anchors(("r1", "c1")))
        # s1 作待校准：δ′ = −δ ⇒ [0, 2]
        self.assertTrue(r2["feasible"])
        self.assertEqual(r2["offset_delta_ms"], [0, 2000])


if __name__ == "__main__":
    unittest.main(verbosity=2)
