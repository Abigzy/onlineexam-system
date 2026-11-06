"""个性化推荐测试的公共装配。

在学情分析的装配（六道题 + 父子知识点 + 三角色）之上，只补两件事：

1. **造「没被作答道过」的候选题。** 推荐要的是「还没练过的题」，
   而 ``AnalyticsBaseTest`` 那六道题全在默认试卷里，一开考就变成已作答。
   所以这里提供一个能在指定知识点、指定难度上造新题的工具——
   难度分布是推荐的第二个维度，没有难度差异就测不出「略高于当前水平」。
2. **把「薄弱」造出来。** 薄弱不是手动标的，是判分结果聚合出来的：
   ``make_weak()`` 让某位学生在默认试卷上全错，相关知识点自然落到「薄弱」档。
   要多个知识点同时薄弱，就用 ``make_exam_with_questions()`` 另造一份卷子全错。
"""

from decimal import Decimal
from uuid import uuid4

from apps.analytics.tests.base import (  # noqa: F401  （转出给各测试文件用）
    CHILD_KP_NAME,
    PARENT_KP_NAME,
    AnalyticsBaseTest,
)
from apps.common.constants import QuestionType
from apps.exam.tests.base import PASSWORD, SUBJECT  # noqa: F401
from apps.questions.models import KnowledgePoint, Question, QuestionOption

CHOICE_TYPES = (QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE)


class RecommendBaseTest(AnalyticsBaseTest):
    """个性化推荐公共装配。"""

    #: 默认把两位学生放进**默认学科**（数据结构）的学科班。
    #: 「已加入的学科」是每日练习可选范围的唯一依据，绝大多数用例都默认能练这门课；
    #: 需要验证「没加入任何学科班」的用例把它置为 ``False``。
    default_subject_classroom = True

    def setUp(self):
        super().setUp()
        # 本次用例里新造的候选题（实例级，避免跨用例互相污染）
        self.extra_questions = {}
        if self.default_subject_classroom:
            for student in (self.student, self.student_b):
                self.join_subject(SUBJECT, student=student)

    # ------------------------------------------------------------------
    # 造候选题
    # ------------------------------------------------------------------
    def add_candidate(self, *, kp=None, difficulty=2, qtype=QuestionType.SINGLE,
                      subject=None, analysis="候选题解析，仅用于推荐测试。"):
        """造一道**尚未被作答**的候选题，挂到指定知识点上。"""
        kp = kp if kp is not None else self.child_kp
        question = Question.objects.create(
            type=qtype,
            stem=f"【候选题·{qtype}】题干 {uuid4().hex[:6]}",
            difficulty=difficulty,
            score=Decimal("2"),
            subject=SUBJECT if subject is None else subject,
            reference_answer="A",
            analysis=analysis,
        )
        if qtype in CHOICE_TYPES:
            QuestionOption.objects.bulk_create([
                QuestionOption(question=question, label=label, content=f"选项{label}",
                               is_correct=(label == "A"), order=index)
                for index, label in enumerate(["A", "B", "C", "D"])
            ])
        question.knowledge_points.add(kp)
        self.extra_questions[question.id] = question
        return question

    def add_knowledge_point(self, name, *, parent=None, code=""):
        return KnowledgePoint.objects.create(
            name=name, subject=SUBJECT, parent=parent, code=code or f"RC-{uuid4().hex[:4]}",
        )

    # ------------------------------------------------------------------
    # 造「已加入的学科」
    # ------------------------------------------------------------------
    def join_subject(self, subject="", *, student=None, is_active=True):
        """让学生加入一个**学科班**。

        「已加入的学科」是每日练习可选范围的唯一依据（``selectable_subjects``），
        所以凡是要练某个学科的用例，都得先把学生放进对应的学科班。
        """
        from apps.classroom.constants import ClassType
        from apps.classroom.models import Classroom

        classroom = Classroom.objects.create(
            name=f"{subject or '无学科'}-测试班-{uuid4().hex[:6]}",
            subject=subject,
            class_type=ClassType.SUBJECT,
            is_active=is_active,
            created_by=self.teacher,
        )
        classroom.students.add(student or self.student)
        classroom.teachers.add(self.teacher)
        return classroom

    # ------------------------------------------------------------------
    # 造薄弱
    # ------------------------------------------------------------------
    def make_weak(self, student=None, *, correct_types=()):
        """在默认试卷上作答并指定「哪些题型答对」，其余答错。

        ``correct_types=()`` 表示**全错**——相关知识点正确率归零，必然薄弱。
        """
        return self.judged_attempt(
            self.exam, student=student or self.student, correct_types=set(correct_types),
        )

    def make_exam_with_questions(self, questions, *, student=None, correct_types=(), publish=True):
        """用指定的几道题组一份卷子并作答，返回 exam。

        用来在**另一个知识点**上造出薄弱：默认那场考试只覆盖「线性表」。
        """
        from apps.papers.services import PaperService

        paper = PaperService.create(
            data={"title": f"推荐测试卷-{uuid4().hex[:6]}", "subject": SUBJECT,
                  "duration_minutes": 30, "pass_score": Decimal("1")},
            user=self.teacher,
        )
        PaperService.add_questions(paper=paper, questions=list(questions), strict=False)
        if publish:
            PaperService.change_status(paper, "published")
        exam = self.make_exam(paper=paper)
        self.judged_attempt(exam, student=student or self.student,
                            correct_types=set(correct_types))
        return exam

    def make_exam_with_single_question(self, *, kp=None, difficulty=2, student=None,
                                       correct=True):
        """只有一道题的考试——用来造「样本不足」的场景。"""
        question = self.add_candidate(kp=kp, difficulty=difficulty)
        from apps.papers.services import PaperService

        paper = PaperService.create(
            data={"title": f"单题卷-{uuid4().hex[:6]}", "subject": SUBJECT,
                  "duration_minutes": 10, "pass_score": Decimal("1")},
            user=self.teacher,
        )
        PaperService.add_questions(paper=paper, questions=[question], strict=False)
        PaperService.change_status(paper, "published")
        exam = self.make_exam(paper=paper)
        self.judged_attempt(
            exam, student=student or self.student,
            correct_types={question.type} if correct else set(),
        )
        return question, exam


__all__ = [
    "AnalyticsBaseTest",
    "CHILD_KP_NAME",
    "CHOICE_TYPES",
    "PARENT_KP_NAME",
    "PASSWORD",
    "RecommendBaseTest",
    "SUBJECT",
]
