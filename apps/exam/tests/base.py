"""考试模块测试的公共装配。

装配策略：**六种题型各一道 + 一份已发布试卷 + 三种角色**。

为什么每型只造一道就够：考试模块要验的是「下发 / 暂存 / 计时 / 交卷 / 权限」这条链路，
不是组卷算法。题目贵精不贵多——六道题刚好把「客观题（单选/多选/判断/填空）走标签比对」
和「主观题（简答/论述）走文本」两类作答形态都覆盖到，断言也更容易读。
"""

from decimal import Decimal
from uuid import uuid4

from django.test import TestCase

from apps.ai.client import reset_ai_client
from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import QuestionType
from apps.exam.models import Exam
from apps.exam.services import ExamService
from apps.papers.services import PaperService
from apps.questions.models import KnowledgePoint, Question, QuestionOption
from apps.users.models import User

SUBJECT = "数据结构"
PASSWORD = "Exam@123456"

CHOICE_TYPES = {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE}
# 每道题的卷面分值：刻意设成不一样，好验证「按题算分」而不是「按道数算分」
SCORE_BY_TYPE = {
    QuestionType.SINGLE: "2",
    QuestionType.MULTIPLE: "3",
    QuestionType.JUDGE: "1",
    QuestionType.FILL: "2",
    QuestionType.SHORT: "5",
    QuestionType.ESSAY: "10",
}
TOTAL_SCORE = 23


class ExamBaseTest(TestCase):
    """公共装配：一份 6 题试卷 + 三角色用户 + 造考试/作答的小工具。"""

    @classmethod
    def setUpTestData(cls):
        cls.kp = KnowledgePoint.objects.create(name="线性表", subject=SUBJECT, code="EX-01")

        cls.questions = {}
        for qtype in [QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE,
                      QuestionType.FILL, QuestionType.SHORT, QuestionType.ESSAY]:
            question = Question.objects.create(
                type=qtype,
                stem=f"【{qtype}】测试题干 {uuid4().hex[:6]}",
                difficulty=2,
                score=Decimal(SCORE_BY_TYPE[qtype]),
                subject=SUBJECT,
                reference_answer="A" if qtype != QuestionType.FILL else "栈\n队列",
                analysis="（测试解析：这里应当只对教师可见）",
            )
            if qtype in CHOICE_TYPES:
                QuestionOption.objects.bulk_create([
                    QuestionOption(question=question, label=label, content=f"选项{label}",
                                   is_correct=(label == "A"), order=index)
                    for index, label in enumerate(["A", "B", "C", "D"])
                ])
            question.knowledge_points.add(cls.kp)
            cls.questions[qtype] = question

        cls.teacher = User.objects.create_user(
            username="e_teacher", email="e_teacher@example.com", password=PASSWORD,
            role="teacher", real_name="监考老师",
        )
        cls.student = User.objects.create_user(
            username="e_student", email="e_student@example.com", password=PASSWORD,
            role="student", real_name="考生甲", student_no="S2026001",
        )
        cls.student_b = User.objects.create_user(
            username="e_student_b", email="e_student_b@example.com", password=PASSWORD,
            role="student", real_name="考生乙", student_no="S2026002",
        )
        cls.admin = User.objects.create_superuser(
            username="e_admin", email="e_admin@example.com", password=PASSWORD,
        )

        # open_to_all 语义收紧后，教师创建的「面向全体」考试只对自己班的学生开放。
        # 给教师建一个行政班并纳入两名学生，让「教师出题 → 学生可考」的前提成立；
        # 需要验证「班外学生不可考」的用例请自行造一个不在该班的学生。
        cls.classroom = Classroom.objects.create(
            name="考试测试班", class_type=ClassType.ADMIN,
            created_by=cls.teacher, head_teacher=cls.teacher,
        )
        cls.classroom.students.add(cls.student, cls.student_b)

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        reset_ai_client()

    def tearDown(self):
        reset_ai_client()

    # ------------------------------------------------------------------
    # 造数据
    # ------------------------------------------------------------------
    def make_paper(self, *, publish=True, **kwargs):
        """一份含 6 道题的试卷（总分 23），默认顺手发布。"""
        data = {"title": f"考试用卷-{uuid4().hex[:6]}", "subject": SUBJECT,
                "duration_minutes": 60, "pass_score": Decimal("14")}
        data.update(kwargs)
        paper = PaperService.create(data=data, user=self.teacher)
        PaperService.add_questions(paper=paper, questions=list(self.questions.values()),
                                   strict=False)
        if publish:
            PaperService.change_status(paper, "published")
        return paper

    def make_exam(self, *, publish=True, paper=None, **kwargs):
        """造一场考试。``publish=True`` 时顺手发布（发布后学生才能开考）。"""
        paper = paper or self.make_paper()
        data = {
            "title": f"测试考试-{uuid4().hex[:6]}",
            "paper": paper,
            "duration_minutes": 60,
            "max_attempts": 1,
            "allow_resume": True,
            "detect_switch": True,
            "max_switch_count": 3,
            "detect_paste": True,
        }
        data.update(kwargs)
        exam = ExamService.create(data=data, user=self.teacher)
        if publish:
            ExamService.change_status(exam, "published")
        return exam

    def start(self, exam, student=None):
        """开考，返回 attempt。"""
        from apps.exam.services import ExamAttemptService

        student = student or self.student
        return ExamAttemptService.start(exam=exam, student=student,
                                        ip="127.0.0.1", user_agent="test-agent")["attempt"]

    def answer_payload(self, attempt):
        """构造一份「全部答对」的作答（客观题选 A，主观题写点东西）。"""
        from apps.exam.services import ExamAttemptService

        plan = ExamAttemptService.build_paper_payload(attempt)
        items = []
        for question in plan["questions"]:
            qtype = question["question_type"]
            if qtype in CHOICE_TYPES:
                items.append({"paper_question_id": question["paper_question_id"],
                              "selected_labels": "A", "time_spent_seconds": 10})
            else:
                items.append({"paper_question_id": question["paper_question_id"],
                              "answer_text": "栈\n队列" if qtype == QuestionType.FILL else "这是作答内容",
                              "time_spent_seconds": 20})
        return items

    # ------------------------------------------------------------------
    # 请求工具
    # ------------------------------------------------------------------
    def auth(self, user=None) -> dict:
        user = user or self.teacher
        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": user.username, "password": PASSWORD},
            content_type="application/json",
        )
        token = response.json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def get_json(self, url, user=None, **kwargs):
        return self.client.get(url, **{**self.auth(user), **kwargs})

    def post_json(self, url, payload=None, user=None, **kwargs):
        return self.client.post(
            url, payload or {}, content_type="application/json",
            **{**self.auth(user), **kwargs},
        )

    def patch_json(self, url, payload=None, user=None, **kwargs):
        return self.client.patch(
            url, payload or {}, content_type="application/json",
            **{**self.auth(user), **kwargs},
        )

    def delete_json(self, url, user=None, **kwargs):
        return self.client.delete(url, **{**self.auth(user), **kwargs})


__all__ = [
    "CHOICE_TYPES",
    "ExamBaseTest",
    "PASSWORD",
    "SCORE_BY_TYPE",
    "SUBJECT",
    "TOTAL_SCORE",
]
