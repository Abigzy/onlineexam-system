"""行为埋点写入测试。

重点验三件事：
1. 逐条落库、类型与条数正确（心跳不入库）；
2. **与模块四的聚合计数对得上**（``switch + blur`` 的埋点条数 == ``ExamAttempt.switch_count``）；
3. 脏数据、关闭检测开关、埋点失败都不能打断考试链路。
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.exam.constants import BehaviorEvent
from apps.exam.services import ExamAttemptService
from apps.proctoring.constants import ANALYZED_EVENT_TYPES
from apps.proctoring.models import ExamBehaviorLog
from apps.proctoring.services import BehaviorLogService

from .base import ProctoringBaseTest


class RecordEventsTest(ProctoringBaseTest):
    """BehaviorLogService.record_events 的单元行为。"""

    def setUp(self):
        self.attempt = self.make_attempt_raw(self.exam)

    def test_records_events_and_counts_by_type(self):
        result = BehaviorLogService.record_events(self.attempt, [
            {"type": BehaviorEvent.SWITCH, "count": 2},
            {"type": BehaviorEvent.PASTE, "count": 1},
        ])
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["by_type"][BehaviorEvent.SWITCH], 2)
        self.assertEqual(ExamBehaviorLog.objects.count(), 2)

    def test_heartbeat_and_unknown_events_are_skipped(self):
        result = BehaviorLogService.record_events(self.attempt, [
            {"type": BehaviorEvent.HEARTBEAT},
            {"type": "no-such-type"},
            "not-a-dict",
        ])
        self.assertEqual(result["created"], 0)
        self.assertEqual(ExamBehaviorLog.objects.count(), 0)

    def test_blur_is_kept_as_its_own_type(self):
        BehaviorLogService.record_events(self.attempt, [{"type": BehaviorEvent.BLUR}])
        log = ExamBehaviorLog.objects.get()
        self.assertEqual(log.event_type, BehaviorEvent.BLUR)

    def test_dirty_values_do_not_break(self):
        result = BehaviorLogService.record_events(self.attempt, [
            {"type": BehaviorEvent.SWITCH, "count": "abc", "duration_seconds": "x",
             "occurred_at": "不是时间", "question_index": None},
        ])
        self.assertEqual(result["created"], 1)
        log = ExamBehaviorLog.objects.get()
        self.assertEqual(log.count, 1)          # 非法 count 兜底为 1
        self.assertEqual(log.duration_seconds, 0)
        self.assertEqual(log.question_index, 0)
        self.assertIsNotNone(log.occurred_at)   # 非法时间兜底为服务端时间

    def test_explicit_occurred_at_is_used(self):
        stamp = timezone.now() - timedelta(minutes=5)
        BehaviorLogService.record_events(self.attempt, [
            {"type": BehaviorEvent.SWITCH, "occurred_at": stamp.isoformat()},
        ])
        log = ExamBehaviorLog.objects.get()
        self.assertLess(abs((log.occurred_at - stamp).total_seconds()), 1)

    def test_detail_is_serialized(self):
        BehaviorLogService.record_events(self.attempt, [
            {"type": BehaviorEvent.SWITCH, "detail": {"from": "tab", "to": "other"}},
        ])
        log = ExamBehaviorLog.objects.get()
        self.assertEqual(log.detail_dict()["from"], "tab")

    def test_detail_failure_returns_empty_dict(self):
        log = self.add_log(self.attempt, BehaviorEvent.SWITCH, detail="{不是 JSON")
        self.assertEqual(log.detail_dict(), {})


class ExamIntegrationTest(ProctoringBaseTest):
    """模块四 record_behavior → 模块八埋点 的联动。"""

    def test_record_behavior_writes_logs(self):
        attempt = self.start(self.exam)
        ExamAttemptService.record_behavior(attempt, [
            {"type": BehaviorEvent.SWITCH},
            {"type": BehaviorEvent.PASTE},
        ])
        attempt.refresh_from_db()
        self.assertEqual(ExamBehaviorLog.objects.filter(attempt=attempt).count(), 2)
        self.assertEqual(attempt.switch_count, 1)
        self.assertEqual(attempt.paste_count, 1)

    def test_logs_aggregate_matches_attempt_counters(self):
        """埋点聚合必须与 ExamAttempt 的聚合计数对得上（blur 也算切屏）。"""
        attempt = self.start(self.exam)
        ExamAttemptService.record_behavior(attempt, [
            {"type": BehaviorEvent.SWITCH, "count": 2},
            {"type": BehaviorEvent.BLUR, "count": 1},
            {"type": BehaviorEvent.PASTE, "count": 3},
            {"type": BehaviorEvent.FULLSCREEN_EXIT, "count": 1},
        ])
        attempt.refresh_from_db()
        logs = list(ExamBehaviorLog.objects.filter(attempt=attempt))
        switch_logged = sum(log.count for log in logs
                            if log.event_type in (BehaviorEvent.SWITCH, BehaviorEvent.BLUR))
        paste_logged = sum(log.count for log in logs if log.event_type == BehaviorEvent.PASTE)
        fullscreen_logged = sum(log.count for log in logs
                                if log.event_type == BehaviorEvent.FULLSCREEN_EXIT)
        self.assertEqual(switch_logged, attempt.switch_count)
        self.assertEqual(paste_logged, attempt.paste_count)
        self.assertEqual(fullscreen_logged, attempt.fullscreen_exit_count)

    def test_detection_switch_off_does_not_log(self):
        """关掉切屏检测后，事件既不计入计数也不落明细（两边口径一致）。"""
        exam = self.make_exam(detect_switch=False)
        attempt = self.start(exam)
        ExamAttemptService.record_behavior(attempt, [
            {"type": BehaviorEvent.SWITCH, "count": 3},
        ])
        attempt.refresh_from_db()
        self.assertEqual(attempt.switch_count, 0)
        self.assertEqual(ExamBehaviorLog.objects.filter(attempt=attempt).count(), 0)

    def test_detection_paste_off_does_not_log(self):
        exam = self.make_exam(detect_paste=False)
        attempt = self.start(exam)
        ExamAttemptService.record_behavior(attempt, [
            {"type": BehaviorEvent.PASTE, "count": 2},
        ])
        self.assertEqual(ExamBehaviorLog.objects.filter(attempt=attempt).count(), 0)

    def test_heartbeat_is_not_logged(self):
        attempt = self.start(self.exam)
        ExamAttemptService.record_behavior(attempt, [{"type": BehaviorEvent.HEARTBEAT}])
        self.assertEqual(ExamBehaviorLog.objects.filter(attempt=attempt).count(), 0)

    def test_logging_failure_does_not_break_reporting(self):
        """埋点写入抛异常时，不影响考试链路的计数与返回。"""
        from unittest import mock

        attempt = self.start(self.exam)
        with mock.patch.object(BehaviorLogService, "record_events",
                               side_effect=RuntimeError("埋点炸了")):
            result = ExamAttemptService.record_behavior(attempt, [
                {"type": BehaviorEvent.SWITCH, "count": 2},
            ])
        attempt.refresh_from_db()
        self.assertEqual(result["switch_count"], 2)   # 计数照常
        self.assertEqual(attempt.switch_count, 2)
        self.assertEqual(ExamBehaviorLog.objects.filter(attempt=attempt).count(), 0)


class RetentionTest(ProctoringBaseTest):
    """保留策略（写多读少的数据需要裁剪）。"""

    def setUp(self):
        self.attempt = self.make_attempt_raw(self.exam)

    def test_purge_removes_only_old_logs(self):
        old = self.add_log(self.attempt, BehaviorEvent.SWITCH,
                           occurred_at=timezone.now() - timedelta(days=400))
        fresh = self.add_log(self.attempt, BehaviorEvent.SWITCH,
                             occurred_at=timezone.now() - timedelta(days=2))
        deleted = BehaviorLogService.purge_old_logs(180)
        self.assertEqual(deleted, 1)
        remaining = list(ExamBehaviorLog.objects.values_list("id", flat=True))
        self.assertEqual(remaining, [fresh.id])
        self.assertNotIn(old.id, remaining)

    def test_purge_dry_run_keeps_rows(self):
        self.add_log(self.attempt, BehaviorEvent.SWITCH,
                     occurred_at=timezone.now() - timedelta(days=400))
        count = BehaviorLogService.purge_old_logs(180, dry_run=True)
        self.assertEqual(count, 1)
        self.assertEqual(ExamBehaviorLog.objects.count(), 1)

    def test_purge_with_zero_days_is_noop(self):
        self.add_log(self.attempt, BehaviorEvent.SWITCH,
                     occurred_at=timezone.now() - timedelta(days=400))
        self.assertEqual(BehaviorLogService.purge_old_logs(0), 0)
        self.assertEqual(ExamBehaviorLog.objects.count(), 1)


class ConstantsTest(TestCase):
    """埋点口径的自我一致性。"""

    def test_heartbeat_not_in_analyzed_types(self):
        self.assertNotIn(BehaviorEvent.HEARTBEAT, ANALYZED_EVENT_TYPES)

    def test_analyzed_types_are_valid_events(self):
        for event_type in ANALYZED_EVENT_TYPES:
            self.assertIn(event_type, BehaviorEvent.values)
