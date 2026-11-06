"""考试服务层测试。

覆盖四块：
  * ``ExamService``            —— 创建 / 更新 / 删除 / 状态机 / 时间驱动流转
  * ``ExamAttemptService``     —— 开考 / 续考 / 下发（防答案泄漏）/ 暂存 / 计时 / 行为 / 交卷
  * ``ExamAnalysisService``    —— 统计与行为预警
  * 边界与异常                  —— 超时、宽限期、越权、次数上限

这一层是模块四的核心，所以测试密度刻意高于接口层。
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.utils import timezone

from apps.common.constants import AttemptStatus, ExamStatus, QuestionType
from apps.common.exceptions import BusinessError
from apps.exam.constants import SUBMIT_GRACE_SECONDS, SubmitReason
from apps.exam.models import Answer, Exam, ExamAttempt
from apps.exam.services import ExamAnalysisService, ExamAttemptService, ExamService
from apps.papers.models import PaperQuestion
from apps.users.models import User

from .base import PASSWORD, TOTAL_SCORE, ExamBaseTest


# ======================================================================
class ExamServiceTests(ExamBaseTest):
    """考试本体的写操作与状态机。"""

    def test_create_defaults(self):
        exam = self.make_exam(publish=False, duration_minutes=0)
        self.assertEqual(exam.status, ExamStatus.DRAFT)
        self.assertEqual(exam.created_by, self.teacher)
        # 时长 0 = 沿用试卷时长
        self.assertEqual(exam.effective_duration_minutes, exam.paper.duration_minutes)
        self.assertTrue(exam.open_to_all)

    def test_create_with_participants_turns_off_open_to_all(self):
        exam = ExamService.create(
            data={"title": "指定考生", "paper": self.make_paper(),
                  "participant_ids": [self.student.id]},
            user=self.teacher,
        )
        self.assertFalse(exam.open_to_all)
        self.assertEqual(exam.participants.count(), 1)
        self.assertTrue(exam.open_to(self.student))
        self.assertFalse(exam.open_to(self.student_b))

    def test_effective_pass_score_falls_back_to_paper(self):
        paper = self.make_paper(pass_score=Decimal("14"))
        exam = self.make_exam(publish=False, paper=paper, pass_score=None)
        self.assertEqual(float(exam.effective_pass_score), 14.0)
        self.assertEqual(float(exam.total_score), float(paper.total_score))

    def test_delete_without_attempt_ok(self):
        exam = self.make_exam(publish=False)
        ExamService.delete(exam)
        self.assertFalse(Exam.objects.filter(pk=exam.pk).exists())

    def test_delete_with_attempt_is_rejected(self):
        exam = self.make_exam()
        self.start(exam)
        with self.assertRaises(BusinessError):
            ExamService.delete(exam)

    # ------------------------------------------------------------------
    def test_publish_requires_questions(self):
        from apps.papers.services import PaperService

        empty = PaperService.create(data={"title": "空卷", "subject": "测试"},
                                    user=self.teacher)
        exam = self.make_exam(publish=False, paper=empty)
        with self.assertRaises(BusinessError) as ctx:
            ExamService.change_status(exam, ExamStatus.PUBLISHED)
        self.assertIn("没有题目", str(ctx.exception))

    def test_publish_rejects_draft_paper(self):
        paper = self.make_paper(publish=False)
        exam = self.make_exam(publish=False, paper=paper)
        with self.assertRaises(BusinessError) as ctx:
            ExamService.change_status(exam, ExamStatus.PUBLISHED)
        self.assertIn("草稿", str(ctx.exception))

    def test_publish_rejects_closed_scope_without_participants(self):
        exam = self.make_exam(publish=False, open_to_all=False, participant_ids=[])
        with self.assertRaises(BusinessError) as ctx:
            ExamService.change_status(exam, ExamStatus.PUBLISHED)
        self.assertIn("指定考生", str(ctx.exception))

    def test_publish_sets_published_at(self):
        exam = self.make_exam(publish=False)
        ExamService.change_status(exam, ExamStatus.PUBLISHED)
        self.assertIsNotNone(exam.published_at)

    def test_invalid_transition_rejected(self):
        exam = self.make_exam(publish=False)
        with self.assertRaises(BusinessError):
            ExamService.change_status(exam, ExamStatus.ONGOING)  # 草稿不能直接跳到进行中

    def test_archived_exam_hidden_from_default_list(self):
        exam = self.make_exam()
        ExamService.change_status(exam, ExamStatus.FINISHED)
        ExamService.change_status(exam, ExamStatus.ARCHIVED)
        self.assertNotIn(exam, Exam.objects.active())

    # ------------------------------------------------------------------
    def test_computed_status_by_time_window(self):
        now = timezone.now()
        exam = self.make_exam(publish=False,
                              start_time=now + timedelta(hours=1),
                              end_time=now + timedelta(hours=2))
        ExamService.change_status(exam, ExamStatus.PUBLISHED)
        self.assertEqual(exam.computed_status(now), ExamStatus.PUBLISHED)
        self.assertEqual(exam.computed_status(now + timedelta(hours=1, minutes=10)),
                         ExamStatus.ONGOING)
        self.assertEqual(exam.computed_status(now + timedelta(hours=3)),
                         ExamStatus.FINISHED)

    def test_is_open_respects_window(self):
        now = timezone.now()
        exam = self.make_exam(start_time=now + timedelta(hours=1))
        self.assertFalse(exam.is_open(now))
        self.assertTrue(exam.is_open(now + timedelta(hours=1, minutes=5)))

    def test_sync_status_writes_running_state(self):
        now = timezone.now()
        exam = self.make_exam(start_time=now - timedelta(minutes=5),
                              end_time=now + timedelta(hours=1))
        ExamService.sync_status(exam, now)
        exam.refresh_from_db()
        self.assertEqual(exam.status, ExamStatus.ONGOING)

    def test_cannot_change_paper_after_someone_started(self):
        exam = self.make_exam()
        self.start(exam)
        other = self.make_paper()
        with self.assertRaises(BusinessError):
            ExamService.update(exam, data={"paper": other})

    def test_update_rejects_end_before_start(self):
        exam = self.make_exam(publish=False)
        now = timezone.now()
        with self.assertRaises(BusinessError):
            ExamService.update(exam, data={"start_time": now,
                                           "end_time": now - timedelta(hours=1)})

    def test_available_for_returns_only_visible_and_in_scope(self):
        open_exam = self.make_exam()
        closed_exam = self.make_exam(publish=False, open_to_all=False,
                                     participant_ids=[self.student_b.id])
        ExamService.change_status(closed_exam, ExamStatus.PUBLISHED)
        self.make_exam(publish=False)  # 草稿，学生看不到

        visible = list(ExamService.available_for(self.student))
        self.assertIn(open_exam, visible)
        self.assertNotIn(closed_exam, visible)

        visible_b = list(ExamService.available_for(self.student_b))
        self.assertIn(closed_exam, visible_b)

    def test_open_to_all_teacher_exam_restricted_to_own_class(self):
        """教师创建的「面向全体」考试，只对本班学生开放；班外学生不可见也不可考。"""
        exam = self.make_exam()
        outsider = User.objects.create_user(username="e_out", password=PASSWORD, role="student")
        self.assertTrue(exam.open_to(self.student))
        self.assertFalse(exam.open_to(outsider))
        self.assertNotIn(exam, Exam.objects.for_student(outsider))
        self.assertIn(exam, Exam.objects.for_student(self.student))

    def test_open_to_all_admin_exam_visible_to_all(self):
        """管理员创建的「面向全体」考试，对全校学生开放。"""
        paper = self.make_paper()
        exam = ExamService.create(data={"title": f"考试-{uuid4().hex[:6]}", "paper": paper},
                                  user=self.admin)
        outsider = User.objects.create_user(username="e_out2", password=PASSWORD, role="student")
        self.assertTrue(exam.open_to(self.student))
        self.assertTrue(exam.open_to(outsider))


# ======================================================================
class ExamAttemptTests(ExamBaseTest):
    """作答全流程。"""

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()

    # ------------------------------------------------------------------
    # 开考
    # ------------------------------------------------------------------
    def test_start_creates_attempt_with_deadline(self):
        attempt = self.start(self.exam)
        self.assertEqual(attempt.status, AttemptStatus.IN_PROGRESS)
        self.assertEqual(attempt.question_count, 6)
        self.assertEqual(float(attempt.total_score_snapshot), TOTAL_SCORE)
        span = (attempt.deadline_at - attempt.started_at).total_seconds() / 60
        self.assertAlmostEqual(span, 60, delta=1)

    def test_deadline_capped_by_exam_end_time(self):
        now = timezone.now()
        exam = self.make_exam(start_time=now - timedelta(minutes=5),
                              end_time=now + timedelta(minutes=10),
                              duration_minutes=90)
        attempt = self.start(exam)
        # 时长 90 分钟，但考试 10 分钟后结束 → 必须以结束时间封顶
        self.assertLessEqual(attempt.deadline_at, exam.end_time)
        self.assertLess(attempt.remaining_seconds(), 11 * 60)

    def test_start_resumes_existing_attempt(self):
        first = self.start(self.exam)
        result = ExamAttemptService.start(exam=self.exam, student=self.student)
        self.assertTrue(result["resumed"])
        self.assertEqual(result["attempt"].id, first.id)
        self.assertEqual(ExamAttempt.objects.filter(exam=self.exam).count(), 1)

    def test_start_without_resume_closes_old_and_opens_new(self):
        exam = self.make_exam(allow_resume=False, max_attempts=2)
        first = self.start(exam)
        result = ExamAttemptService.start(exam=exam, student=self.student)
        self.assertFalse(result["resumed"])
        first.refresh_from_db()
        self.assertEqual(first.status, AttemptStatus.SUBMITTED)

    def test_start_fails_when_attempts_exhausted(self):
        exam = self.make_exam(max_attempts=1)
        attempt = self.start(exam)
        ExamAttemptService.submit(attempt)
        with self.assertRaises(BusinessError) as ctx:
            ExamAttemptService.start(exam=exam, student=self.student)
        self.assertIn("最多作答", str(ctx.exception))

    def test_start_fails_when_not_started_yet(self):
        exam = self.make_exam(start_time=timezone.now() + timedelta(hours=2))
        with self.assertRaises(BusinessError):
            self.start(exam)

    def test_start_fails_when_out_of_scope(self):
        exam = self.make_exam(open_to_all=False, participant_ids=[self.student_b.id])
        with self.assertRaises(BusinessError) as ctx:
            self.start(exam, student=self.student)
        self.assertEqual(ctx.exception.status_code, 403)

    # ------------------------------------------------------------------
    # 下发：防答案泄漏（本模块最关键的一组断言）
    # ------------------------------------------------------------------
    def test_payload_never_leaks_answers(self):
        attempt = self.start(self.exam)
        payload = ExamAttemptService.build_paper_payload(attempt)

        blob = str(payload)
        self.assertNotIn("reference_answer", blob)
        self.assertNotIn("analysis", blob)
        self.assertNotIn("is_correct", blob)

        for question in payload["questions"]:
            self.assertEqual(
                sorted(question.keys()),
                ["answer", "difficulty", "display_order", "number", "options",
                 "paper_question_id", "question_type", "score", "section_id", "stem",
                 "type_display"],
            )
            for option in question["options"]:
                self.assertEqual(sorted(option.keys()), ["content", "label"])

    def test_payload_exposes_question_id_only_as_paper_question(self):
        """下发只给卷内条目 id，不给题库题目 id（少一条可被利用的线索）。"""
        attempt = self.start(self.exam)
        payload = ExamAttemptService.build_paper_payload(attempt)
        question_ids = {q.id for q in PaperQuestion.objects.filter(paper=attempt.exam.paper)}
        for question in payload["questions"]:
            self.assertIn(question["paper_question_id"], question_ids)

    def test_payload_groups_by_section_and_keeps_flat_list(self):
        attempt = self.start(self.exam)
        payload = ExamAttemptService.build_paper_payload(attempt)
        self.assertEqual(len(payload["questions"]), 6)
        self.assertEqual(sum(len(s["questions"]) for s in payload["sections"]), 6)
        self.assertEqual(payload["paper"]["total_score"], float(TOTAL_SCORE))

    def test_payload_carries_remaining_and_config(self):
        attempt = self.start(self.exam)
        payload = ExamAttemptService.build_paper_payload(attempt)
        self.assertGreater(payload["remaining_seconds"], 0)
        self.assertTrue(payload["config"]["detect_switch"])
        self.assertEqual(payload["config"]["max_switch_count"], 3)
        self.assertTrue(payload["server_time"])
        self.assertTrue(payload["deadline_at"])

    def test_shuffle_is_deterministic_for_same_seed(self):
        exam = self.make_exam(shuffle_questions=True, shuffle_options=True)
        attempt = self.start(exam)
        first = [q["paper_question_id"] for q in
                 ExamAttemptService.build_paper_payload(attempt)["questions"]]
        second = [q["paper_question_id"] for q in
                  ExamAttemptService.build_paper_payload(attempt)["questions"]]
        self.assertEqual(first, second, "同一份作答刷新后题序必须一致")

    def test_shuffle_changes_order_for_different_seed(self):
        exam = self.make_exam(shuffle_questions=True)
        attempt = self.start(exam)
        base = [q["paper_question_id"] for q in
                ExamAttemptService.build_paper_payload(attempt)["questions"]]
        attempt.shuffle_seed = attempt.shuffle_seed + 7919
        attempt.save(update_fields=["shuffle_seed"])
        shuffled = [q["paper_question_id"] for q in
                    ExamAttemptService.build_paper_payload(attempt)["questions"]]
        self.assertEqual(sorted(base), sorted(shuffled), "打乱不能增删题目")
        self.assertNotEqual(base, shuffled)

    def test_no_shuffle_keeps_paper_order(self):
        attempt = self.start(self.exam)
        payload = ExamAttemptService.build_paper_payload(attempt)
        numbers = [q["number"] for q in payload["questions"]]
        self.assertEqual(numbers, sorted(numbers))

    # ------------------------------------------------------------------
    # 暂存
    # ------------------------------------------------------------------
    def test_save_answers_persists_and_normalizes_labels(self):
        attempt = self.start(self.exam)
        plan = ExamAttemptService.build_paper_payload(attempt)
        single = next(q for q in plan["questions"] if q["question_type"] == QuestionType.SINGLE)

        result = ExamAttemptService.save_answers(attempt, [
            {"paper_question_id": single["paper_question_id"], "selected_labels": "b a",
             "time_spent_seconds": 15},
        ])
        self.assertEqual(result["saved"], 1)
        answer = Answer.objects.get(attempt=attempt,
                                   paper_question_id=single["paper_question_id"])
        self.assertEqual(answer.selected_labels, "AB")  # 大写 + 排序
        self.assertTrue(answer.is_answered)
        self.assertEqual(answer.time_spent_seconds, 15)

    def test_whitespace_text_is_not_counted_as_answered(self):
        attempt = self.start(self.exam)
        plan = ExamAttemptService.build_paper_payload(attempt)
        essay = next(q for q in plan["questions"] if q["question_type"] == QuestionType.ESSAY)
        ExamAttemptService.save_answers(attempt, [
            {"paper_question_id": essay["paper_question_id"], "answer_text": "   \n  "},
        ])
        answer = Answer.objects.get(attempt=attempt,
                                   paper_question_id=essay["paper_question_id"])
        self.assertFalse(answer.is_answered)

    def test_fill_answer_in_text_counts_as_answered(self):
        """填空题归「可精确判分」，但答案存在文本里——曾经按客观题判就成了漏答，锁死它。"""
        attempt = self.start(self.exam)
        plan = ExamAttemptService.build_paper_payload(attempt)
        fill = next(q for q in plan["questions"] if q["question_type"] == QuestionType.FILL)
        result = ExamAttemptService.save_answers(attempt, [
            {"paper_question_id": fill["paper_question_id"], "answer_text": "栈\n队列"},
        ])
        self.assertEqual(result["saved"], 1)
        answer = Answer.objects.get(attempt=attempt,
                                   paper_question_id=fill["paper_question_id"])
        self.assertTrue(answer.is_answered)
        self.assertEqual(answer.selected_labels, "")
        self.assertEqual(result["answered_count"], 1)

    def test_save_answers_ignores_foreign_question(self):
        attempt = self.start(self.exam)
        other_paper = self.make_paper()
        foreign = PaperQuestion.objects.filter(paper=other_paper).first()
        result = ExamAttemptService.save_answers(attempt, [
            {"paper_question_id": foreign.id, "selected_labels": "A"},
        ])
        self.assertEqual(result["saved"], 0)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(attempt.answers.count(), 0)

    def test_save_answers_updates_progress(self):
        attempt = self.start(self.exam)
        items = self.answer_payload(attempt)[:3]
        result = ExamAttemptService.save_answers(attempt, items)
        self.assertEqual(result["answered_count"], 3)
        self.assertEqual(result["progress"]["total"], 6)
        self.assertEqual(result["progress"]["percent"], 50.0)

    def test_save_single_answer_returns_updated_row(self):
        attempt = self.start(self.exam)
        plan = ExamAttemptService.build_paper_payload(attempt)
        target = plan["questions"][0]
        result = ExamAttemptService.save_answer(
            attempt, paper_question_id=target["paper_question_id"],
            selected_labels="A", is_flagged=True,
        )
        self.assertEqual(result["answer"]["selected_labels"], "A")
        self.assertTrue(result["answer"]["is_flagged"])

    def test_save_rejected_after_grace_period(self):
        attempt = self.start(self.exam)
        attempt.deadline_at = timezone.now() - timedelta(seconds=SUBMIT_GRACE_SECONDS + 5)
        attempt.save(update_fields=["deadline_at"])
        with self.assertRaises(BusinessError):
            ExamAttemptService.save_answers(attempt, self.answer_payload(attempt)[:1])

    def test_save_allowed_within_grace_period(self):
        attempt = self.start(self.exam)
        attempt.deadline_at = timezone.now() - timedelta(seconds=5)
        attempt.save(update_fields=["deadline_at"])
        items = self.answer_payload(attempt)[:1]
        result = ExamAttemptService.save_answers(attempt, items)
        self.assertEqual(result["saved"], 1)

    def test_save_rejected_after_submit(self):
        attempt = self.start(self.exam)
        ExamAttemptService.submit(attempt)
        with self.assertRaises(BusinessError):
            ExamAttemptService.save_answers(attempt, self.answer_payload(attempt)[:1])

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    def test_heartbeat_counts_and_reports_drift(self):
        attempt = self.start(self.exam)
        result = ExamAttemptService.heartbeat(attempt, client_remaining=99999)
        attempt.refresh_from_db()
        self.assertEqual(attempt.heartbeat_count, 1)
        self.assertIsNotNone(attempt.last_heartbeat_at)
        self.assertGreater(result["clock_drift_seconds"], 0)
        self.assertGreater(result["remaining_seconds"], 0)
        self.assertFalse(result["should_submit"])

    def test_heartbeat_after_submit_signals_should_submit(self):
        attempt = self.start(self.exam)
        ExamAttemptService.submit(attempt)
        result = ExamAttemptService.heartbeat(attempt)
        self.assertTrue(result["should_submit"])

    # ------------------------------------------------------------------
    # 行为采集
    # ------------------------------------------------------------------
    def test_record_behavior_counts(self):
        attempt = self.start(self.exam)
        result = ExamAttemptService.record_behavior(attempt, [
            {"type": "switch"}, {"type": "switch", "count": 2}, {"type": "paste"},
        ])
        self.assertEqual(result["switch_count"], 3)
        self.assertEqual(result["paste_count"], 1)
        # 「允许切屏 3 次」= 恰好 3 次不违规，第 4 次才算超
        self.assertFalse(result["limit_exceeded"])
        self.assertFalse(result["auto_submitted"])

        exceeded = ExamAttemptService.record_behavior(attempt, [{"type": "switch"}])
        self.assertTrue(exceeded["limit_exceeded"])

    def test_record_behavior_respects_detection_switch(self):
        exam = self.make_exam(detect_switch=False, detect_paste=False)
        attempt = self.start(exam)
        result = ExamAttemptService.record_behavior(attempt, [
            {"type": "switch"}, {"type": "paste"},
        ])
        self.assertEqual(result["switch_count"], 0)
        self.assertEqual(result["paste_count"], 0)
        self.assertFalse(result["limit_exceeded"])

    def test_behavior_auto_submits_when_configured(self):
        exam = self.make_exam(max_switch_count=1, auto_submit_on_limit=True)
        attempt = self.start(exam)
        result = ExamAttemptService.record_behavior(attempt, [
            {"type": "switch"}, {"type": "switch"},
        ])
        self.assertTrue(result["auto_submitted"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.SUBMITTED)
        self.assertEqual(attempt.submit_reason, SubmitReason.FORCED)

    def test_behavior_rejected_after_submit(self):
        attempt = self.start(self.exam)
        ExamAttemptService.submit(attempt)
        with self.assertRaises(BusinessError):
            ExamAttemptService.record_behavior(attempt, [{"type": "switch"}])

    # ------------------------------------------------------------------
    # 交卷 / 超时
    # ------------------------------------------------------------------
    def test_submit_records_state(self):
        attempt = self.start(self.exam)
        ExamAttemptService.save_answers(attempt, self.answer_payload(attempt)[:4])
        attempt = ExamAttemptService.submit(attempt)
        self.assertEqual(attempt.status, AttemptStatus.SUBMITTED)
        self.assertEqual(attempt.submit_reason, SubmitReason.MANUAL)
        self.assertFalse(attempt.is_timeout)
        self.assertEqual(attempt.answered_count, 4)
        self.assertIsNotNone(attempt.submitted_at)
        self.assertGreaterEqual(attempt.time_spent_seconds, 0)

    def test_submit_after_deadline_marks_timeout(self):
        attempt = self.start(self.exam)
        attempt.deadline_at = timezone.now() - timedelta(minutes=1)
        attempt.save(update_fields=["deadline_at"])
        attempt = ExamAttemptService.submit(attempt, reason=SubmitReason.MANUAL)
        self.assertEqual(attempt.status, AttemptStatus.TIMEOUT)
        self.assertTrue(attempt.is_timeout)
        self.assertEqual(attempt.submit_reason, SubmitReason.TIMEOUT)

    def test_submit_is_idempotent(self):
        attempt = self.start(self.exam)
        first = ExamAttemptService.submit(attempt)
        stamp = first.submitted_at
        second = ExamAttemptService.submit(attempt)
        self.assertEqual(second.submitted_at, stamp)
        self.assertEqual(second.status, AttemptStatus.SUBMITTED)

    def test_resolve_expiry_auto_submits_after_grace(self):
        attempt = self.start(self.exam)
        attempt.deadline_at = timezone.now() - timedelta(seconds=SUBMIT_GRACE_SECONDS + 10)
        attempt.save(update_fields=["deadline_at"])
        self.assertTrue(ExamAttemptService.resolve_expiry(attempt))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.TIMEOUT)

    def test_resolve_expiry_does_nothing_within_grace(self):
        attempt = self.start(self.exam)
        attempt.deadline_at = timezone.now() - timedelta(seconds=5)
        attempt.save(update_fields=["deadline_at"])
        self.assertFalse(ExamAttemptService.resolve_expiry(attempt))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.IN_PROGRESS)

    def test_get_state_returns_finished_after_timeout(self):
        attempt = self.start(self.exam)
        attempt.deadline_at = timezone.now() - timedelta(seconds=SUBMIT_GRACE_SECONDS + 10)
        attempt.save(update_fields=["deadline_at"])
        state = ExamAttemptService.get_state(attempt)
        self.assertTrue(state["finished"])
        self.assertEqual(state["remaining_seconds"], 0)

    def test_get_state_restores_saved_answers(self):
        attempt = self.start(self.exam)
        ExamAttemptService.save_answers(attempt, self.answer_payload(attempt)[:2])
        state = ExamAttemptService.get_state(attempt)
        restored = [q for q in state["questions"] if q["answer"]["is_answered"]]
        self.assertEqual(len(restored), 2)
        self.assertFalse(state["finished"])


# ======================================================================
class ExamAnalysisTests(ExamBaseTest):
    """统计与行为预警。"""

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam()
        self.attempt = self.start(self.exam)

    def test_overview_counts(self):
        ExamAttemptService.submit(self.attempt)
        ExamAttemptService.start(exam=self.exam, student=self.student_b)

        overview = ExamAnalysisService.overview(self.exam)
        self.assertEqual(overview["started"], 2)
        self.assertEqual(overview["submitted"], 1)
        self.assertEqual(overview["in_progress"], 1)
        self.assertEqual(overview["timeout"], 0)
        self.assertEqual(overview["graded"], 0)
        # 还没判分时成绩必须是 None，不能拿 0 分冒充
        self.assertIsNone(overview["avg_score"])
        self.assertIsNone(overview["pass_rate"])

    def test_overview_counts_not_started(self):
        overview = ExamAnalysisService.overview(self.exam)
        # 面向全体的考试：应考人数按在册学生数算
        self.assertEqual(overview["applicants"], 2)
        self.assertEqual(overview["not_started"], 1)

    def test_behavior_stats_flags_suspicious(self):
        ExamAttemptService.record_behavior(self.attempt, [
            {"type": "switch"}, {"type": "switch"}, {"type": "switch"}, {"type": "switch"},
        ])
        stats = ExamAnalysisService.behavior_stats(self.exam)
        self.assertEqual(stats["switch_total"], 4)
        self.assertEqual(stats["flagged_count"], 1)
        self.assertEqual(stats["flagged"][0]["student_id"], self.student.id)

    def test_attempt_table_filters(self):
        ExamAttemptService.submit(self.attempt)
        ExamAttemptService.start(exam=self.exam, student=self.student_b)

        running = ExamAnalysisService.attempt_table(self.exam, status=AttemptStatus.IN_PROGRESS)
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["student_id"], self.student_b.id)

        rows = ExamAnalysisService.attempt_table(self.exam, keyword="考生乙")
        self.assertEqual(len(rows), 1)

        self.assertEqual(len(ExamAnalysisService.attempt_table(self.exam)), 2)

    def test_attempt_table_marks_suspicious(self):
        ExamAttemptService.record_behavior(self.attempt, [
            {"type": "switch"}, {"type": "switch"}, {"type": "switch"}, {"type": "switch"},
        ])
        rows = ExamAnalysisService.attempt_table(self.exam)
        self.assertTrue(rows[0]["suspicious"])
        self.assertEqual(rows[0]["switch_count"], 4)

    def test_global_statistics_shape(self):
        stats = ExamAnalysisService.global_statistics()
        self.assertGreaterEqual(stats["total"], 1)
        self.assertEqual(stats["attempt_total"], 1)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(len(stats["by_status"]), len(ExamStatus.choices))

    def test_global_statistics_scoped_to_teacher(self):
        other_teacher = self.admin  # 管理员看全部
        as_admin = ExamAnalysisService.global_statistics(other_teacher)
        self.assertEqual(as_admin["total"], Exam.objects.count())
