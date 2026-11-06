"""教师复核与 AI 校准测试。

复核这条链路的价值不在「能改分」，而在**改分这件事留下了可信的痕迹**：
AI 建议了多少、教师改成多少、是采纳还是推翻。这份痕迹是课程设计里
「AI 批改效果评估」的全部依据，所以每条断言都直接盯着 ``GradingFeedback`` 的字段。
"""

from decimal import Decimal

from apps.common.constants import AttemptStatus, QuestionType
from apps.common.exceptions import BusinessError
from apps.grading.constants import GradeSource, ReviewAction
from apps.grading.models import GradingFeedback
from apps.grading.services import GradingService, ScoreReportService
from apps.grading.tests.base import GradingBaseTest


class ReviewSetupMixin:
    """共用装配：交卷 → AI 判分 → 拿到两道主观题的 Answer。"""

    def setUp(self):
        self.attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(self.attempt, with_ai=True)
        self.attempt.refresh_from_db()
        self.short = self.answer_of(self.attempt, QuestionType.SHORT)      # 满分 5
        self.essay = self.answer_of(self.attempt, QuestionType.ESSAY)      # 满分 10


class AcceptAiTest(ReviewSetupMixin, GradingBaseTest):
    def test_accept_ai_sets_score_to_ai_suggestion(self):
        ai_score = self.short.ai_score
        self.assertIsNotNone(ai_score)

        feedback = GradingService.review_answer(
            self.short, reviewer=self.teacher, accept_ai=True,
        )
        self.short.refresh_from_db()

        self.assertEqual(self.short.score, ai_score)
        self.assertEqual(feedback.action, ReviewAction.ACCEPT)
        self.assertEqual(feedback.source, GradeSource.AI)
        self.assertEqual(feedback.ai_score, ai_score)
        self.assertEqual(feedback.final_score, ai_score)
        self.assertEqual(feedback.delta, Decimal("0.00"))
        self.assertEqual(feedback.ai_delta, Decimal("0.00"))
        self.assertEqual(feedback.full_score, Decimal("5.00"))
        self.assertEqual(feedback.reviewer, self.teacher)

    def test_accept_ai_without_suggestion_is_rejected(self):
        no_ai = self.answer_of(self.attempt, QuestionType.SINGLE)
        with self.assertRaises(BusinessError):
            GradingService.review_answer(no_ai, reviewer=self.teacher, accept_ai=True)


class AdjustTest(ReviewSetupMixin, GradingBaseTest):
    def test_adjust_records_delta_and_action(self):
        feedback = GradingService.review_answer(
            self.essay, reviewer=self.teacher, score=Decimal("6"), comment="论证不够充分",
        )
        self.essay.refresh_from_db()

        self.assertEqual(self.essay.score, Decimal("6.00"))
        self.assertEqual(self.essay.teacher_comment, "论证不够充分")
        self.assertEqual(feedback.action, ReviewAction.ADJUST)
        self.assertEqual(feedback.original_score, Decimal("10.00"))      # 复核前是 AI 的 10 分
        self.assertEqual(feedback.final_score, Decimal("6.00"))
        self.assertEqual(feedback.delta, Decimal("-4.00"))
        self.assertEqual(feedback.ai_delta, Decimal("-4.00"))
        self.assertEqual(feedback.ai_ratio, 1.0)

    def test_override_when_no_ai_suggestion(self):
        objective = self.answer_of(self.attempt, QuestionType.SINGLE)
        feedback = GradingService.review_answer(
            objective, reviewer=self.teacher, score=Decimal("1"),
        )
        objective.refresh_from_db()
        self.assertEqual(feedback.action, ReviewAction.OVERRIDE)
        self.assertEqual(feedback.source, GradeSource.AUTO)
        self.assertEqual(objective.score, Decimal("1.00"))
        # 满分 2 分给了 1 分 → 不算「正确」
        self.assertFalse(objective.is_correct)

    def test_objective_review_can_mark_correct(self):
        objective = self.answer_of(self.attempt, QuestionType.SINGLE)
        objective.is_correct = False
        objective.score = Decimal("0")
        objective.save(update_fields=["is_correct", "score"])

        GradingService.review_answer(objective, reviewer=self.teacher, score=Decimal("2"))
        objective.refresh_from_db()
        self.assertTrue(objective.is_correct)

    def test_subjective_is_correct_stays_null(self):
        """主观题没有「对 / 错」的二元结论，复核后也不该硬塞一个 bool。"""
        GradingService.review_answer(self.short, reviewer=self.teacher, score=Decimal("3"))
        self.short.refresh_from_db()
        self.assertIsNone(self.short.is_correct)

    def test_review_updates_attempt_summary(self):
        GradingService.review_answer(
            self.essay, reviewer=self.teacher, score=Decimal("0"),
        )
        self.attempt.refresh_from_db()
        # 客观 8 + 简答 5 + 论述 0 = 13
        self.assertEqual(self.attempt.score, Decimal("13.00"))
        self.assertEqual(self.attempt.subjective_score, Decimal("5.00"))


class ReviewGuardTest(ReviewSetupMixin, GradingBaseTest):
    def test_score_above_full_marks_is_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            GradingService.review_answer(self.short, reviewer=self.teacher, score=Decimal("6"))
        self.assertIn("不能超过", str(ctx.exception))

    def test_negative_score_is_rejected(self):
        with self.assertRaises(BusinessError):
            GradingService.review_answer(self.short, reviewer=self.teacher, score=Decimal("-1"))

    def test_missing_score_and_accept_flag_is_rejected(self):
        with self.assertRaises(BusinessError):
            GradingService.review_answer(self.short, reviewer=self.teacher)

    def test_cannot_review_in_progress_attempt(self):
        attempt = self.start(self.make_exam())      # 换一场考试，避开次数上限
        answer = self.answer_of(attempt, QuestionType.SHORT)
        with self.assertRaises(BusinessError):
            GradingService.review_answer(answer, reviewer=self.teacher, score=Decimal("1"))

    def test_full_marks_review_is_allowed(self):
        feedback = GradingService.review_answer(
            self.short, reviewer=self.teacher, score=Decimal("5"),
        )
        self.assertEqual(feedback.final_score, Decimal("5.00"))


