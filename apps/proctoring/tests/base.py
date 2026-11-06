"""作弊行为检测测试的公共装配。

继承 ``AnalyticsBaseTest``（它已经带好六种题型 + 真实判分链路 + 父子知识点），
在上面补两样模块八专属的工具：

1. **精确造行为埋点** —— 规则引擎读的是埋点，要验「切屏 5 次会不会触发」
   就必须能精确摆出这 5 次，而不是靠前端模拟。
2. **直接造作答记录** —— 异常检测需要「一场考试里 ≥8 份作答」，
   走真实开考链路要建十几个学生、发十几份卷子，慢且噪音大。
   这里直接写 ``ExamAttempt``：被测的是离群算法，不是开考流程（开考在模块四测过了）。
"""

from datetime import timedelta

from django.utils import timezone

from apps.analytics.tests.base import AnalyticsBaseTest
from apps.common.constants import AttemptStatus
from apps.exam.constants import BehaviorEvent
from apps.proctoring.models import ExamBehaviorLog
from apps.users.models import User

PASSWORD = "Exam@123456"


class ProctoringBaseTest(AnalyticsBaseTest):
    """作弊行为检测公共装配。"""

    PASSWORD = PASSWORD

    # ------------------------------------------------------------------
    # 造埋点
    # ------------------------------------------------------------------
    def add_log(self, attempt, event_type, *, count=1, duration=0, occurred_at=None,
                ip="", user_agent="", question=None, question_index=0, detail=""):
        """往一次作答上加一条行为埋点。"""
        return ExamBehaviorLog.objects.create(
            attempt=attempt,
            exam=attempt.exam,
            student_id=attempt.student_id,
            event_type=event_type,
            count=count,
            occurred_at=occurred_at or timezone.now(),
            duration_seconds=duration,
            question=question,
            question_index=question_index,
            ip_address=ip or (attempt.ip_address or ""),
            user_agent=user_agent or (attempt.user_agent or ""),
            detail=detail,
        )

    def add_logs(self, attempt, event_type, times, **kwargs):
        """连加多条同类型埋点（造「频繁」「突发」场景用）。"""
        now = timezone.now()
        return [
            self.add_log(attempt, event_type,
                         occurred_at=now - timedelta(seconds=offset), **kwargs)
            for offset in range(times)
        ]

    # ------------------------------------------------------------------
    # 造作答（不走开考链路，直接写库）
    # ------------------------------------------------------------------
    def make_attempt_raw(self, exam, student=None, *, status=AttemptStatus.SUBMITTED,
                         minutes=30, answered=10, question_count=10,
                         switch_count=0, paste_count=0, fullscreen_exit_count=0,
                         ip="127.0.0.1", user_agent="proctor-agent", **extra):
        """直接造一条作答记录，精确控制聚合计数与用时。"""
        from apps.exam.models import ExamAttempt

        now = timezone.now()
        in_progress = status == AttemptStatus.IN_PROGRESS
        return ExamAttempt.objects.create(
            exam=exam,
            student=student or self.student,
            status=status,
            started_at=now - timedelta(minutes=minutes),
            deadline_at=now + timedelta(minutes=60),
            submitted_at=None if in_progress else now,
            time_spent_seconds=minutes * 60,
            answered_count=answered,
            question_count=question_count,
            switch_count=switch_count,
            paste_count=paste_count,
            fullscreen_exit_count=fullscreen_exit_count,
            ip_address=ip,
            user_agent=user_agent,
            **extra,
        )

    def make_students(self, count, *, prefix="p_stu"):
        """造一批学生（异常检测需要一场考试里有足够多的作答）。"""
        users = []
        for index in range(count):
            users.append(User.objects.create_user(
                username=f"{prefix}_{index}",
                email=f"{prefix}_{index}@example.com",
                password=PASSWORD,
                role="student",
                real_name=f"考生{index:02d}",
                student_no=f"P{index:04d}",
            ))
        return users

    def make_exam_with_attempts(self, count, *, exam=None, **attempt_kwargs):
        """造一场考试 + ``count`` 份作答（每份来自不同学生），返回 ``(exam, attempts)``。"""
        exam = exam or self.make_exam()
        students = self.make_students(count)
        attempts = [self.make_attempt_raw(exam, student, **attempt_kwargs)
                    for student in students]
        return exam, attempts

    def make_exam_with_outlier(self, normal=10):
        """造一场考试：``normal`` 份「正常」作答 + 1 份「极端」作答。

        用于异常检测：只有正常样本时模型学不出离群，必须有一个明显的异类。
        返回 ``(exam, attempts, extreme)``。
        """
        exam = self.make_exam()
        students = self.make_students(normal + 1)
        attempts = [self.make_attempt_raw(exam, student) for student in students]
        extreme = attempts[-1]
        extreme.switch_count = 40
        extreme.paste_count = 8
        extreme.fullscreen_exit_count = 9
        extreme.save(update_fields=["switch_count", "paste_count",
                                    "fullscreen_exit_count", "updated_at"])
        self.add_log(extreme, BehaviorEvent.SWITCH, duration=600)
        return exam, attempts, extreme


__all__ = ["PASSWORD", "ProctoringBaseTest"]
