"""纯手动组卷：从零建空卷 → 加题 → 发布。

这条链路以前是断的：组卷工作台的「从题库加题」在新建页是 disabled，
必须先用 AI 组卷生成一份草稿拿到 paper_id 才能手动挑题。
现在开放手动组卷，前端靠 `/papers/` 建空卷，后端本来就支持
（`PaperWriteSerializer.question_ids` 非必填，`PaperService.create` 默认 manual 模式）。

这里把后端能力、空卷发布规则、以及页面按钮是否解禁一并钉死。
"""

import re

from apps.common.constants import QuestionType
from apps.papers.constants import PaperGenerationMode, PaperStatus
from apps.papers.models import Paper

from .base import SUBJECT, PaperBaseTest

BTN_PICKER_RE = re.compile(r'<button[^>]*id="btnOpenPicker"[^>]*>', re.I)


class ManualComposeApiTests(PaperBaseTest):
    """接口侧：空卷能建、能加题、发布规则符合预期。"""

    def test_create_empty_paper_without_question_ids(self):
        """不带 question_ids 也能建卷，且落库就是 manual 模式的空卷。"""
        response = self.post_json("/api/v1/papers/", {
            "title": "纯手动卷",
            "subject": SUBJECT,
            "duration_minutes": 90,
            "target_difficulty": 2,
        })
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["code"], 0)

        data = body["data"]
        self.assertEqual(data["title"], "纯手动卷")
        self.assertEqual(data["generation_mode"], PaperGenerationMode.MANUAL)
        self.assertEqual(data["question_count"], 0)
        self.assertEqual(data["total_score"], 0.0)
        self.assertEqual(data["sections"], [])
        self.assertEqual(data["status"], PaperStatus.DRAFT)

        paper = Paper.objects.get(pk=data["id"])
        self.assertEqual(paper.generation_mode, PaperGenerationMode.MANUAL)
        self.assertEqual(paper.question_count, 0)

    def test_empty_paper_accepts_explicit_empty_question_ids(self):
        """前端只传卷头，但如果传了空数组也不应报错。"""
        response = self.post_json("/api/v1/papers/", {
            "title": "空数组卷", "subject": SUBJECT, "question_ids": [],
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["question_count"], 0)

    def test_add_questions_to_empty_paper(self):
        """空卷加 3 道 2 分题：题量与总分都要重算出来。"""
        created = self.post_json("/api/v1/papers/", {"title": "手动加题卷", "subject": SUBJECT})
        paper_id = created.json()["data"]["id"]

        question_ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, 3)]
        self.assertEqual(len(question_ids), 3)

        response = self.post_json(
            f"/api/v1/papers/{paper_id}/questions/",
            {"question_ids": question_ids, "strict": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["added_count"], 3)

        detail = self.client.get(f"/api/v1/papers/{paper_id}/", **self.auth()).json()["data"]
        self.assertEqual(detail["question_count"], 3)
        self.assertEqual(detail["total_score"], 6.0)  # 3 道 × 2 分

        paper = Paper.objects.get(pk=paper_id)
        self.assertEqual(paper.question_count, 3)
        self.assertEqual(float(paper.total_score), 6.0)

    def test_publish_rejects_empty_paper(self):
        """现有业务规则：空卷不能发布（服务层校验，code 13006）。"""
        created = self.post_json("/api/v1/papers/", {"title": "空卷待发", "subject": SUBJECT})
        paper_id = created.json()["data"]["id"]

        response = self.post_json(f"/api/v1/papers/{paper_id}/status/", {"status": "published"})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], 13006)
        self.assertIn("空卷不能发布", body["message"])

        paper = Paper.objects.get(pk=paper_id)
        self.assertEqual(paper.status, PaperStatus.DRAFT)
        self.assertIsNone(paper.published_at)

    def test_publish_rejects_when_pass_score_above_total(self):
        """另一条发布前置：及格分不能高于总分（默认及格分 60，少量题时必然触发）。"""
        created = self.post_json("/api/v1/papers/", {"title": "及格分过高", "subject": SUBJECT})
        paper_id = created.json()["data"]["id"]
        question_ids = [q.id for q in self.questions_of(QuestionType.JUDGE, 2, 2)]
        self.post_json(
            f"/api/v1/papers/{paper_id}/questions/",
            {"question_ids": question_ids, "strict": False},
        )

        response = self.post_json(f"/api/v1/papers/{paper_id}/status/", {"status": "published"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 13007)
        self.assertIn("不能高于总分", response.json()["message"])

    def test_publish_succeeds_after_adding_questions(self):
        """同一份卷子加上题、且及格分不超过总分后就能发布——说明卡住手动组卷的只是前端。"""
        # 及格分给 0：默认的 60 分要求总分大于 60，单测里没必要造那么多题
        created = self.post_json("/api/v1/papers/", {
            "title": "加题后发布", "subject": SUBJECT, "pass_score": 0,
        })
        paper_id = created.json()["data"]["id"]
        question_ids = [q.id for q in self.questions_of(QuestionType.JUDGE, 2, 2)]
        self.post_json(
            f"/api/v1/papers/{paper_id}/questions/",
            {"question_ids": question_ids, "strict": False},
        )

        response = self.post_json(f"/api/v1/papers/{paper_id}/status/", {"status": "published"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], PaperStatus.PUBLISHED)


class ManualComposePageTests(PaperBaseTest):
    """页面侧：新建页与已有试卷页的「从题库加题」都不该是 disabled。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher)

    def _picker_button(self, html: str) -> str:
        match = BTN_PICKER_RE.search(html)
        self.assertIsNotNone(match, "页面里找不到 #btnOpenPicker 按钮")
        return match.group(0)

    def test_new_page_picker_button_enabled(self):
        """/papers/new/：按钮解禁，并且页面里带着建卷逻辑。"""
        response = self.client.get("/papers/new/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        button = self._picker_button(html)
        self.assertNotIn("disabled", button)

        # 手动组卷靠这两个函数：先 ensurePaper 落空卷，再统一的建卷后开关
        self.assertIn("ensurePaper", html)
        self.assertIn("enablePaperEditing", html)

    def test_existing_paper_page_picker_button_enabled(self):
        """已存在试卷的页面同样可用（回归：别把解禁做成只对新建页生效）。"""
        paper = self.make_paper()

        response = self.client.get(f"/papers/{paper.id}/compose/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        button = self._picker_button(html)
        self.assertNotIn("disabled", button)
        # 已有试卷是服务端注入 paper_id，不走 ensurePaper 建卷分支
        self.assertIn(f"paperId: {paper.id}", html)
