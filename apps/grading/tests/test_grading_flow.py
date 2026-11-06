"""判分主流程测试：编排、汇总幂等、状态推进、批改队列。

这个文件的重点是三条「纪律」有没有被守住：

1. **汇总只有一个写入口且幂等** —— ``recalc()`` 重跑多少次分数都不会变；
2. **状态推进单向** —— 已出分 ``GRADED`` 是终态，绝不被后续操作打回；
3. **未交卷不能判分** —— 否则学生还在答题，分数就已经定了。
"""

from decimal import Decimal

from apps.common.constants import AttemptStatus, QuestionType
from apps.common.exceptions import BusinessError
from apps.exam.constants import MultipleChoicePolicy
from apps.grading.services import GradingService, ScoreReportService
from apps.grading.tests.base import GradingBaseTest


class GradeAttemptTest(GradingBaseTest):
    def test_cannot_grade_in_progress_attempt(self):
        attempt = self.start(self.exam)
        with self.assertRaises(BusinessError):
            GradingService.grade_attempt(attempt)

    def test_objective_grading_sets_scores_and_reviewing_status(self):
        """混卷（有主观题）判客观题后应停在「待人工复核」，而不是自动完成。"""
        attempt = self.submit_with_correct_answers()
        result = GradingService.grade_attempt(attempt, with_ai=False)
        attempt.refresh_from_db()

        self.assertEqual(result["objective"]["graded"], 4)
        self.assertEqual(attempt.objective_score, Decimal("8.00"))
        self.assertEqual(attempt.subjective_score, Decimal("0.00"))
        self.assertEqual(attempt.score, Decimal("8.00"))
        self.assertEqual(attempt.status, AttemptStatus.REVIEWING)
        self.assertIsNotNone(attempt.graded_at)

    def test_objective_only_paper_reaches_auto_graded(self):
        exam = self.make_objective_exam()
        attempt = self.start(exam)
        self.save_answers(attempt, self.objective_payload(attempt))

        from apps.exam.services import ExamAttemptService

        ExamAttemptService.submit(attempt)
        GradingService.grade_attempt(attempt)
        attempt.refresh_from_db()

        self.assertEqual(attempt.status, AttemptStatus.AUTO_GRADED)
        self.assertTrue(attempt.is_graded)
        # 8 分满分、及格分 5 → 应当判及格
        self.assertTrue(attempt.passed())

    def test_with_ai_fills_subjective(self):
        attempt = self.submit_with_correct_answers()
        result = GradingService.grade_attempt(attempt, with_ai=True)
        attempt.refresh_from_db()

        self.assertEqual(result["subjective"]["graded"], 2)
        # 主观题（简答 5 + 论述 10）完全答对 → 15 分
        self.assertEqual(attempt.subjective_score, Decimal("15.00"))
        self.assertEqual(attempt.score, Decimal("23.00"))
        self.assertEqual(attempt.status, AttemptStatus.REVIEWING)

    def test_regrading_is_idempotent(self):
        """**最关键的一条**：重复判分不能把分数累加。"""
        attempt = self.submit_with_correct_answers()
        first = GradingService.grade_attempt(attempt, with_ai=True)["summary"]
        second = GradingService.grade_attempt(attempt, with_ai=True)["summary"]
        third = GradingService.grade_attempt(attempt, with_ai=True)["summary"]
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_recalc_recomputes_from_answers(self):
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt)

        # 手动改一道题的分数，recalc 应当把它算进去（而不是在旧值上加）
        answer = self.answer_of(attempt, QuestionType.SINGLE)
        answer.score = Decimal("2.00")
        answer.save(update_fields=["score"])
        summary = GradingService.recalc(attempt)
        self.assertEqual(summary["objective_score"], 8.0)

        answer.score = Decimal("0.00")
        answer.save(update_fields=["score"])
        summary = GradingService.recalc(attempt)
        self.assertEqual(summary["objective_score"], 6.0)      # 8 - 2

    def test_policy_override_is_honoured(self):
        attempt = self.start(self.make_exam(multiple_choice_policy=MultipleChoicePolicy.PARTIAL))
        mapping = self.paper_map(attempt)
        self.save_answers(attempt, [
            {"paper_question_id": mapping[QuestionType.MULTIPLE]["paper_question_id"],
             "selected_labels": "A"},                      # 漏选
        ])
        from apps.exam.services import ExamAttemptService

        ExamAttemptService.submit(attempt)
        GradingService.grade_attempt(attempt)
        answer = self.answer_of(attempt, QuestionType.MULTIPLE)
        answer.refresh_from_db()
        self.assertEqual(answer.score, Decimal("1.50"))

    def test_grading_after_graded_requires_force(self):
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt, with_ai=True)
        GradingService.finalize_attempt(attempt)

        with self.assertRaises(BusinessError):
            GradingService.grade_attempt(attempt)

        result = GradingService.grade_attempt(attempt, with_ai=True, force=True)
        self.assertEqual(result["summary"]["score"], 23.0)


