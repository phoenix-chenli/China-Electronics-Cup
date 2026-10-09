"""Numeric counter-checks: the headline figure must survive an independent re-derivation.

The aggregate is produced by one SQL expression. These tests confirm the same
number is reproduced from the contribution rows with Decimal arithmetic, and
that a mismatch blocks the conclusion instead of being published as verified.
"""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="evidenceflow-counter-")
os.environ["EVIDENCEFLOW_DATA_DIR"] = TEMP.name

from backend import counter_check, engine, store as s
from backend.seed import seed_business, write_seed_docs


def detail_evidence(result):
    return next(e for e in result["evidence"] if e["type"] == "sql_result")


class CounterCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The bonus branch cross-checks the rule document, so the isolated
        # workspace needs the seeded corpora indexed too.
        s.init(); seed_business(); write_seed_docs()
        for path in sorted(s.DOCS.iterdir()):
            if path.is_file(): s.index_document(path)
    @classmethod
    def tearDownClass(cls):
        TEMP.cleanup()

    def total(self):
        return engine.execute("2024 年华东净销售额是多少？", persist=False)

    def test_aggregate_total_is_independently_reproduced(self):
        result = self.total()
        check = (result.get("counter_checks") or [None])[0]
        self.assertIsNotNone(check, "汇总结果必须附带独立复算记录")
        self.assertEqual(check["status"], "passed")
        self.assertEqual(check["method"], "明细求和")
        self.assertIn("Decimal", check["detail"])
        self.assertEqual(result["claims"][0]["status"], "verified")
        self.assertEqual(result["claims"][0]["counter_check"]["status"], "passed")
        self.assertEqual(result["coverage"]["supported"], result["coverage"]["total"])

    def test_mismatched_aggregate_is_rejected(self):
        result = self.total()
        tampered = copy.deepcopy(result["rows"]); tampered[0]["net_sales"] = 999999.99
        check = counter_check.verify(result["query_spec"], tampered, {"locator": detail_evidence(result)["locator"]})
        self.assertEqual(check["status"], "failed")
        self.assertIn("已阻止发布", check["detail"])
        self.assertEqual(check["values"]["detail"], "1740000.00")

    def test_mismatched_order_count_is_rejected(self):
        result = self.total()
        tampered = copy.deepcopy(result["rows"]); tampered[0]["order_count"] = 7
        check = counter_check.verify(result["query_spec"], tampered, {"locator": detail_evidence(result)["locator"]})
        self.assertEqual(check["status"], "failed")
        self.assertIn("订单数", check["detail"])

    def test_count_metric_is_reproduced_by_distinct_detail(self):
        result = engine.execute("2024 年华北有多少客户？", persist=False)
        check = (result.get("counter_checks") or [None])[0]
        self.assertEqual(check["status"], "passed")
        self.assertEqual(check["method"], "明细去重")
        self.assertEqual(result["claims"][0]["status"], "verified")
        tampered = copy.deepcopy(result["rows"]); tampered[0]["customer_count"] = 5
        check = counter_check.verify(result["query_spec"], tampered, {"locator": detail_evidence(result)["locator"]})
        self.assertEqual(check["status"], "failed")

    def test_growth_periods_are_rederived(self):
        result = engine.execute("2025 年华东净销售额比 2024 年增长多少？", persist=False)
        check = (result.get("counter_checks") or [None])[0]
        self.assertEqual(check["status"], "passed")
        self.assertEqual(check["method"], "分期求和")
        self.assertEqual(set(check["values"]), {"current", "compare"})

    def test_grouped_ranking_conserves_the_detail_total(self):
        result = engine.execute("2024 年华东净销售额最高的产品是什么？", persist=False)
        check = (result.get("counter_checks") or [None])[0]
        self.assertEqual(check["status"], "passed")
        self.assertEqual(check["method"], "守恒检查")
        tampered = copy.deepcopy(result["rows"]); tampered[0]["net_sales"] = 1.0
        check = counter_check.verify(result["query_spec"], tampered, {"locator": detail_evidence(result)["locator"]})
        self.assertEqual(check["status"], "failed")
        self.assertIn("守恒", check["method"])

    def test_failed_counter_check_blocks_the_conclusion(self):
        failed = {"method": "明细求和", "status": "failed", "detail": "注入的不一致", "values": {}}
        with patch.object(counter_check, "verify", return_value=failed):
            result = engine.execute("2024 年华东净销售额是多少？", persist=False)
        statuses = [c["status"] for c in result["claims"]]
        self.assertIn("needs_evidence", statuses)
        self.assertNotIn("verified", statuses)
        self.assertEqual(result["coverage"]["supported"], 0)
        self.assertTrue(any("注入的不一致" in c["text"] for c in result["claims"]))

    def test_formula_claims_still_recompute_parameters(self):
        result = engine.execute("2024 年华东净销售额是多少？它的奖金是多少？", persist=False)
        formula = next(c for c in result["claims"] if c["kind"] == "formula")
        self.assertEqual(formula["status"], "verified")


if __name__ == "__main__":
    unittest.main()
