"""判分与成绩模块测试的公共装配。

在考试模块的装配（六种题型各一道 + 三角色）之上，只做两处**必要**的改造：

1. **多选题的正确答案改成 ``AB``。** 考试模块的装配里所有选择题的正确项都是 ``A``，
   那样根本测不出「漏选」「错选」——多选策略是本模块的核心，样本必须能表达这两种情形。
2. **主观题给一段真实的参考答案。** 考试模块里简答 / 论述的参考答案就是 ``A``，
   mock 批改器算出的相似度必然是 0，测不出阈值分档。这里换成一段有实义的要点，
   让「答得完整」「只答一半」两种作答都能拿到预期档位的分数。

刻意**不重新造题**：判分要读的字段（``reference_answer`` / ``correct_labels()`` /
``PaperQuestion.score`` 快照）都是考试模块已经在用的，装配复用才能保证两条链路
读的是同一份数据。
"""

from decimal import Decimal
from uuid import uuid4

from apps.common.constants import QuestionType
from apps.exam.tests.base import SUBJECT, ExamBaseTest

# 各题型的「标准正确答案」（与下面 setUpTestData 里的题库数据保持一致）
CORRECT_LABELS = {
    QuestionType.SINGLE: "A",
    QuestionType.MULTIPLE: "AB",
    QuestionType.JUDGE: "A",
}
FILL_ANSWER = "栈\n队列"

SHORT_REFERENCE = "需要答出定义、基本原理与典型应用场景"
ESSAY_REFERENCE = "需包含概念界定、价值分析、局限性讨论与实例佐证四个层次"


