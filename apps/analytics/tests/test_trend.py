"""成绩趋势与线性回归测试。

回归是纯函数，边界直接测；趋势是聚合，重点验**顺序、班级均值、预测夹取**。
"""

from apps.analytics.constants import TREND_MIN_POINTS
from apps.analytics.services import TrendService
from apps.common.constants import QuestionType

from .base import AnalyticsBaseTest

FULL_TYPES = {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE,
              QuestionType.FILL, QuestionType.SHORT, QuestionType.ESSAY}


class LinearFitTest(AnalyticsBaseTest):
    """最小二乘拟合（纯函数）。"""

    def test_upward_trend(self):
        fit = TrendService.linear_fit([0.2, 0.4, 0.6, 0.8])
        self.assertGreater(fit["slope"], 0)
        self.assertEqual(fit["direction"], "up")
        self.assertTrue(fit["enough_points"])
        # 完全线性 → 拟合优度 1
        self.assertAlmostEqual(fit["r2"], 1.0, places=4)
        self.assertAlmostEqual(fit["predict_next"], 1.0, places=4)

    def test_downward_trend(self):
        fit = TrendService.linear_fit([0.9, 0.7, 0.5, 0.3])
        self.assertLess(fit["slope"], 0)
        self.assertEqual(fit["direction"], "down")
        self.assertAlmostEqual(fit["predict_next"], 0.1, places=4)

    def test_flat_trend(self):
        fit = TrendService.linear_fit([0.60, 0.601, 0.599, 0.600])
        self.assertEqual(fit["direction"], "flat")
        self.assertAlmostEqual(fit["predict_next"], 0.6, places=2)

    def test_not_enough_points(self):
        """点数不足时不做预测——宁可不给结论，也不给错误结论。"""
        fit = TrendService.linear_fit([0.3, 0.6])
        self.assertFalse(fit["enough_points"])
        self.assertIsNone(fit["predict_next"])
        self.assertEqual(fit["direction"], "up")
        self.assertEqual(TREND_MIN_POINTS, 3)

    def test_single_point(self):
        fit = TrendService.linear_fit([0.5])
        self.assertEqual(fit["n"], 1)
        self.assertIsNone(fit["slope"])
        self.assertEqual(fit["direction"], "unknown")

    def test_prediction_is_clamped(self):
        """陡峭上升的序列外推会超过 1，必须夹回 [0, 1]（得分率不可能大于 100%）。"""
        fit = TrendService.linear_fit([0.5, 0.8, 1.0, 1.0, 1.0, 1.0])
        self.assertLessEqual(fit["predict_next"], 1.0)
        self.assertGreaterEqual(fit["predict_next"], 0.0)

    def test_empty_series(self):
        fit = TrendService.linear_fit([])
        self.assertEqual(fit["n"], 0)
        self.assertIsNone(fit["slope"])


class StudentTrendTest(AnalyticsBaseTest):

    def setUp(self):
        super().setUp()
        # 三场考试，得分率递增；同时让考生乙每场都只答对判断题，制造班级均线差异
        self.exams = []
        for plan in ({QuestionType.SINGLE},
                     {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE},
                     FULL_TYPES):
            exam = self.make_exam()
            self.judged_attempt(exam, student=self.student, correct_types=plan)
            self.judged_attempt(exam, student=self.student_b,
                                correct_types={QuestionType.JUDGE})
            self.exams.append(exam)

    def test_points_are_ordered_and_ascending(self):
        data = TrendService.student_trend(self.student)
        points = data["points"]

        self.assertEqual(len(points), 3)
        self.assertEqual([row["index"] for row in points], [1, 2, 3])
        ratios = [row["ratio"] for row in points]
        self.assertEqual(ratios, sorted(ratios))
        self.assertEqual(data["fit"]["direction"], "up")
        self.assertEqual(data["summary"]["direction"], "up")

    def test_class_average_and_delta(self):
        points = TrendService.student_trend(self.student)["points"]
        for row in points:
            self.assertIsNotNone(row["class_avg_ratio"])
            # 该生每场都高于只答对判断题的考生乙 → 与班级均值之差为正
            self.assertGreater(row["delta_vs_class"], 0)
            # 三个字段各自 round(4)，差值与再相减最多差一个最小单位，用 1e-3 容差
            self.assertAlmostEqual(
                row["delta_vs_class"], row["ratio"] - row["class_avg_ratio"], places=3,
            )

    def test_limit_keeps_latest(self):
        """``limit`` 取的是**最近** N 次，不是最早 N 次。"""
        data = TrendService.student_trend(self.student, limit=2)
        self.assertEqual(len(data["points"]), 2)
        latest_exams = [self.exams[1].id, self.exams[2].id]
        self.assertEqual([row["exam_id"] for row in data["points"]], latest_exams)

    def test_summary_fields(self):
        summary = TrendService.student_trend(self.student)["summary"]
        self.assertEqual(summary["count"], 3)
        self.assertIsNotNone(summary["avg_percent"])
        self.assertGreater(summary["change"], 0)
        self.assertEqual(summary["worst"]["percent"], 8.7)

    def test_subject_filter_and_empty(self):
        data = TrendService.student_trend(self.student, subject="不存在的学科")
        self.assertEqual(data["points"], [])
        self.assertEqual(data["fit"]["direction"], "unknown")
        self.assertIsNone(data["summary"]["avg_percent"])

    def test_points_carry_exam_meta(self):
        row = TrendService.student_trend(self.student)["points"][0]
        for key in ("attempt_id", "exam_id", "exam_title", "submitted_at",
                    "score", "total_score", "ratio", "percent", "passed"):
            self.assertIn(key, row)
        self.assertFalse(row["is_timeout"])


__all__ = ["LinearFitTest", "StudentTrendTest"]
