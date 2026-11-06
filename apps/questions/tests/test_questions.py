"""
题库模块测试。

重点验证三件事：
  1. 题型规则（单选必须唯一正确、判断题选项固定、主观题必须给参考答案）被真正拦住；
  2. **学生视角永远拿不到答案**；
  3. AI 出题 → 草稿 → 确认入库 → 统计 这条链路走得通（离线走 mock）。
"""

import io
import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.ai.client import reset_ai_client
from apps.common.constants import Difficulty, QuestionType
from apps.questions.constants import QuestionSource
from apps.questions.importers import COLUMNS, build_export_rows, write_csv
from apps.questions.models import KnowledgePoint, Question, QuestionOption
from apps.questions.serializers import QuestionStudentSerializer
from apps.questions.services import QuestionService

User = get_user_model()
PASSWORD = "Exam@123456"


class BaseApiTest(TestCase):
    """公共装配：一个教师、一个学生、一个管理员，以及带 token 的请求头。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="q_admin", email="q_admin@example.com", password=PASSWORD
        )
        cls.teacher = User.objects.create_user(
            username="q_teacher", email="q_teacher@example.com", password=PASSWORD,
            role="teacher", real_name="出题老师",
        )
        cls.student = User.objects.create_user(
            username="q_student", email="q_student@example.com", password=PASSWORD, role="student"
        )

    def setUp(self):
        # 登录接口带 auth 限流（30/min），而限流计数存在默认缓存里、同一个测试进程共享。
        # 不清理的话，跑到第 30 个用例之后所有登录都会 429。生产环境不需要这一步。
        cache.clear()
        reset_ai_client()

    def tearDown(self):
        reset_ai_client()

    def auth(self, user=None):
        user = user or self.teacher
        token = self.client.post(
            "/api/v1/auth/login/", {"username": user.username, "password": PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ==================================================================
# 知识点
# ==================================================================
class KnowledgePointApiTest(BaseApiTest):
    url = "/api/v1/knowledge-points/"

    def test_student_forbidden(self):
        self.assertEqual(self.client.get(self.url, **self.auth(self.student)).status_code, 403)

    def test_create_and_full_path(self):
        root = KnowledgePoint.objects.create(name="数据结构", subject="计算机", created_by=self.teacher)
        child = KnowledgePoint.objects.create(name="树", subject="计算机", parent=root)
        leaf = KnowledgePoint.objects.create(name="二叉树", subject="计算机", parent=child)

        self.assertEqual(leaf.full_path, "数据结构 / 树 / 二叉树")
        self.assertEqual(leaf.level, 3)
        self.assertEqual(sorted(root.descendant_ids()), sorted([root.id, child.id, leaf.id]))

    def test_create_via_api(self):
        resp = self.client.post(
            self.url, {"name": "操作系统", "subject": "计算机", "code": "CS-OS"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["name"], "操作系统")

    def test_duplicate_name_rejected(self):
        KnowledgePoint.objects.create(name="数据库", subject="计算机")
        resp = self.client.post(
            self.url, {"name": "数据库", "subject": "计算机"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_parent_cannot_be_self(self):
        point = KnowledgePoint.objects.create(name="算法", subject="计算机")
        resp = self.client.patch(
            f"{self.url}{point.id}/", {"parent": point.id},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_tree_endpoint(self):
        root = KnowledgePoint.objects.create(name="数据结构", subject="计算机")
        KnowledgePoint.objects.create(name="树", subject="计算机", parent=root)
        KnowledgePoint.objects.create(name="图", subject="计算机", parent=root)

        resp = self.client.get(f"{self.url}tree/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(len(data[0]["children"]), 2)

    def test_delete_blocked_when_has_questions(self):
        point = KnowledgePoint.objects.create(name="排序", subject="计算机")
        question = Question.objects.create(type=QuestionType.SINGLE, stem="题干内容", subject="计算机")
        question.knowledge_points.add(point)

        resp = self.client.delete(f"{self.url}{point.id}/", **self.auth())
        self.assertEqual(resp.status_code, 400)

    def test_delete_success_without_questions(self):
        point = KnowledgePoint.objects.create(name="空知识点", subject="计算机")
        resp = self.client.delete(f"{self.url}{point.id}/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(KnowledgePoint.objects.filter(pk=point.pk).exists())

    def test_delete_blocked_when_child_has_questions(self):
        """子知识点上有题目，删父节点也要拒绝。

        父节点的 ``questions`` 是空的，但删它会级联删掉子节点，
        子节点上的题目就会静默失去知识点归属——所以检查必须覆盖整棵子树。
        """
        parent = KnowledgePoint.objects.create(name="数据结构", subject="计算机")
        child = KnowledgePoint.objects.create(name="树", subject="计算机", parent=parent)
        question = Question.objects.create(
            type=QuestionType.SINGLE, stem="树的深度", subject="计算机",
        )
        question.knowledge_points.add(child)

        resp = self.client.delete(f"{self.url}{parent.id}/", **self.auth())
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], 12030)
        self.assertIn("子知识点", resp.json()["message"])
        # 拒绝后父子都不能被删（别删了一半）
        self.assertTrue(KnowledgePoint.objects.filter(pk=parent.pk).exists())
        self.assertTrue(KnowledgePoint.objects.filter(pk=child.pk).exists())

    def test_delete_cascades_children_when_clean(self):
        """子树没有题目时允许级联删除，并如实告知删掉几个子节点。"""
        parent = KnowledgePoint.objects.create(name="操作系统", subject="计算机")
        for index in range(2):
            KnowledgePoint.objects.create(
                name=f"子节点{index}", subject="计算机", parent=parent,
            )

        resp = self.client.delete(f"{self.url}{parent.id}/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("含 2 个子知识点", resp.json()["message"])
        self.assertFalse(KnowledgePoint.objects.filter(name="操作系统").exists())
        self.assertFalse(KnowledgePoint.objects.filter(name__startswith="子节点").exists())

    def test_delete_ignores_inactive_questions(self):
        """已停用的题目不该阻塞知识点整理。"""
        point = KnowledgePoint.objects.create(name="废弃节点", subject="计算机")
        question = Question.objects.create(
            type=QuestionType.SINGLE, stem="停用题目", subject="计算机", is_active=False,
        )
        question.knowledge_points.add(point)

        resp = self.client.delete(f"{self.url}{point.id}/", **self.auth())
        self.assertEqual(resp.status_code, 200)


# ==================================================================
# 题目：题型规则
# ==================================================================
class QuestionValidationTest(BaseApiTest):
    url = "/api/v1/questions/"

    def _payload(self, **overrides):
        data = {
            "type": QuestionType.SINGLE,
            "stem": "二叉树的中序遍历顺序是？",
            "difficulty": Difficulty.MEDIUM,
            "score": 2,
            "subject": "数据结构",
            "options": [
                {"label": "A", "content": "根左右", "is_correct": False},
                {"label": "B", "content": "左根右", "is_correct": True},
            ],
        }
        data.update(overrides)
        return data

    def test_create_single_choice(self):
        resp = self.client.post(self.url, self._payload(), content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 201)
        body = resp.json()["data"]
        self.assertEqual(body["reference_answer"], "B")  # 自动冗余答案字母
        self.assertEqual(len(body["options"]), 2)

    def test_option_content_cannot_be_blank(self):
        """空内容的选项必须被拒。

        BUG-02 的修法是**前端过滤空行**（``collect()`` 里 ``filter(o => o.content)``），
        不是放宽后端。这条守门防止有人反过来把 ``allow_blank`` 改成 True——
        那样空行会静默入库，题库里出现「空白选项」的脏数据。
        """
        payload = self._payload(options=[
            {"label": "A", "content": "甲", "is_correct": True},
            {"label": "B", "content": "", "is_correct": False},
        ])
        resp = self.client.post(
            self.url, payload, content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_single_choice_needs_exactly_one_correct(self):
        payload = self._payload(options=[
            {"label": "A", "content": "甲", "is_correct": True},
            {"label": "B", "content": "乙", "is_correct": True},
        ])
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("1 个正确选项", json.dumps(resp.json(), ensure_ascii=False))

    def test_choice_needs_at_least_two_options(self):
        payload = self._payload(options=[{"label": "A", "content": "唯一选项", "is_correct": True}])
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 400)

    def test_judge_options_normalized(self):
        payload = {
            "type": QuestionType.JUDGE, "stem": "HTTP 是无状态协议。",
            "subject": "网络", "score": 1,
            "options": [{"label": "A", "content": "随便写的", "is_correct": True},
                        {"label": "B", "content": "也是随便写的", "is_correct": False}],
        }
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 201)
        options = {o["label"]: o["content"] for o in resp.json()["data"]["options"]}
        self.assertEqual(options, {"A": "正确", "B": "错误"})

    def test_multiple_choice_allows_many_correct(self):
        payload = {
            "type": QuestionType.MULTIPLE, "stem": "下列属于 HTTP 方法的有？",
            "subject": "网络", "score": 3,
            "options": [
                {"label": "A", "content": "GET", "is_correct": True},
                {"label": "B", "content": "POST", "is_correct": True},
                {"label": "C", "content": "FETCH", "is_correct": False},
            ],
        }
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["reference_answer"], "AB")

    def test_subjective_requires_reference_answer(self):
        payload = {"type": QuestionType.SHORT, "stem": "简述快速排序的思想。", "subject": "算法"}
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 400)

    def test_subjective_without_options_is_fine(self):
        payload = {"type": QuestionType.ESSAY, "stem": "论述分治法的适用条件。",
                   "reference_answer": "要点一\n要点二", "subject": "算法"}
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["options"], [])

    def test_default_score_by_type(self):
        payload = {"type": QuestionType.ESSAY, "stem": "论述题题干内容。",
                   "reference_answer": "参考答案", "subject": "算法"}
        resp = self.client.post(self.url, payload, content_type="application/json", **self.auth())
        self.assertEqual(float(resp.json()["data"]["score"]), 10.0)

    def test_short_stem_rejected(self):
        resp = self.client.post(self.url, self._payload(stem="短"),
                                content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 400)


# ==================================================================
# 题目：更新 / 删除 / 权限
# ==================================================================
class QuestionCrudTest(BaseApiTest):
    url = "/api/v1/questions/"

    def setUp(self):
        super().setUp()
        self.question = Question.objects.create(
            type=QuestionType.SINGLE, stem="原始题干内容", subject="数据结构",
            difficulty=Difficulty.EASY, score=2, reference_answer="A", created_by=self.teacher,
        )
        QuestionOption.objects.create(question=self.question, label="A", content="对", is_correct=True)
        QuestionOption.objects.create(question=self.question, label="B", content="错", is_correct=False)

    def test_student_forbidden(self):
        self.assertEqual(self.client.get(self.url, **self.auth(self.student)).status_code, 403)

    def test_update_options_replaced(self):
        resp = self.client.patch(
            f"{self.url}{self.question.id}/",
            {"options": [{"label": "A", "content": "新对", "is_correct": False},
                         {"label": "B", "content": "新错", "is_correct": True}]},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.question.refresh_from_db()
        self.assertEqual(self.question.correct_labels(), ["B"])
        self.assertEqual(self.question.options.count(), 2)

    def test_delete_is_soft(self):
        resp = self.client.delete(f"{self.url}{self.question.id}/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.question.refresh_from_db()
        self.assertFalse(self.question.is_active)
        self.assertTrue(Question.objects.filter(pk=self.question.pk).exists())

    def test_detail_exposes_answer_for_teacher(self):
        resp = self.client.get(f"{self.url}{self.question.id}/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reference_answer", resp.json()["data"])
        self.assertTrue(any(o["is_correct"] is not None for o in resp.json()["data"]["options"]))


# ==================================================================
# 答案防泄漏
# ==================================================================
class StudentViewTest(BaseApiTest):
    """学生视角序列化器绝不能带答案。"""

    def setUp(self):
        super().setUp()
        self.question = Question.objects.create(
            type=QuestionType.SINGLE, stem="题干内容需要够长", reference_answer="A",
        )
        QuestionOption.objects.create(question=self.question, label="A", content="正确答案", is_correct=True)
        QuestionOption.objects.create(question=self.question, label="B", content="错误答案", is_correct=False)

    def test_serializer_excludes_answer_fields(self):
        data = QuestionStudentSerializer(self.question).data
        dumped = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("reference_answer", dumped)
        self.assertNotIn("analysis", dumped)
        self.assertNotIn("is_correct", dumped)
        self.assertNotIn("correct", dumped)
        self.assertEqual(len(data["options"]), 2)
        self.assertEqual(set(data["options"][0].keys()), {"label", "content"})

    def test_student_view_dict(self):
        data = self.question.student_view()
        self.assertEqual(set(data.keys()),
                         {"id", "type", "type_display", "stem", "difficulty", "score", "options"})


# ==================================================================
# 筛选
# ==================================================================
class QuestionFilterTest(BaseApiTest):
    url = "/api/v1/questions/"

    def setUp(self):
        super().setUp()
        self.tree = KnowledgePoint.objects.create(name="树", subject="数据结构")
        self.binary = KnowledgePoint.objects.create(name="二叉树", subject="数据结构", parent=self.tree)
        self.graph = KnowledgePoint.objects.create(name="图", subject="数据结构")

        self.q1 = Question.objects.create(type=QuestionType.SINGLE, stem="二叉树相关题目内容",
                                          difficulty=Difficulty.EASY, subject="数据结构")
        self.q1.knowledge_points.add(self.binary)
        self.q2 = Question.objects.create(type=QuestionType.ESSAY, stem="图的遍历论述题内容",
                                          difficulty=Difficulty.HARD, subject="数据结构")
        self.q2.knowledge_points.add(self.graph)

    def test_filter_by_type(self):
        resp = self.client.get(f"{self.url}?type=essay", **self.auth())
        self.assertEqual(resp.json()["data"]["count"], 1)

    def test_filter_by_difficulty_range(self):
        resp = self.client.get(f"{self.url}?difficulty_gte=2", **self.auth())
        self.assertEqual(resp.json()["data"]["count"], 1)

    def test_filter_by_parent_knowledge_point_includes_descendants(self):
        """选「树」时，挂在「二叉树」下的题目也要被搜出来。"""
        resp = self.client.get(f"{self.url}?knowledge_point={self.tree.id}", **self.auth())
        self.assertEqual(resp.json()["data"]["count"], 1)
        self.assertEqual(resp.json()["data"]["results"][0]["id"], self.q1.id)

    def test_keyword_search(self):
        resp = self.client.get(f"{self.url}?keyword=二叉树", **self.auth())
        self.assertEqual(resp.json()["data"]["count"], 1)

    def test_search_param(self):
        resp = self.client.get(f"{self.url}?search=论述", **self.auth())
        self.assertEqual(resp.json()["data"]["count"], 1)

    def test_pagination_fields(self):
        resp = self.client.get(self.url, **self.auth())
        data = resp.json()["data"]
        for key in ("count", "total_pages", "page", "page_size", "results"):
            self.assertIn(key, data)


# ==================================================================
# AI 出题 / 批量入库
# ==================================================================
class AIGenerateFlowTest(BaseApiTest):
    def test_ai_generate_requires_material(self):
        resp = self.client.post("/api/v1/questions/ai-generate/", {},
                                content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 400)

    def test_ai_generate_returns_drafts(self):
        payload = {
            "subject": "数据结构",
            "knowledge_points": ["二叉树", "图的遍历"],
            "question_types": ["single", "judge"],
            "difficulty": Difficulty.MEDIUM,
            "count": 4,
            "source_text": "二叉树是每个节点最多有两个子树的树结构。",
        }
        resp = self.client.post("/api/v1/questions/ai-generate/", payload,
                                content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data["drafts"]), 4)
        self.assertTrue(data["meta"]["is_mock"])
        self.assertEqual(data["drafts"][0]["knowledge_point_names"], ["二叉树"])
        # 草稿不应该落库
        self.assertEqual(Question.objects.count(), 0)

    def test_batch_create_from_drafts(self):
        generated = self.client.post(
            "/api/v1/questions/ai-generate/",
            {"subject": "数据结构", "knowledge_points": ["二叉树"], "question_types": ["single"],
             "count": 3, "source_text": "素材内容"},
            content_type="application/json", **self.auth(),
        ).json()["data"]["drafts"]

        resp = self.client.post(
            "/api/v1/questions/batch/",
            {"items": generated, "subject": "数据结构", "source": QuestionSource.AI},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["created_count"], 3)
        self.assertEqual(Question.objects.count(), 3)

        # 知识点被自动创建并关联
        question = Question.objects.first()
        self.assertEqual([kp.name for kp in question.knowledge_points.all()], ["二叉树"])
        self.assertEqual(question.source, QuestionSource.AI)

    def test_batch_create_tolerates_bad_items(self):
        resp = self.client.post(
            "/api/v1/questions/batch/",
            {"items": [
                {"type": "short", "stem": "有效的主观题题干内容", "reference_answer": "答案"},
                {"type": "short", "stem": "缺答案的主观题题干"},
            ]},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["created_count"], 1)
        self.assertEqual(len(resp.json()["data"]["errors"]), 1)

    def test_ai_explain_saves_analysis(self):
        question = Question.objects.create(type=QuestionType.SINGLE, stem="题干内容足够长",
                                           reference_answer="A")
        QuestionOption.objects.create(question=question, label="A", content="对", is_correct=True)
        QuestionOption.objects.create(question=question, label="B", content="错", is_correct=False)

        resp = self.client.post(f"/api/v1/questions/{question.id}/ai-explain/", {},
                                content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 200)
        question.refresh_from_db()
        self.assertTrue(question.analysis)

    def test_ai_grade_rejects_objective_question(self):
        question = Question.objects.create(type=QuestionType.SINGLE, stem="题干内容足够长")
        resp = self.client.post(f"/api/v1/questions/{question.id}/ai-grade/",
                                {"student_answer": "A"},
                                content_type="application/json", **self.auth())
        self.assertEqual(resp.status_code, 400)

    def test_ai_grade_subjective_with_thresholds(self):
        question = Question.objects.create(
            type=QuestionType.SHORT, stem="简述二叉树遍历方式。", score=10,
            reference_answer="前序遍历\n中序遍历\n后序遍历",
        )
        resp = self.client.post(
            f"/api/v1/questions/{question.id}/ai-grade/",
            {"student_answer": "前序遍历、中序遍历、后序遍历三种方式。"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("score_ratio", data)
        self.assertIn("score", data)
        self.assertLessEqual(data["score"], 10)


# ==================================================================
# 导入 / 导出
# ==================================================================
class ImportExportTest(BaseApiTest):
    def _csv_file(self, rows, filename="questions.csv"):
        return SimpleUploadedFile(filename, write_csv(rows), content_type="text/csv")

    def test_template_download(self):
        resp = self.client.get("/api/v1/questions/import-template/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("题干", resp.content.decode("utf-8-sig"))

    def test_import_csv_dry_run(self):
        rows = [COLUMNS, ["", "二叉树", "数据结构", "单选题", "中等", "2", "题干内容够长",
                          "选项一", "选项二", "", "", "", "", "B", ""]]
        resp = self.client.post(
            "/api/v1/questions/import/",
            {"file": self._csv_file(rows), "dry_run": "true"},
            **self.auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["parsed"], 1)
        self.assertEqual(Question.objects.count(), 0)

    def test_import_csv_real(self):
        rows = [COLUMNS, ["", "二叉树", "数据结构", "单选题", "中等", "2", "题干内容够长",
                          "选项一", "选项二", "", "", "", "", "B", "解析内容"]]
        resp = self.client.post(
            "/api/v1/questions/import/",
            {"file": self._csv_file(rows)},
            **self.auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["created_count"], 1)
        question = Question.objects.get()
        self.assertEqual(question.source, QuestionSource.IMPORT)
        self.assertEqual(question.correct_labels(), ["B"])
        self.assertEqual([kp.name for kp in question.knowledge_points.all()], ["二叉树"])

    def test_import_reports_bad_rows(self):
        rows = [
            COLUMNS,
            ["", "", "", "单选题", "", "", "缺少正确答案的题", "甲", "乙", "", "", "", "", "", ""],
            ["", "", "", "不存在的题型", "", "", "题干内容够长", "", "", "", "", "", "", "", ""],
        ]
        resp = self.client.post("/api/v1/questions/import/",
                                {"file": self._csv_file(rows)}, **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["error_count"], 2)

    def test_export_csv(self):
        Question.objects.create(type=QuestionType.SINGLE, stem="导出用的题干内容",
                                reference_answer="A", subject="数据结构")
        resp = self.client.get("/api/v1/questions/export/?file_format=csv", **self.auth())
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode("utf-8-sig")
        self.assertIn("导出用的题干内容", text)

    def test_export_xlsx(self):
        Question.objects.create(type=QuestionType.SINGLE, stem="导出用的题干内容",
                                reference_answer="A", subject="数据结构")
        resp = self.client.get("/api/v1/questions/export/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        self.assertGreater(len(resp.content), 1000)

    def test_build_export_rows_includes_header(self):
        rows = build_export_rows(Question.objects.none())
        self.assertEqual(rows, [COLUMNS])


# ==================================================================
# 统计
# ==================================================================
class StatisticsTest(BaseApiTest):
    def setUp(self):
        super().setUp()
        for index in range(3):
            Question.objects.create(
                type=QuestionType.SINGLE if index < 2 else QuestionType.ESSAY,
                stem=f"统计用题干内容 {index}",
                difficulty=Difficulty.EASY + index,
                subject="数据结构",
                source=QuestionSource.AI if index == 0 else QuestionSource.MANUAL,
            )

    def test_statistics_shape(self):
        resp = self.client.get("/api/v1/questions/statistics/", **self.auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["subjective_count"], 1)
        self.assertEqual(len(data["by_difficulty"]), 4)
        self.assertGreater(data["average_difficulty"], 0)

        by_source = {row["source"]: row["count"] for row in data["by_source"]}
        self.assertEqual(by_source.get("ai"), 1)
        self.assertEqual(by_source.get("manual"), 2)


# ==================================================================
# 服务层
# ==================================================================
class QuestionServiceTest(BaseApiTest):
    def test_resolve_creates_missing_knowledge_point(self):
        points = QuestionService.resolve_knowledge_points(
            names=["全新知识点"], subject="新学科", user=self.teacher
        )
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].subject, "新学科")
        self.assertIsNone(points[0].parent)

    def test_resolve_reuses_existing(self):
        KnowledgePoint.objects.create(name="已有知识点", subject="学科A")
        first = QuestionService.resolve_knowledge_points(names=["已有知识点"], subject="学科A")
        second = QuestionService.resolve_knowledge_points(names=["已有知识点"], subject="学科A")
        self.assertEqual(first[0].id, second[0].id)
        self.assertEqual(KnowledgePoint.objects.filter(name="已有知识点").count(), 1)

    def test_accuracy_property(self):
        question = Question.objects.create(type=QuestionType.SINGLE, stem="题干内容够长",
                                           usage_count=10, correct_count=7)
        self.assertAlmostEqual(question.accuracy, 0.7)

    def test_validate_options_ignores_options_for_subjective(self):
        self.assertEqual(QuestionService.validate_options("essay", [{"content": "x"}]), [])