class GradingBaseTest(ExamBaseTest):
    """判分模块公共装配。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        # 1) 多选题：把 B 也标成正确项 → 正确答案 AB
        multiple = cls.questions[QuestionType.MULTIPLE]
        multiple.options.filter(label="B").update(is_correct=True)

        # 2) 主观题：换成有实义的参考答案（否则相似度恒为 0，测不出分档）
        short = cls.questions[QuestionType.SHORT]
        short.reference_answer = SHORT_REFERENCE
        short.save(update_fields=["reference_answer"])

        essay = cls.questions[QuestionType.ESSAY]
        essay.reference_answer = ESSAY_REFERENCE
        essay.save(update_fields=["reference_answer"])

    # ------------------------------------------------------------------
    # 取卷面
    # ------------------------------------------------------------------
    def paper_map(self, attempt) -> dict:
        """``{题型: 题目下发数据}`` —— 测试里按题型取 ``paper_question_id``。

        用 ``build_paper_payload()`` 而不是直接查库：它拿到的就是学生真实看到的
        那份数据（含乱序后的题序），判分与下发不会因为「测试用的另一套 id」而错位。
        """
        from apps.exam.services import ExamAttemptService

        plan = ExamAttemptService.build_paper_payload(attempt)
        return {question["question_type"]: question for question in plan["questions"]}

    def save_answers(self, attempt, items):
        from apps.exam.services import ExamAttemptService

        return ExamAttemptService.save_answers(attempt, items)

    # ------------------------------------------------------------------
    # 标准作答
    # ------------------------------------------------------------------
    def correct_payload(self, attempt) -> list:
        """一份「客观题全对、主观题答得完整」的作答。"""
        mapping = self.paper_map(attempt)
        items = []
        for question_type, label in CORRECT_LABELS.items():
            items.append({
                "paper_question_id": mapping[question_type]["paper_question_id"],
                "selected_labels": label,
            })
        items.append({
            "paper_question_id": mapping[QuestionType.FILL]["paper_question_id"],
            "answer_text": FILL_ANSWER,
        })
        items.append({
            "paper_question_id": mapping[QuestionType.SHORT]["paper_question_id"],
            "answer_text": SHORT_REFERENCE,          # 与参考答案一致 → 应得满分
        })
        items.append({
            "paper_question_id": mapping[QuestionType.ESSAY]["paper_question_id"],
            "answer_text": ESSAY_REFERENCE,
        })
        return items

    def submit_with_correct_answers(self, exam=None, student=None):
        """开考 → 全部作答 → 交卷，返回 attempt。"""
        from apps.exam.services import ExamAttemptService

        exam = exam or self.exam
        attempt = self.start(exam, student=student)
        self.save_answers(attempt, self.correct_payload(attempt))
        ExamAttemptService.submit(attempt)
        attempt.refresh_from_db()
        return attempt

    def grade_all_correct(self, exam=None, student=None, *, with_ai=True):
        """开考 → 全对作答 → 交卷 → 判分，返回 (attempt, 判分结果)。"""
        attempt = self.submit_with_correct_answers(exam=exam, student=student)
        result = self.grade(attempt, with_ai=with_ai)
        attempt.refresh_from_db()
        return attempt, result

    def grade(self, attempt, *, with_ai=False, **kwargs):
        from apps.grading.services import GradingService

        return GradingService.grade_attempt(attempt, with_ai=with_ai, **kwargs)

    def answer_of(self, attempt, question_type):
        """取某题型对应的 ``Answer`` 行（没有就建一条空的）。

        判分链路是「先有作答行、再判分」，但单元测试常常只想验「这道题的判分逻辑」，
        所以这里 ``get_or_create`` 一下——测试关注的是判分，不是「学生有没有点开这道题」。
        """
        from apps.exam.models import Answer

        mapping = self.paper_map(attempt)
        paper_question_id = mapping[question_type]["paper_question_id"]
        answer, _ = Answer.objects.get_or_create(
            attempt=attempt, paper_question_id=paper_question_id,
            defaults={"order": mapping[question_type]["number"]},
        )
        return answer

    @property
    def exam(self):
        """默认考试（每次访问按键缓存，避免同一用例里重复造卷）。"""
        cache = self.__dict__.setdefault("_exam_cache", {})
        if "default" not in cache:
            cache["default"] = self.make_exam()
        return cache["default"]

    # ------------------------------------------------------------------
    # 全客观题考试
    # ------------------------------------------------------------------
    def make_objective_exam(self, **kwargs):
        """只有四道客观题的考试。

        用来验证「全客观题 → 自动判分完成 ``AUTO_GRADED``」这条分支：
        有主观题的卷子会停在 ``REVIEWING`` 等人工复核，永远到不了 ``AUTO_GRADED``，
        所以必须有这么一份纯客观卷才测得到。
        """
        from apps.papers.services import PaperService

        paper = PaperService.create(
            data={"title": f"全客观题卷-{uuid4().hex[:6]}", "subject": SUBJECT,
                  "duration_minutes": 30, "pass_score": Decimal("5")},
            user=self.teacher,
        )
        PaperService.add_questions(
            paper=paper,
            questions=[self.questions[question_type] for question_type in (
                QuestionType.SINGLE, QuestionType.MULTIPLE,
                QuestionType.JUDGE, QuestionType.FILL,
            )],
            strict=False,
        )
        PaperService.change_status(paper, "published")
        return self.make_exam(paper=paper, **kwargs)

    def objective_payload(self, attempt) -> list:
        """一份「客观题全对」的作答（不含主观题）。"""
        mapping = self.paper_map(attempt)
        items = [
            {"paper_question_id": mapping[question_type]["paper_question_id"],
             "selected_labels": label}
            for question_type, label in CORRECT_LABELS.items()
        ]
        items.append({
            "paper_question_id": mapping[QuestionType.FILL]["paper_question_id"],
            "answer_text": FILL_ANSWER,
        })
        return items


__all__ = [
    "CORRECT_LABELS",
    "ESSAY_REFERENCE",
    "FILL_ANSWER",
    "GradingBaseTest",
    "SHORT_REFERENCE",
]
