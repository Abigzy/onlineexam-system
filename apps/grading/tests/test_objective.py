"""客观题判分测试。

客观题判分是**纯函数**，所以这里的用例可以写得非常「死」——不需要 mock、
不依赖时间、不依赖大模型，输入什么就一定输出什么。这一类断言最值钱：
判分错一分，整张成绩单、及格率、班级排名会一起错。
"""

from decimal import Decimal

from apps.common.constants import QuestionType
from apps.grading.constants import MultipleChoicePolicy
from apps.grading.services import (
    ObjectiveGradingService,
    normalize_labels,
    normalize_text,
)
from apps.grading.tests.base import FILL_ANSWER, GradingBaseTest


class NormalizeHelperTest(GradingBaseTest):
    """归一化函数本身的用例——判分不准时先怀疑这里。"""

    def test_labels_are_upper_deduped_sorted(self):
        self.assertEqual(normalize_labels("ba"), "AB")
        self.assertEqual(normalize_labels("A,A,B"), "AB")
        self.assertEqual(normalize_labels(["b", "a", "b"]), "AB")
        self.assertEqual(normalize_labels(""), "")
        self.assertEqual(normalize_labels(None), "")

    def test_text_normalization_covers_four_real_accidents(self):
        # 全角 → 半角
        self.assertEqual(normalize_text("（栈）"), normalize_text("(栈)"))
        self.assertEqual(normalize_text("１２３"), "123")
        # 去空白
        self.assertEqual(normalize_text("栈 和 队列"), "栈和队列")
        # 去首尾标点
        self.assertEqual(normalize_text("栈队列。"), "栈队列")
        # 英文小写
        self.assertEqual(normalize_text("Stack"), "stack")


class SingleChoiceGradingTest(GradingBaseTest):
    def setUp(self):
        self.attempt = self.start(self.exam)
        self.answer = self.answer_of(self.attempt, QuestionType.SINGLE)

    def test_correct_answer_gets_full_score(self):
        self.answer.selected_labels = "A"
        result = ObjectiveGradingService.grade(self.answer)
        self.assertTrue(result["judgeable"])
        self.assertTrue(result["is_correct"])
        self.assertEqual(result["score"], Decimal("2.00"))

    def test_wrong_answer_gets_zero(self):
        self.answer.selected_labels = "B"
        result = ObjectiveGradingService.grade(self.answer)
        self.assertTrue(result["judgeable"])
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["score"], Decimal("0.00"))

    def test_unanswered_is_not_correct_but_judgeable(self):
        self.answer.selected_labels = ""
        result = ObjectiveGradingService.grade(self.answer)
        self.assertTrue(result["judgeable"])
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["score"], Decimal("0.00"))
        self.assertIn("未作答", result["reason"])

    def test_multi_selection_on_single_choice_is_wrong(self):
        """单选题勾了两个字母（脏数据）不该被判对。"""
        self.answer.selected_labels = "AB"
        result = ObjectiveGradingService.grade(self.answer)
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["score"], Decimal("0.00"))


class JudgeGradingTest(GradingBaseTest):
    def test_judge_correct(self):
        attempt = self.start(self.exam)
        answer = self.answer_of(attempt, QuestionType.JUDGE)
        answer.selected_labels = "A"
        result = ObjectiveGradingService.grade(answer)
        self.assertTrue(result["is_correct"])
        self.assertEqual(result["score"], Decimal("1.00"))


class MultipleChoicePolicyTest(GradingBaseTest):
    """多选三策略——本模块最值得逐条钉死的逻辑。"""

    def setUp(self):
        self.attempt = self.start(
            self.make_exam(multiple_choice_policy=MultipleChoicePolicy.STRICT),
        )
        self.answer = self.answer_of(self.attempt, QuestionType.MULTIPLE)

    def _grade(self, labels, policy):
        self.answer.selected_labels = labels
        return ObjectiveGradingService.grade(self.answer, policy=policy)

    # ---- 严格：全对才得分 ----
    def test_strict_full_marks(self):
        result = self._grade("AB", MultipleChoicePolicy.STRICT)
        self.assertTrue(result["is_correct"])
        self.assertEqual(result["score"], Decimal("3.00"))

    def test_strict_partial_selection_gets_zero(self):
        result = self._grade("A", MultipleChoicePolicy.STRICT)
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["score"], Decimal("0.00"))

    def test_strict_wrong_selection_gets_zero(self):
        self.assertEqual(self._grade("AC", MultipleChoicePolicy.STRICT)["score"], Decimal("0.00"))

    # ---- 漏选部分分：错选不得分 ----
    def test_partial_missing_one_gets_half(self):
        result = self._grade("A", MultipleChoicePolicy.PARTIAL)
        self.assertFalse(result["is_correct"])       # 不是全对
        self.assertEqual(result["score"], Decimal("1.50"))   # 1/2 × 3 分
        self.assertIn("漏选", result["reason"])

    def test_partial_with_wrong_selection_gets_zero(self):
        result = self._grade("AC", MultipleChoicePolicy.PARTIAL)
        self.assertEqual(result["score"], Decimal("0.00"))
        self.assertIn("错选", result["reason"])

    def test_partial_full_marks_still_full(self):
        self.assertEqual(self._grade("AB", MultipleChoicePolicy.PARTIAL)["score"], Decimal("3.00"))

    # ---- 错选倒扣 ----
    def test_penalty_offsets_wrong_selection(self):
        # 命中 1 个、错 1 个 → 净 0 → 0 分（不倒欠）
        result = self._grade("AC", MultipleChoicePolicy.PENALTY)
        self.assertEqual(result["score"], Decimal("0.00"))
        self.assertIn("倒扣", result["reason"])

    def test_penalty_never_goes_negative(self):
        self.assertEqual(self._grade("CD", MultipleChoicePolicy.PENALTY)["score"], Decimal("0.00"))

    def test_penalty_missing_one_still_scores(self):
        self.assertEqual(self._grade("A", MultipleChoicePolicy.PENALTY)["score"], Decimal("1.50"))

    # ---- 未作答 ----
    def test_unanswered_multiple_is_zero_in_every_policy(self):
        for policy in MultipleChoicePolicy.values:
            self.assertEqual(self._grade("", policy)["score"], Decimal("0.00"))


