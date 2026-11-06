"""主观题 AI 批改测试。

分两层验证：

1. **真实链路（走 mock provider）** —— 验证「提示词拼装 → 调 provider → 解析 JSON →
   阈值折算」整条链路是通的，且离线可用（没配 API Key 也能演示、能跑测试）；
2. **注入受控的 AI 返回** —— 阈值分档、AI 失败降级、解析失败降级这些**边界**，
   必须用受控输入来验，不能指望 mock 恰好落在 0.69 这个点上。

第二层是这个文件里最值钱的部分：AI 是外部依赖，「AI 挂了教师还能不能干活」
比「AI 答得对不对」更影响系统能不能交付。
"""

from decimal import Decimal
from unittest.mock import patch

from apps.ai.exceptions import AIServiceError, AIResponseFormatError
from apps.ai.schemas import GradingResult
from apps.common.constants import QuestionType
from apps.grading.services import GradingService, SubjectiveGradingService
from apps.grading.tests.base import ESSAY_REFERENCE, SHORT_REFERENCE, GradingBaseTest


class SubjectiveMockPipelineTest(GradingBaseTest):
    """真实链路：走本地 mock provider。"""

    def setUp(self):
        self.attempt = self.start(self.exam)

    def test_answer_matching_reference_gets_full_score(self):
        answer = self.answer_of(self.attempt, QuestionType.SHORT)
        answer.answer_text = SHORT_REFERENCE
        result = SubjectiveGradingService.grade(answer)
        self.assertTrue(result["gradeable"])
        self.assertEqual(result["score"], Decimal("5.00"))     # 简答题 5 分
        self.assertEqual(result["ratio"], 1.0)
        self.assertTrue(result["is_mock"])                    # 明确告知走的是本地模拟
        self.assertIn("本地模拟", result["comment"])

    def test_empty_answer_short_circuits_without_calling_ai(self):
        answer = self.answer_of(self.attempt, QuestionType.SHORT)
        answer.answer_text = "   "
        with patch("apps.grading.services.get_ai_client") as mocked:
            result = SubjectiveGradingService.grade(answer)
        mocked.assert_not_called()                            # 空答案不该白花一次调用
        self.assertEqual(result["score"], Decimal("0.00"))
        self.assertIn("未作答", result["comment"])

    def test_irrelevant_answer_gets_zero(self):
        answer = self.answer_of(self.attempt, QuestionType.ESSAY)
        answer.answer_text = "qwertyuiop"                     # 与中文参考答案零重合
        result = SubjectiveGradingService.grade(answer)
        self.assertEqual(result["score"], Decimal("0.00"))
        self.assertLess(result["ratio"], 0.4)


class SubjectiveThresholdTest(GradingBaseTest):
    """阈值分档：用受控的 AI 返回把 0.7 / 0.4 两条线钉死。"""

    def setUp(self):
        self.attempt = self.start(self.exam)
        self.answer = self.answer_of(self.attempt, QuestionType.SHORT)   # 满分 5
        self.answer.answer_text = "任何作答"
        self.answer.save(update_fields=["answer_text"])

    def _grade_with_ratio(self, ratio):
        fake = GradingResult(score_ratio=ratio, reason="受控测试", suggestion="建议")
        with patch("apps.grading.services.parse_grading_result", return_value=fake):
            return SubjectiveGradingService.grade(self.answer)

    def test_ratio_at_full_threshold_gets_full_score(self):
        self.assertEqual(self._grade_with_ratio(0.7)["score"], Decimal("5.00"))

    def test_ratio_above_threshold_gets_full_score(self):
        self.assertEqual(self._grade_with_ratio(0.95)["score"], Decimal("5.00"))

    def test_ratio_in_partial_band_is_proportional(self):
        # 0.55 落在 [0.4, 0.7)：线性插值 (0.55-0.4)/(0.7-0.4)=0.5 → 2.5 分
        self.assertEqual(self._grade_with_ratio(0.55)["score"], Decimal("2.50"))

    def test_ratio_below_partial_threshold_gets_zero(self):
        self.assertEqual(self._grade_with_ratio(0.39)["score"], Decimal("0.00"))

    def test_thresholds_are_not_stepwise_at_boundary(self):
        """0.41 与 0.69 不该拿到同一个分——这正是线性插值存在的意义。"""
        low = self._grade_with_ratio(0.41)["score"]
        high = self._grade_with_ratio(0.69)["score"]
        self.assertLess(low, high)
        self.assertGreater(low, Decimal("0.00"))