class StatusTransitionTest(GradingBaseTest):
    def test_graded_is_terminal(self):
        """已出分的作答，再怎么重判 / 复核都不该被打回「待复核」。"""
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt, with_ai=True)
        GradingService.finalize_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.GRADED)

        GradingService.sync_status(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.GRADED)

    def test_timeout_attempt_can_be_graded(self):
        """真超时（服务端过 deadline）交卷的记录，同样要能进入判分链路。"""
        from django.utils import timezone

        from apps.exam.services import ExamAttemptService

        attempt = self.start(self.exam)
        # 把截止时间挪到过去，复现「超时自动收卷」
        attempt.deadline_at = timezone.now() - timezone.timedelta(seconds=1)
        attempt.save(update_fields=["deadline_at"])
        ExamAttemptService.submit(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.TIMEOUT)

        GradingService.grade_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.REVIEWING)


class EnsureGradedTest(GradingBaseTest):
    def test_ensure_graded_is_lazy_and_idempotent(self):
        attempt = self.submit_with_correct_answers()
        self.assertTrue(GradingService.ensure_graded(attempt))      # 第一次真的判了
        self.assertFalse(GradingService.ensure_graded(attempt))     # 第二次什么都不做

    def test_ensure_graded_skips_in_progress(self):
        attempt = self.start(self.exam)
        self.assertFalse(GradingService.ensure_graded(attempt))

    def test_ensure_graded_never_calls_ai(self):
        """惰性判分只判客观题——绝不能在 GET 里顺手烧 token。"""
        attempt = self.submit_with_correct_answers()
        GradingService.ensure_graded(attempt)
        short = self.answer_of(attempt, QuestionType.SHORT)
        short.refresh_from_db()
        self.assertIsNone(short.ai_score)


class PendingAndQueueTest(GradingBaseTest):
    def test_pending_counts_subjective_and_unjudged_objective(self):
        attempt = self.submit_with_correct_answers()
        status = GradingService.grading_status(attempt)

        # 还没判分：客观题 4 道待判 + 主观题 2 道待复核
        self.assertEqual(status["objective_total"], 4)
        self.assertEqual(status["objective_graded"], 0)
        self.assertEqual(status["subjective_total"], 2)
        self.assertEqual(status["pending"], 6)
        self.assertFalse(status["complete"])

    def test_after_objective_grading_only_subjective_remains(self):
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt)
        status = GradingService.grading_status(attempt)
        self.assertEqual(status["objective_graded"], 4)
        self.assertEqual(status["pending"], 2)
        self.assertFalse(status["complete"])

    def test_queue_lists_pending_then_drops_when_done(self):
        attempt = self.submit_with_correct_answers()
        rows = GradingService.grading_queue(self.exam)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt_id"], attempt.id)

        GradingService.grade_attempt(attempt, with_ai=True)
        # 主观题还没人工复核 → 仍在队列里
        self.assertEqual(len(GradingService.grading_queue(self.exam)), 1)

        # 把两道主观题都复核掉 → 队列清空
        for question_type in (QuestionType.SHORT, QuestionType.ESSAY):
            answer = self.answer_of(attempt, question_type)
            GradingService.review_answer(answer, reviewer=self.teacher, score=Decimal("0.00"),
                                         comment="测试复核")
        self.assertEqual(GradingService.grading_queue(self.exam), [])
        # only_pending=False 时仍然能看到（用于「已批改」分页）
        self.assertEqual(len(GradingService.grading_queue(self.exam, only_pending=False)), 1)

    def test_queue_excludes_in_progress(self):
        self.start(self.exam)
        self.assertEqual(GradingService.grading_queue(self.exam), [])


class VisibilityTest(GradingBaseTest):
    def test_not_visible_before_grading(self):
        attempt = self.submit_with_correct_answers()
        self.assertFalse(ScoreReportService.score_visible(attempt))

    def test_pure_objective_visible_only_when_configured(self):
        exam = self.make_objective_exam(show_score_immediately=False)
        attempt = self.start(exam)
        self.save_answers(attempt, self.objective_payload(attempt))
        from apps.exam.services import ExamAttemptService

        ExamAttemptService.submit(attempt)
        GradingService.grade_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.AUTO_GRADED)
        self.assertFalse(ScoreReportService.score_visible(attempt))

        attempt.exam.show_score_immediately = True
        attempt.exam.save(update_fields=["show_score_immediately"])
        self.assertTrue(ScoreReportService.score_visible(attempt))

    def test_finalize_publishes_mixed_exam_score(self):
        """混卷即使配了「立即显示得分」也不能提前给分——主观题还没人看。"""
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt, with_ai=True)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, AttemptStatus.REVIEWING)
        self.assertFalse(ScoreReportService.score_visible(attempt))

        GradingService.finalize_attempt(attempt)
        self.assertTrue(ScoreReportService.score_visible(attempt))
