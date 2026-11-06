"""试卷模块测试的公共装配。

自建题库的策略：**两个知识点 × 六种题型 × 四个难度档 × 每格 6 道题**（共 144 道）。
"每格 6 道"这个数字是照着默认蓝图算出来的——默认蓝图里单选要 10 道，
按难度配比 20/60/20 拆开就是 2/6/2，所以某个难度档最多需要 6 道。
格子里的题量够，组卷才不会因为库存不足而走回退路径，测试才能稳定断言。
"""

from decimal import Decimal
from uuid import uuid4

from django.test import TestCase

from apps.ai.client import reset_ai_client
from apps.common.constants import QuestionType
from apps.questions.models import KnowledgePoint, Question, QuestionOption
from apps.papers.services import PaperService
from apps.users.models import User

SUBJECT = "数据结构"
PASSWORD = "Exam@123456"

CHOICE_TYPES = {QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE}
ALL_TYPES = [
    QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE,
    QuestionType.FILL, QuestionType.SHORT, QuestionType.ESSAY,
]


def make_question(qtype, difficulty, *, kp=None, score=2, stem=None, subject=SUBJECT):
    """造一道题。选择题会带上 A~D 四个选项（A 正确）。"""
    question = Question.objects.create(
        type=qtype,
        stem=stem or f"{qtype}-d{difficulty}-{uuid4().hex[:8]}",
        difficulty=difficulty,
        score=Decimal(str(score)),
        subject=subject,
        reference_answer="A",
        analysis="（测试解析）",
    )
    if qtype in CHOICE_TYPES:
        QuestionOption.objects.bulk_create([
            QuestionOption(question=question, label=label, content=f"选项{label}",
                           is_correct=(label == "A"), order=index)
            for index, label in enumerate(["A", "B", "C", "D"])
        ])
    if kp is not None:
        question.knowledge_points.add(kp)
    return question


class PaperBaseTest(TestCase):
    """公共装配：题库 + 三个角色的用户。"""

    @classmethod
    def setUpTestData(cls):
        cls.kp_a = KnowledgePoint.objects.create(name="栈", subject=SUBJECT, code="T-A")
        cls.kp_b = KnowledgePoint.objects.create(name="队列", subject=SUBJECT, code="T-B")

        bulk = []
        for qtype in ALL_TYPES:
            for difficulty in (1, 2, 3, 4):
                for index in range(6):
                    bulk.append(Question(
                        type=qtype,
                        stem=f"{qtype}-d{difficulty}-{index}-{uuid4().hex[:6]}",
                        difficulty=difficulty,
                        score=Decimal("2"),
                        subject=SUBJECT,
                        reference_answer="A",
                        analysis="（测试解析）",
                    ))
        Question.objects.bulk_create(bulk)

        # bulk_create 在 MySQL 上不保证回填主键，所以统一从库里查回来再挂关联
        all_questions = list(Question.objects.order_by("id"))

        # 交替挂到两个知识点上，组卷做「知识点轮转」时才有得可轮
        for index, question in enumerate(all_questions):
            question.knowledge_points.add(cls.kp_a if index % 2 == 0 else cls.kp_b)

        # 选择题补上选项（A 正确）：没有选项就没法验证「学生视角不含答案」、
        # 卷面预览的选项渲染这类逻辑
        QuestionOption.objects.bulk_create([
            QuestionOption(question=question, label=label, content=f"选项{label}",
                           is_correct=(label == "A"), order=index)
            for question in all_questions if question.type in CHOICE_TYPES
            for index, label in enumerate(["A", "B", "C", "D"])
        ])

        cls.teacher = User.objects.create_user(
            username="p_teacher", email="p_teacher@example.com", password=PASSWORD,
            role="teacher", real_name="组卷老师",
        )
        cls.student = User.objects.create_user(
            username="p_student", email="p_student@example.com", password=PASSWORD,
            role="student", real_name="学生甲",
        )
        cls.admin = User.objects.create_superuser(
            username="p_admin", email="p_admin@example.com", password=PASSWORD,
        )

    def setUp(self):
        # 限流计数存在默认缓存里、同进程共享；不清会在跑够几十个用例后集体 429
        from django.core.cache import cache

        cache.clear()
        reset_ai_client()

    def tearDown(self):
        reset_ai_client()

    # ------------------------------------------------------------------
    def make_paper(self, **kwargs):
        data = {"title": "测试卷", "subject": SUBJECT, "duration_minutes": 90}
        data.update(kwargs)
        return PaperService.create(data=data, user=self.teacher)

    def questions_of(self, qtype, difficulty, limit=None, kp=None) -> list:
        queryset = Question.objects.filter(type=qtype, difficulty=difficulty)
        if kp is not None:
            queryset = queryset.filter(knowledge_points=kp)
        rows = list(queryset.order_by("id"))
        return rows[:limit] if limit else rows

    def auth(self, user=None) -> dict:
        """拿 JWT 请求头。"""
        user = user or self.teacher
        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": user.username, "password": PASSWORD},
            content_type="application/json",
        )
        token = response.json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def post_json(self, url, payload=None, user=None, **kwargs):
        return self.client.post(
            url, payload or {}, content_type="application/json",
            **{**self.auth(user), **kwargs},
        )


__all__ = ["ALL_TYPES", "CHOICE_TYPES", "PASSWORD", "SUBJECT", "PaperBaseTest", "make_question"]