class FeedbackRecordTest(ReviewSetupMixin, GradingBaseTest):
    def test_feedback_is_linked_and_queryable(self):
        GradingService.review_answer(self.short, reviewer=self.teacher, score=Decimal("3"))
        GradingService.review_answer(self.essay, reviewer=self.teacher, score=Decimal("7"))

        self.assertEqual(self.short.feedbacks.count(), 1)
        self.assertEqual(self.attempt.grading_feedbacks.count(), 2)
        self.assertEqual(
            GradingFeedback.objects.of_attempt(self.attempt).count(), 2,
        )
        self.assertEqual(
            GradingFeedback.objects.with_ai_suggestion().count(), 2,
        )
        self.assertEqual(GradingFeedback.objects.of_reviewer(self.teacher).count(), 2)

    def test_multiple_reviews_keep_full_history(self):
        """一道题改两次，历史不能被覆盖——否则「教师改了两次」这件事查不出来。"""
        GradingService.review_answer(self.short, reviewer=self.teacher, score=Decimal("2"))
        self.short.refresh_from_db()
        GradingService.review_answer(self.short, reviewer=self.teacher, score=Decimal("4"))

        rows = list(GradingFeedback.objects.of_answer(self.short).order_by("id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].original_score, Decimal("5.00"))    # 第一次改之前是 AI 给的 5
        self.assertEqual(rows[0].final_score, Decimal("2.00"))
        self.assertEqual(rows[1].original_score, Decimal("2.00"))    # 第二次改之前是 2
        self.assertEqual(rows[1].final_score, Decimal("4.00"))

    def test_payload_shape(self):
        feedback = GradingService.review_answer(self.short, reviewer=self.teacher,
                                                score=Decimal("4"), comment="还行")
        payload = feedback.to_payload()
        for key in ("id", "answer_id", "attempt_id", "reviewer_name", "action",
                    "ai_score", "original_score", "final_score", "delta", "ai_delta",
                    "full_score", "comment", "created_at"):
            self.assertIn(key, payload)
        self.assertEqual(payload["comment"], "还行")


class CalibrationTest(ReviewSetupMixin, GradingBaseTest):
    def test_empty_calibration(self):
        data = ScoreReportService.calibration()
        self.assertEqual(data["sample"], 0)
        self.assertIsNone(data["mean_abs_delta"])
        self.assertIsNone(data["accept_rate"])

    def test_calibration_measures_ai_agreement(self):
        # 简答（满分 5，AI 给 5）：原样采纳 → 偏差 0
        GradingService.review_answer(self.short, reviewer=self.teacher, accept_ai=True)
        # 论述（满分 10，AI 给 10）：教师改成 6 分 → 偏差 -4
        GradingService.review_answer(self.essay, reviewer=self.teacher, score=Decimal("6"))

        data = ScoreReportService.calibration()

        self.assertEqual(data["sample"], 2)
        self.assertEqual(data["total_reviews"], 2)
        self.assertEqual(data["accepted"], 1)
        self.assertEqual(data["adjusted"], 1)
        self.assertEqual(data["accept_rate"], 50.0)
        self.assertEqual(data["mean_abs_delta"], 2.0)          # (0 + 4) / 2
        self.assertEqual(data["mean_signed_delta"], -2.0)      # (0 - 4) / 2
        # 简答两份都在「满分档」，论述从满分档掉到部分档 → 一致率 50%
        self.assertEqual(data["band_match_rate"], 50.0)
        self.assertTrue(data["by_question_type"])

    def test_calibration_ignores_reviews_without_ai_sample(self):
        objective = self.answer_of(self.attempt, QuestionType.SINGLE)
        GradingService.review_answer(objective, reviewer=self.teacher, score=Decimal("2"))
        data = ScoreReportService.calibration()
        self.assertEqual(data["total_reviews"], 1)
        self.assertEqual(data["sample"], 0)                    # 没有 AI 建议分 → 不进样本

    def test_calibration_scoped_to_reviewer(self):
        GradingService.review_answer(self.short, reviewer=self.teacher, accept_ai=True)
        self.assertEqual(ScoreReportService.calibration(self.teacher)["total_reviews"], 1)
        self.assertEqual(ScoreReportService.calibration(self.student)["total_reviews"], 0)


class FinalizeTest(ReviewSetupMixin, GradingBaseTest):
    def test_finalize_marks_graded_and_visible(self):
        attempt = GradingService.finalize_attempt(self.attempt, grader=self.teacher)
        self.assertEqual(attempt.status, AttemptStatus.GRADED)
        self.assertIsNotNone(attempt.graded_at)
        self.assertTrue(ScoreReportService.score_visible(attempt))

    def test_finalize_recomputes_before_publishing(self):
        GradingService.review_answer(self.essay, reviewer=self.teacher, score=Decimal("0"))
        attempt = GradingService.finalize_attempt(self.attempt)
        self.assertEqual(attempt.score, Decimal("13.00"))

    def test_finalize_in_progress_is_rejected(self):
        attempt = self.start(self.make_exam())
        with self.assertRaises(BusinessError):
            GradingService.finalize_attempt(attempt)