class SubjectiveDegradeTest(GradingBaseTest):
    """AI 不可用时的降级：**绝不能把异常抛给教师**。"""

    def setUp(self):
        self.attempt = self.start(self.exam)
        self.answer = self.answer_of(self.attempt, QuestionType.SHORT)
        self.answer.answer_text = "我的作答内容"
        self.answer.save(update_fields=["answer_text"])

    def test_ai_call_failure_degrades_to_manual_pending(self):
        client = type("BrokenClient", (), {
            "run_scene": lambda *a, **k: (_ for _ in ()).throw(
                AIServiceError("网络超时", provider="deepseek")),
            "is_mock": False,
        })()
        with patch("apps.grading.services.get_ai_client", return_value=client):
            result = SubjectiveGradingService.grade(self.answer)

        self.assertFalse(result["gradeable"])                # 明确标为「判不了」
        self.assertEqual(result["score"], Decimal("0.00"))
        self.assertIn("待人工判分", result["comment"])
        self.assertIn("网络超时", result["error"])

    def test_unparseable_ai_response_degrades(self):
        with patch("apps.grading.services.parse_grading_result",
                   side_effect=AIResponseFormatError("不是 JSON", raw="oops")):
            result = SubjectiveGradingService.grade(self.answer)
        self.assertFalse(result["gradeable"])
        self.assertIn("待人工判分", result["comment"])

    def test_batch_grading_survives_one_failure(self):
        """一道题失败不该让整批白干——这是「批量 AI 批改」最重要的一条。"""
        essay = self.answer_of(self.attempt, QuestionType.ESSAY)
        essay.answer_text = ESSAY_REFERENCE
        essay.save(update_fields=["answer_text"])

        answers = list(self.attempt.answers.select_related("paper_question__question").all())
        original = SubjectiveGradingService.grade

        def flaky(answer, **kwargs):
            if answer.paper_question.question_type == QuestionType.SHORT:
                return SubjectiveGradingService._degrade(Decimal("5"), "模拟的偶发失败")
            return original(answer, **kwargs)

        with patch.object(SubjectiveGradingService, "grade", staticmethod(flaky)):
            result = SubjectiveGradingService.grade_attempt(answers)

        self.assertEqual(result["graded"] + result["failed"], 2)
        self.assertGreaterEqual(result["failed"], 1)
        # 成功的那道题必须真的拿到了分
        self.assertGreater(result["score"], 0)


class SubjectiveBatchTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.start(self.exam)
        self.save_answers(self.attempt, self.correct_payload(self.attempt))

    def test_batch_only_touches_subjective_and_persists(self):
        answers = list(self.attempt.answers.select_related("paper_question__question").all())
        result = SubjectiveGradingService.grade_attempt(answers, max_questions=10)

        self.assertEqual(result["graded"], 2)                # 简答 + 论述
        # 客观题的 ai_score 必须保持为空——AI 不参与客观题
        objective = [a for a in answers
                     if a.paper_question.question_type not in (QuestionType.SHORT,
                                                               QuestionType.ESSAY)]
        for answer in objective:
            self.assertIsNone(answer.ai_score)

    def test_second_run_skips_already_suggested(self):
        answers = list(self.attempt.answers.select_related("paper_question__question").all())
        SubjectiveGradingService.grade_attempt(answers, max_questions=10)
        second = SubjectiveGradingService.grade_attempt(answers, max_questions=10)
        self.assertEqual(second["graded"], 0)                # 幂等：不重复烧 token

    def test_overflow_is_reported(self):
        answers = list(self.attempt.answers.select_related("paper_question__question").all())
        result = SubjectiveGradingService.grade_attempt(answers, max_questions=1)
        self.assertEqual(result["graded"], 1)
        self.assertEqual(result["overflow"], 1)


class SubjectiveWiringTest(GradingBaseTest):
    """确认主观题确实接的是模块二那套提示词与阈值，而不是另起一套。"""

    def test_uses_shared_prompt_scene_and_threshold(self):
        from apps.ai.prompts import SCENE_PROMPTS

        self.assertEqual(SCENE_PROMPTS.get("grade_subjective"), "grade_subjective")
        from apps.grading.constants import SUBJECTIVE_FULL_THRESHOLD, SUBJECTIVE_PARTIAL_THRESHOLD
        self.assertEqual(SUBJECTIVE_FULL_THRESHOLD, 0.7)
        self.assertEqual(SUBJECTIVE_PARTIAL_THRESHOLD, 0.4)

    def test_full_ai_grading_via_service_sets_ai_score(self):
        attempt = self.submit_with_correct_answers()
        GradingService.grade_attempt(attempt, with_ai=True)
        short = self.answer_of(attempt, QuestionType.SHORT)
        short.refresh_from_db()
        self.assertIsNotNone(short.ai_score)
        self.assertTrue(short.ai_comment)
        self.assertEqual(short.score, short.ai_score)        # 未复核前先用建议分占位
