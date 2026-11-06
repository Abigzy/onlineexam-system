"""学情分析测试的公共装配。

在判分模块的装配（六种题型 + 真实判分链路）之上，只做两件事：

1. **补一层父子知识点。** 掌握度是按知识点聚合并**向上归并**到祖先的，
   只挂一个孤立知识点根本测不出归并逻辑。这里给 ``线性表`` 加一个父节点
   ``数据结构``，让「子知识点答题 → 父知识点汇总」有真实数据可验。
2. **提供一个「精确控制判分结果」的造数据工具。** 学情分析读的是
   ``Answer.is_correct`` / ``Answer.score``，要验证「正确率 0.5 落在哪一档」
   就必须能精确摆出这个正确率。走真实判分链路会被题型分值牵着走
   （简答题 5 分、论述题 10 分，很难配出想要的正确率），
   所以这里直接写判分字段——**这不是绕过被测代码**，
   被测的是聚合口径，不是判分逻辑（判分在模块五已经测过了）。
"""

from decimal import Decimal

from apps.analytics.constants import ANALYZABLE_STATUSES
from apps.common.constants import AttemptStatus, QuestionType
from apps.exam.services import ExamAttemptService
from apps.grading.tests.base import GradingBaseTest
from apps.questions.models import KnowledgePoint, Question

# 父子知识点：题目挂在「线性表」，掌握度要能汇总到「数据结构」
PARENT_KP_NAME = "数据结构"
CHILD_KP_NAME = "线性表"


class AnalyticsBaseTest(GradingBaseTest):
    """学情分析公共装配。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        # 给已有知识点（线性表）挂一个父节点，用来验证「向上归并」
        cls.child_kp = cls.kp
        cls.parent_kp = KnowledgePoint.objects.create(
            name=PARENT_KP_NAME, subject=cls.child_kp.subject, code="EX-00",
        )
        cls.child_kp.parent = cls.parent_kp
        cls.child_kp.save(update_fields=["parent"])

    # ------------------------------------------------------------------
    # 造「已判分」的作答（精确控制对错）
    # ------------------------------------------------------------------
    def judged_attempt(self, exam=None, *, student=None, correct_types=None,
                       partial=None, status=None, fail_reason=""):
        """开考 → 全部作答 → 交卷 → 直接写判分结果。

        :param correct_types: 判为「答对」的题型集合，不在其中的客观题判为答错
        :param partial: ``{题型: 分数}``，用于主观题 / 填空题的精确得分
        :param status: 强制指定 ``ExamAttempt.status``，默认 ``graded``

        ``correct_types=None`` 表示全部答对（含主观题拿满分）。
        """
        attempt = self.start(exam or self.exam, student=student)
        ExamAttemptService.save_answers(attempt, self.answer_payload(attempt))
        ExamAttemptService.submit(attempt)
        attempt.refresh_from_db()

        # 按「这份卷子实际有哪些题」走，而不是按类里那套固定的六道题——
        # 单题卷 / 全客观题卷的题型集合是不一样的。
        question_types = list(self.paper_map(attempt).keys())
        correct_types = set(correct_types) if correct_types is not None else set(question_types)
        partial = partial or {}

        objective_total = Decimal("0")
        subjective_total = Decimal("0")
        for question_type in question_types:
            answer = self.answer_of(attempt, question_type)
            full = Decimal(str(answer.paper_question.score or 0))

            if question_type in partial:
                score = Decimal(str(partial[question_type]))
                answer.score = score
                # 主观题没有「对错」的二元结论；客观题按是否拿满判定
                answer.is_correct = (
                    None if question_type in (QuestionType.SHORT, QuestionType.ESSAY)
                    else bool(full > 0 and score >= full)
                )
            elif question_type in correct_types:
                answer.score = full
                answer.is_correct = (
                    None if question_type in (QuestionType.SHORT, QuestionType.ESSAY) else True
                )
            else:
                answer.score = Decimal("0")
                answer.is_correct = (
                    None if question_type in (QuestionType.SHORT, QuestionType.ESSAY) else False
                )

            answer.save(update_fields=["score", "is_correct", "updated_at"])
            if question_type in (QuestionType.SHORT, QuestionType.ESSAY):
                subjective_total += answer.score
            else:
                objective_total += answer.score

        attempt.status = status or AttemptStatus.GRADED
        attempt.score = objective_total + subjective_total
        attempt.objective_score = objective_total
        attempt.subjective_score = subjective_total
        attempt.save(update_fields=["status", "score", "objective_score",
                                    "subjective_score", "updated_at"])
        attempt.refresh_from_db()
        return attempt

    # ------------------------------------------------------------------
    def make_exam_with_attempts(self, count_per_student=1, **kwargs):
        """造一场考试，并让两名学生各作答 N 次（全部答对）。

        返回 ``(exam, [attempt, ...])``。多份作答是为了测分布 / 区分度 / 排名。
        """
        exam = self.make_exam(max_attempts=max(1, count_per_student), **kwargs)
        attempts = []
        for student, factor in ((self.student, 1.0), (self.student_b, 0.5)):
            for _ in range(count_per_student):
                attempts.append(self.judged_attempt(
                    exam, student=student,
                    # 考生乙只答对一半题型，制造分数差异，否则区分度算不出来
                    correct_types=set(self.questions.keys()) if factor == 1.0
                    else {QuestionType.SINGLE, QuestionType.JUDGE, QuestionType.SHORT},
                ))
        return exam, attempts

    # ------------------------------------------------------------------
    def add_question_with_knowledge_points(self, question_type, knowledge_points) -> Question:
        """再造一道题并挂到指定知识点上（用于归并 / 去重的边界用例）。"""
        from uuid import uuid4

        question = Question.objects.create(
            type=question_type,
            stem=f"【{question_type}】归并用题干 {uuid4().hex[:6]}",
            difficulty=2,
            score=Decimal("2"),
            subject=self.child_kp.subject,
            reference_answer="A",
        )
        question.knowledge_points.add(*knowledge_points)
        self.questions[question_type] = question
        return question

    # ------------------------------------------------------------------
    def status_counts(self, student=None):
        """返回 ``{状态: 次数}``，方便断言「哪些作答参与了统计」。"""
        from apps.exam.models import ExamAttempt

        rows = ExamAttempt.objects.filter(student=student or self.student)
        result = {status: 0 for status in AttemptStatus.values}
        for row in rows:
            result[row.status] = result.get(row.status, 0) + 1
        return result


__all__ = [
    "AnalyticsBaseTest",
    "CHILD_KP_NAME",
    "PARENT_KP_NAME",
    "ANALYZABLE_STATUSES",
]