class FillGradingTest(GradingBaseTest):
    """填空题：按行对齐、按空计分、允许多种写法。"""

    def setUp(self):
        self.attempt = self.start(self.exam)
        self.answer = self.answer_of(self.attempt, QuestionType.FILL)
        # 参考答案是「栈\n队列」两个空，整题 2 分 → 每空 1 分

    def test_all_blanks_correct(self):
        self.answer.answer_text = FILL_ANSWER
        result = ObjectiveGradingService.grade(self.answer)
        self.assertTrue(result["is_correct"])
        self.assertEqual(result["score"], Decimal("2.00"))
        self.assertEqual(len(result["blanks"]), 2)

    def test_one_blank_correct_gets_proportional_score(self):
        self.answer.answer_text = "栈\n链表"
        result = ObjectiveGradingService.grade(self.answer)
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["score"], Decimal("1.00"))
        self.assertEqual(result["blanks"][0]["matched"], True)
        self.assertEqual(result["blanks"][1]["matched"], False)

    def test_missing_second_blank_keeps_alignment(self):
        """只写了一个空：第二个空没填，不该让第一个空也判错。"""
        self.answer.answer_text = "栈"
        result = ObjectiveGradingService.grade(self.answer)
        self.assertEqual(result["score"], Decimal("1.00"))

    def test_whitespace_and_fullwidth_are_ignored(self):
        self.answer.answer_text = " 栈 \n 队列 "
        self.assertTrue(ObjectiveGradingService.grade(self.answer)["is_correct"])

    def test_empty_answer_scores_zero(self):
        self.answer.answer_text = ""
        result = ObjectiveGradingService.grade(self.answer)
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["score"], Decimal("0.00"))

    def test_multiple_acceptable_spellings(self):
        question = self.answer.paper_question.question
        question.reference_answer = "栈|栈式结构\n队列"
        question.save(update_fields=["reference_answer"])
        self.answer.answer_text = "栈式结构\n队列"
        self.assertTrue(ObjectiveGradingService.grade(self.answer)["is_correct"])


class UnjudgeableTest(GradingBaseTest):
    """题目本身缺答案时**不能**当 0 分——那是数据问题，不是学生答错。"""

    def test_choice_without_correct_option_is_unjudgeable(self):
        attempt = self.start(self.exam)
        answer = self.answer_of(attempt, QuestionType.SINGLE)
        answer.paper_question.question.options.update(is_correct=False)
        result = ObjectiveGradingService.grade(answer)
        self.assertFalse(result["judgeable"])
        self.assertIsNone(result["is_correct"])
        self.assertIn("未配置正确答案", result["reason"])

    def test_fill_without_reference_is_unjudgeable(self):
        attempt = self.start(self.exam)
        answer = self.answer_of(attempt, QuestionType.FILL)
        question = answer.paper_question.question
        question.reference_answer = ""
        question.save(update_fields=["reference_answer"])
        result = ObjectiveGradingService.grade(answer)
        self.assertFalse(result["judgeable"])
        self.assertIsNone(result["is_correct"])


class BatchObjectiveGradingTest(GradingBaseTest):
    def test_batch_grades_only_objective_and_persists(self):
        from apps.exam.models import Answer

        attempt = self.start(self.exam)
        self.save_answers(attempt, self.correct_payload(attempt))
        answers = list(attempt.answers.select_related("paper_question__question").all())

        result = ObjectiveGradingService.grade_attempt(answers)

        self.assertEqual(result["graded"], 4)          # 单选/多选/判断/填空
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["correct"], 4)
        # 单选 2 + 多选 3 + 判断 1 + 填空 2 = 8 分
        self.assertEqual(result["score"], 8.0)
        self.assertEqual(result["policy"], MultipleChoicePolicy.STRICT)

        # 已落库：四道客观题都应被标记为「正确」
        self.assertEqual(
            Answer.objects.filter(attempt=attempt, is_correct=True).count(), 4,
        )

    def test_batch_records_unjudgeable_reason(self):
        attempt = self.start(self.exam)
        self.save_answers(attempt, self.correct_payload(attempt))

        answer = self.answer_of(attempt, QuestionType.SINGLE)
        answer.paper_question.question.options.update(is_correct=False)

        answers = list(attempt.answers.select_related("paper_question__question").all())
        result = ObjectiveGradingService.grade_attempt(answers)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(result["warnings"]), 1)

    def test_persist_false_does_not_write(self):
        from apps.exam.models import Answer

        attempt = self.start(self.exam)
        self.save_answers(attempt, self.correct_payload(attempt))
        answers = list(attempt.answers.select_related("paper_question__question").all())

        ObjectiveGradingService.grade_attempt(answers, persist=False)
        self.assertEqual(Answer.objects.filter(attempt=attempt, is_correct__isnull=False).count(), 0)
