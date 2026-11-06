"""异常检测测试。

分三层：
* **纯函数**（特征向量 / 归一化 / 稳健 z 分）用 ``SimpleTestCase``，不碰数据库；
* **离群识别**用真实作答数据，验证极端作答确实被判为离群；
* **降级路径**用 mock 让孤立森林初始化失败，验证「未安装依赖也能用」。
"""

from unittest import mock

from django.test import SimpleTestCase

from apps.exam.constants import BehaviorEvent
from apps.proctoring.constants import ANOMALY_FEATURES, ANOMALY_MIN_SAMPLES
from apps.proctoring.services import AnomalyDetectionService

from .base import ProctoringBaseTest


class PureFunctionTest(SimpleTestCase):
    """不依赖数据库的算法零件。"""

    def test_feature_vector_order_matches_constants(self):
        metrics = {
            "switch_count": 1, "paste_count": 2, "fullscreen_exit_count": 3,
            "max_absence_seconds": 4, "event_per_minute": 5.0,
            "seconds_per_question": 6.0, "ip_count": 7, "distinct_user_agents": 2,
            "burst_max": 8,
        }
        vector = AnomalyDetectionService.feature_vector(metrics)
        self.assertEqual(len(vector), len(ANOMALY_FEATURES))
        # device_changed 是布尔特征，被折成 0/1，其余按同名字段取值
        self.assertEqual(vector[:7], [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        self.assertEqual(vector[7], 1.0)
        self.assertEqual(vector[8], 8.0)

    def test_feature_vector_tolerates_missing_keys(self):
        vector = AnomalyDetectionService.feature_vector({})
        self.assertEqual(vector, [0.0] * len(ANOMALY_FEATURES))

    def test_normalize_maps_to_unit_range(self):
        normalized = AnomalyDetectionService._normalize([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(normalized[0], 0.0)
        self.assertEqual(normalized[-1], 1.0)

    def test_normalize_identical_values_is_all_zero(self):
        self.assertEqual(AnomalyDetectionService._normalize([2.0, 2.0, 2.0]), [0.0, 0.0, 0.0])

    def test_normalize_empty(self):
        self.assertEqual(AnomalyDetectionService._normalize([]), [])

    def test_robust_zscore_flags_extreme(self):
        vectors = [[0.0, 0.0]] * 9 + [[50.0, 0.0]]
        max_zs, top_indexes = AnomalyDetectionService._robust_zscore(vectors)
        self.assertEqual(top_indexes[-1], 0)          # 最突出的是第 0 个特征
        self.assertGreater(max_zs[-1], max_zs[0])

    def test_robust_zscore_identical_columns_are_zero(self):
        vectors = [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]]
        max_zs, _ = AnomalyDetectionService._robust_zscore(vectors)
        self.assertEqual(max_zs, [0.0, 0.0, 0.0])

    def test_robust_zscore_empty(self):
        self.assertEqual(AnomalyDetectionService._robust_zscore([]), ([], []))


class InsufficientSampleTest(ProctoringBaseTest):
    """样本不足就不给结论。"""

    def test_too_few_attempts_returns_unavailable(self):
        exam, _ = self.make_exam_with_attempts(ANOMALY_MIN_SAMPLES - 3)
        result = AnomalyDetectionService.detect(exam)
        self.assertFalse(result["available"])
        self.assertEqual(result["results"], [])
        self.assertIn("样本不足", result["reason"])
        self.assertEqual(result["required_samples"], ANOMALY_MIN_SAMPLES)

    def test_exactly_required_samples_is_available(self):
        exam, _ = self.make_exam_with_attempts(ANOMALY_MIN_SAMPLES)
        result = AnomalyDetectionService.detect(exam)
        self.assertTrue(result["available"])
        self.assertEqual(len(result["results"]), ANOMALY_MIN_SAMPLES)


class OutlierDetectionTest(ProctoringBaseTest):
    """有足够样本时能挑出极端作答。"""

    def _build(self, normal=10):
        return self.make_exam_with_outlier(normal)

    def test_extreme_attempt_is_most_anomalous(self):
        exam, _, extreme = self._build()
        result = AnomalyDetectionService.detect(exam)
        self.assertTrue(result["available"])
        top = max(result["results"], key=lambda row: row["anomaly_score"])
        self.assertEqual(top["attempt_id"], extreme.id)
        self.assertGreater(top["anomaly_score"], 0.5)

    def test_extreme_attempt_is_flagged_outlier(self):
        exam, _, extreme = self._build()
        result = AnomalyDetectionService.detect(exam)
        by_id = {row["attempt_id"]: row for row in result["results"]}
        self.assertTrue(by_id[extreme.id]["is_outlier"])
        self.assertGreaterEqual(result["outlier_count"], 1)

    def test_scores_within_unit_range(self):
        exam, _, _ = self._build()
        result = AnomalyDetectionService.detect(exam)
        for row in result["results"]:
            self.assertGreaterEqual(row["anomaly_score"], 0.0)
            self.assertLessEqual(row["anomaly_score"], 1.0)

    def test_results_include_top_feature(self):
        exam, _, _ = self._build()
        result = AnomalyDetectionService.detect(exam)
        self.assertTrue(all("top_feature" in row for row in result["results"]))

    def test_detection_is_deterministic(self):
        exam, _, _ = self._build()
        first = AnomalyDetectionService.detect(exam)
        second = AnomalyDetectionService.detect(exam)
        self.assertEqual(
            [row["anomaly_score"] for row in first["results"]],
            [row["anomaly_score"] for row in second["results"]],
        )
        self.assertEqual([row["is_outlier"] for row in first["results"]],
                         [row["is_outlier"] for row in second["results"]])

    def test_method_is_reported(self):
        exam, _, _ = self._build()
        result = AnomalyDetectionService.detect(exam)
        self.assertIn(result["method"],
                      (AnomalyDetectionService.METHOD_FOREST, AnomalyDetectionService.METHOD_ZSCORE))
        self.assertTrue(result["method_display"])


class DegradationTest(ProctoringBaseTest):
    """孤立森林不可用时降级为稳健 z-score，功能不中断。"""

    def test_falls_back_when_model_fails(self):
        exam, _, extreme = self.make_exam_with_outlier()
        with mock.patch("sklearn.ensemble.IsolationForest",
                        side_effect=RuntimeError("依赖不可用")):
            result = AnomalyDetectionService.detect(exam)
        self.assertTrue(result["available"])
        self.assertEqual(result["method"], AnomalyDetectionService.METHOD_ZSCORE)
        self.assertIn("z-score", result["method_display"])
        self.assertTrue(result["reason"])   # 如实说明已降级
        # 降级后依然能挑出极端作答
        top = max(result["results"], key=lambda row: row["anomaly_score"])
        self.assertEqual(top["attempt_id"], extreme.id)

    def test_available_reflects_importability(self):
        try:
            import sklearn.ensemble  # noqa: F401
            importable = True
        except Exception:  # noqa: BLE001
            importable = False
        self.assertEqual(AnomalyDetectionService.available(), importable)
