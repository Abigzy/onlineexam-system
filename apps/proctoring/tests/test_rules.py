"""规则引擎测试。

每条规则都单独立一个用例，验「命中 / 不命中 / 可解释」三件事：
可疑信号必须带一句话理由与证据数字——教师看到红色标记要能点开看到「因为什么」。
"""

from apps.exam.constants import BehaviorEvent
from apps.proctoring.constants import RULE_CATALOG, RULE_CODES, RiskLevel, level_for
from apps.proctoring.services import RuleEngineService

from .base import ProctoringBaseTest


def codes_of(evaluation) -> set:
    return set(evaluation["signal_codes"])


class MetricTest(ProctoringBaseTest):
    """指标提炼：埋点优先、聚合计数兜底，两边取 max。"""

    def test_metrics_have_expected_keys(self):
        attempt = self.make_attempt_raw(self.exam)
        metrics = RuleEngineService.build_metrics(attempt)
        for key in ("switch_count", "switch_limit", "switch_per_minute", "paste_count",
                    "fullscreen_exit_count", "max_absence_seconds", "burst_max",
                    "ip_count", "distinct_user_agents", "duration_minutes",
                    "answered_count", "seconds_per_question", "log_count"):
            self.assertIn(key, metrics)

    def test_logs_win_when_larger_than_attempt_counts(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=2)
        self.add_log(attempt, BehaviorEvent.SWITCH, count=5)
        metrics = RuleEngineService.build_metrics(attempt)
        self.assertEqual(metrics["switch_count"], 5)

    def test_attempt_counts_win_when_no_logs(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=7)
        metrics = RuleEngineService.build_metrics(attempt)
        self.assertEqual(metrics["switch_count"], 7)

    def test_max_absence_from_logs(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_log(attempt, BehaviorEvent.SWITCH, duration=90)
        self.add_log(attempt, BehaviorEvent.BLUR, duration=30)
        metrics = RuleEngineService.build_metrics(attempt)
        self.assertEqual(metrics["max_absence_seconds"], 90)

    def test_burst_uses_sliding_window(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_logs(attempt, BehaviorEvent.SWITCH, 4)   # 4 条挤在 4 秒内
        metrics = RuleEngineService.build_metrics(attempt)
        self.assertGreaterEqual(metrics["burst_max"], 4)


class RuleHitTest(ProctoringBaseTest):
    """逐条规则：命中与不命中。"""

    def test_no_behavior_is_normal(self):
        attempt = self.make_attempt_raw(self.exam)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertEqual(evaluation["score"], 0)
        self.assertEqual(evaluation["level"], RiskLevel.NORMAL)
        self.assertFalse(evaluation["is_suspicious"])
        self.assertTrue(evaluation["insufficient"])

    def test_switch_over_limit(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=5)   # 上限 3
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("switch_over_limit", codes_of(evaluation))
        self.assertGreaterEqual(evaluation["risk_order"], 2)          # 中度及以上
        self.assertTrue(evaluation["is_suspicious"])

    def test_switch_at_limit_is_not_flagged(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=3)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertNotIn("switch_over_limit", codes_of(evaluation))
        self.assertEqual(evaluation["score"], 0)

    def test_switch_frequent(self):
        exam = self.make_exam(max_switch_count=100)   # 抬高上限，隔离「频率」这条规则
        attempt = self.make_attempt_raw(exam, minutes=3, switch_count=6, answered=10)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("switch_frequent", codes_of(evaluation))

    def test_long_absence(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_log(attempt, BehaviorEvent.SWITCH, duration=120)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("long_absence", codes_of(evaluation))

    def test_long_absence_escalates_to_severe(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_log(attempt, BehaviorEvent.SWITCH, duration=300)
        evaluation = RuleEngineService.evaluate(attempt)
        signal = next(item for item in evaluation["signals"] if item["code"] == "long_absence")
        self.assertEqual(signal["level"], RiskLevel.SEVERE)

    def test_paste(self):
        attempt = self.make_attempt_raw(self.exam, paste_count=1)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("paste_content", codes_of(evaluation))

    def test_fullscreen_exit(self):
        attempt = self.make_attempt_raw(self.exam, fullscreen_exit_count=2)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("fullscreen_exit", codes_of(evaluation))

    def test_answer_too_fast(self):
        attempt = self.make_attempt_raw(self.exam, minutes=1, answered=20)   # 3 秒/题
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("answer_too_fast", codes_of(evaluation))

    def test_answer_too_fast_needs_min_questions(self):
        attempt = self.make_attempt_raw(self.exam, minutes=1, answered=2)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertNotIn("answer_too_fast", codes_of(evaluation))

    def test_ip_changed(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_log(attempt, BehaviorEvent.SWITCH, ip="10.0.0.1")
        self.add_log(attempt, BehaviorEvent.SWITCH, ip="10.0.0.2")
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("ip_changed", codes_of(evaluation))

    def test_device_changed(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_log(attempt, BehaviorEvent.SWITCH, user_agent="ua-A")
        self.add_log(attempt, BehaviorEvent.SWITCH, user_agent="ua-B")
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("device_changed", codes_of(evaluation))

    def test_event_burst(self):
        attempt = self.make_attempt_raw(self.exam)
        self.add_logs(attempt, BehaviorEvent.SWITCH, 5)   # 5 条挤在 5 秒内
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertIn("event_burst", codes_of(evaluation))

    def test_score_is_capped_at_100(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=200, paste_count=50,
                                        fullscreen_exit_count=20)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertEqual(evaluation["score"], 100)
        self.assertEqual(evaluation["level"], RiskLevel.SEVERE)


class ExplainabilityTest(ProctoringBaseTest):
    """可解释性：每条信号都要能回答「因为什么」。"""

    def test_every_signal_has_reason_and_evidence(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=5, paste_count=2)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertTrue(evaluation["signals"])
        for signal in evaluation["signals"]:
            self.assertTrue(signal["name"])
            self.assertTrue(signal["reason"])
            self.assertTrue(signal["evidence"])
            self.assertGreater(signal["points"], 0)
            self.assertIn(signal["code"], RULE_CODES)

    def test_signals_sorted_by_points_desc(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=9, paste_count=1)
        points = [signal["points"] for signal in RuleEngineService.evaluate(attempt)["signals"]]
        self.assertEqual(points, sorted(points, reverse=True))

    def test_reasons_mirror_signals(self):
        attempt = self.make_attempt_raw(self.exam, switch_count=9)
        evaluation = RuleEngineService.evaluate(attempt)
        self.assertEqual(evaluation["reasons"],
                         [signal["reason"] for signal in evaluation["signals"]])

    def test_rule_catalog_is_wellformed(self):
        keys = {"code", "name", "metric", "points", "level", "threshold_desc", "advice"}
        codes = [rule["code"] for rule in RULE_CATALOG]
        self.assertEqual(len(codes), len(set(codes)), "规则 code 不能重复")
        for rule in RULE_CATALOG:
            self.assertTrue(keys.issubset(rule.keys()))
            self.assertIn(rule["level"], RiskLevel.values)


class RiskLevelTest(ProctoringBaseTest):
    """分档边界。"""

    def test_level_bands(self):
        self.assertEqual(level_for(0), RiskLevel.NORMAL)
        self.assertEqual(level_for(1), RiskLevel.LOW)
        self.assertEqual(level_for(30), RiskLevel.MEDIUM)
        self.assertEqual(level_for(55), RiskLevel.HIGH)
        self.assertEqual(level_for(80), RiskLevel.SEVERE)
        self.assertEqual(level_for(999), RiskLevel.SEVERE)

    def test_suspicious_starts_at_medium(self):
        low = self.make_attempt_raw(self.exam)
        self.add_log(low, BehaviorEvent.SWITCH, duration=0)   # 一条普通切屏，不触发规则
        self.assertFalse(RuleEngineService.evaluate(low)["is_suspicious"])
