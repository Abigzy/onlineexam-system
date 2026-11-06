"""试卷模块的接口测试。

覆盖四条线：权限边界、卷子 CRUD、题目编排与生命周期、智能组卷与分析导出。
所有断言都对着**统一响应体**（code/message/data）来写，顺便把响应格式也钉死，
免得以后有人改了全局渲染器却没人发现。
"""

from apps.common.constants import QuestionType
from apps.papers.constants import PaperStatus
from apps.papers.models import Paper

from .base import SUBJECT, PaperBaseTest


# ======================================================================
# 权限
# ======================================================================
class PaperApiPermissionTests(PaperBaseTest):

    def test_anonymous_rejected(self):
        response = self.client.get("/api/v1/papers/")
        self.assertEqual(response.status_code, 401)

    def test_student_forbidden(self):
        response = self.client.get("/api/v1/papers/", **self.auth(self.student))
        self.assertEqual(response.status_code, 403)

    def test_student_cannot_create(self):
        response = self.post_json(
            "/api/v1/papers/", {"title": "学生建的卷"}, user=self.student,
        )
        self.assertEqual(response.status_code, 403)

    def test_teacher_allowed(self):
        response = self.client.get("/api/v1/papers/", **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_admin_allowed(self):
        response = self.client.get("/api/v1/papers/", **self.auth(self.admin))
        self.assertEqual(response.status_code, 200)


# ======================================================================
# 卷子 CRUD
# ======================================================================
class PaperCrudApiTests(PaperBaseTest):

    def test_create_minimal(self):
        response = self.post_json("/api/v1/papers/", {"title": "接口卷", "subject": SUBJECT})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["title"], "接口卷")
        self.assertEqual(body["data"]["status"], PaperStatus.DRAFT)
        self.assertEqual(body["data"]["question_count"], 0)
        self.assertEqual(body["data"]["sections"], [])

    def test_create_rejects_blank_title(self):
        response = self.post_json("/api/v1/papers/", {"title": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.json()["code"], 0)

    def test_create_rejects_bad_duration(self):
        response = self.post_json("/api/v1/papers/", {"title": "坏时长", "duration_minutes": 0})
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_out_of_range_difficulty(self):
        response = self.post_json(
            "/api/v1/papers/", {"title": "坏难度", "target_difficulty": 9},
        )
        self.assertEqual(response.status_code, 400)

    def test_create_with_questions(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=4)]
        response = self.post_json("/api/v1/papers/", {
            "title": "带题卷", "subject": SUBJECT, "question_ids": ids,
            "duration_minutes": 60, "pass_score": "5",
        })
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["question_count"], 4)
        self.assertEqual(data["total_score"], 8.0)
        self.assertEqual(data["pass_score"], 5.0)
        self.assertEqual(len(data["sections"]), 1)
        self.assertEqual(data["sections"][0]["title"], "一、单项选择题")

    def test_create_with_unknown_question_id_is_tolerated(self):
        """新建时带了不存在的题目 id，不该整单失败——能加的加上，加的忽略。"""
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=2)] + [999999]
        response = self.post_json("/api/v1/papers/", {"title": "含无效 id", "question_ids": ids})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["question_count"], 2)

    def test_retrieve_detail(self):
        paper = self.make_paper()
        response = self.client.get(f"/api/v1/papers/{paper.id}/", **self.auth())
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        for key in ("sections", "paper_questions", "description", "ai_notes",
                    "actual_difficulty", "target_difficulty"):
            self.assertIn(key, data)

    def test_retrieve_missing_returns_404(self):
        response = self.client.get("/api/v1/papers/999999/", **self.auth())
        self.assertEqual(response.status_code, 404)

    def test_update_title(self):
        paper = self.make_paper()
        response = self.client.patch(
            f"/api/v1/papers/{paper.id}/", {"title": "改过的标题"},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["title"], "改过的标题")

    def test_update_cannot_forge_totals(self):
        """总分、题量是派生字段，传了也不该生效。"""
        paper = self.make_paper()
        self.client.patch(
            f"/api/v1/papers/{paper.id}/",
            {"title": "想改总分", "total_score": "999", "question_count": 88},
            content_type="application/json", **self.auth(),
        )
        paper.refresh_from_db()
        self.assertEqual(float(paper.total_score), 0.0)
        self.assertEqual(paper.question_count, 0)

    def test_delete_draft(self):
        paper = self.make_paper()
        response = self.client.delete(f"/api/v1/papers/{paper.id}/", **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Paper.objects.filter(id=paper.id).exists())

    def test_delete_published_rejected(self):
        paper = self.make_paper(pass_score=2)
        self.post_json(
            f"/api/v1/papers/{paper.id}/questions/",
            {"question_ids": [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=1)]},
        )
        self.post_json(f"/api/v1/papers/{paper.id}/status/", {"status": PaperStatus.PUBLISHED})

        response = self.client.delete(f"/api/v1/papers/{paper.id}/", **self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能删除", response.json()["message"])

    def test_list_default_hides_archived(self):
        self.make_paper(title="草稿卷")
        archived = self.make_paper(title="归档卷")
        archived.status = PaperStatus.ARCHIVED
        archived.save(update_fields=["status"])

        response = self.client.get("/api/v1/papers/", **self.auth())
        titles = [row["title"] for row in response.json()["data"]["results"]]
        self.assertIn("草稿卷", titles)
        self.assertNotIn("归档卷", titles)

        # 显式传 status 时要能查到
        response = self.client.get("/api/v1/papers/?status=archived", **self.auth())
        titles = [row["title"] for row in response.json()["data"]["results"]]
        self.assertEqual(titles, ["归档卷"])

    def test_list_search(self):
        self.make_paper(title="数据结构期中卷")
        self.make_paper(title="操作系统期末卷")

        # 关键词要挑只出现在标题里的：两份卷子的 subject 都是「数据结构」，
        # 而 search 同时搜 title/subject/description，关键词带学科名会一起命中
        response = self.client.get("/api/v1/papers/?search=期中", **self.auth())
        rows = response.json()["data"]["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "数据结构期中卷")

    def test_list_pagination_shape(self):
        for index in range(12):
            self.make_paper(title=f"分页卷 {index}")
        response = self.client.get("/api/v1/papers/", **self.auth())
        data = response.json()["data"]
        for key in ("count", "total_pages", "page", "results"):
            self.assertIn(key, data)
        self.assertEqual(data["count"], 12)
        self.assertEqual(len(data["results"]), 10)


# ======================================================================
# 题目编排
# ======================================================================
class PaperQuestionApiTests(PaperBaseTest):

    def setUp(self):
        super().setUp()
        # 及格分给足，避免个别用例走到「发布」时被及格分校验拦住
        self.paper = self.make_paper(pass_score=2)

    def add(self, **payload):
        return self.post_json(f"/api/v1/papers/{self.paper.id}/questions/", payload)

    def test_add_questions(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)]
        response = self.add(question_ids=ids)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["added_count"], 3)
        self.assertEqual(data["question_count"], 3)
        self.assertEqual(data["total_score"], 6.0)
        self.assertEqual(data["duplicated_count"], 0)

    def test_add_duplicate_rejected_by_default(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=2)]
        self.add(question_ids=ids)
        response = self.add(question_ids=ids)
        self.assertEqual(response.status_code, 400)
        self.assertIn("已在本卷中", response.json()["message"])

    def test_add_duplicate_skipped_when_not_strict(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=2)]
        self.add(question_ids=ids)
        response = self.add(question_ids=ids, strict=False)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["added_count"], 0)
        self.assertEqual(data["duplicated_count"], 2)

    def test_add_with_custom_score(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=2)]
        response = self.add(question_ids=ids, score="4.5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total_score"], 9.0)

    def test_add_unknown_question_returns_400(self):
        response = self.add(question_ids=[999999])
        self.assertEqual(response.status_code, 400)
        self.assertIn("不存在", response.json()["message"])

    def test_add_empty_payload_returns_400(self):
        response = self.add(question_ids=[])
        self.assertEqual(response.status_code, 400)

    def test_remove_questions(self):
        ids = [q.id for q in self.questions_of(QuestionType.JUDGE, 2, limit=3)]
        self.add(question_ids=ids)
        paper_ids = list(self.paper.paper_questions.values_list("id", flat=True))

        response = self.post_json(
            f"/api/v1/papers/{self.paper.id}/remove-questions/", {"ids": paper_ids[:1]},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["removed_count"], 1)
        self.assertEqual(data["question_count"], 2)

    def test_batch_scores(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)]
        self.add(question_ids=ids)
        items = [
            {"id": item.id, "score": 5}
            for item in self.paper.paper_questions.order_by("order")[:2]
        ]

        response = self.post_json(f"/api/v1/papers/{self.paper.id}/scores/", {"items": items})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["updated_count"], 2)
        self.assertEqual(data["total_score"], 12.0)  # 5 + 5 + 2

    def test_batch_scores_rejects_non_positive(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=1)]
        self.add(question_ids=ids)
        item = self.paper.paper_questions.first()
        response = self.post_json(
            f"/api/v1/papers/{self.paper.id}/scores/", {"items": [{"id": item.id, "score": 0}]},
        )
        self.assertEqual(response.status_code, 400)

    def test_reorder(self):
        ids = [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)]
        self.add(question_ids=ids)
        items = list(self.paper.paper_questions.order_by("order"))

        response = self.post_json(
            f"/api/v1/papers/{self.paper.id}/reorder/",
            {"ordered_ids": [items[1].id, items[2].id, items[0].id]},
        )
        self.assertEqual(response.status_code, 200)

        by_order = list(
            self.paper.paper_questions.order_by("order").values_list("question_id", flat=True)
        )
        self.assertEqual(by_order, [items[1].question_id, items[2].question_id, items[0].question_id])

    def test_preview_groups_by_section_and_keeps_answers(self):
        self.add(question_ids=[q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)])
        self.add(question_ids=[q.id for q in self.questions_of(QuestionType.SHORT, 2, limit=2)])

        response = self.client.get(f"/api/v1/papers/{self.paper.id}/preview/", **self.auth())
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]

        self.assertEqual(len(data["sections"]), 2)
        first = data["sections"][0]
        self.assertEqual(first["question_type"], QuestionType.SINGLE)
        self.assertEqual(first["question_count"], 3)
        self.assertTrue(first["label"].startswith("一、"))
        self.assertEqual(len(first["questions"]), 3)
        # 预览是教师视角：必须带答案与选项正误标记
        self.assertIn("reference_answer", first["questions"][0])
        self.assertIn("is_correct", first["questions"][0]["options"][0])
        self.assertEqual(data["question_count"], 5)
        self.assertEqual(data["total_score"], 10.0)  # 3 道单选 × 2 分 + 2 道简答 × 2 分

    def test_add_blocked_after_publish(self):
        self.add(question_ids=[q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=2)])
        self.post_json(f"/api/v1/papers/{self.paper.id}/status/", {"status": PaperStatus.PUBLISHED})

        response = self.add(question_ids=[q.id for q in self.questions_of(QuestionType.JUDGE, 2, limit=1)])
        self.assertEqual(response.status_code, 400)
        self.assertIn("只有草稿可以修改", response.json()["message"])


# ======================================================================
# 生命周期
# ======================================================================
class PaperLifecycleApiTests(PaperBaseTest):

    def test_clone(self):
        paper = self.make_paper(title="原始卷")
        self.post_json(
            f"/api/v1/papers/{paper.id}/questions/",
            {"question_ids": [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)]},
        )

        response = self.post_json(f"/api/v1/papers/{paper.id}/clone/", {"title": "A/B 卷 B"})
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["title"], "A/B 卷 B")
        self.assertEqual(data["question_count"], 3)
        self.assertEqual(data["status"], PaperStatus.DRAFT)

    def test_clone_default_title(self):
        paper = self.make_paper(title="原始卷")
        response = self.post_json(f"/api/v1/papers/{paper.id}/clone/", {})
        self.assertEqual(response.status_code, 201)
        self.assertIn("副本", response.json()["data"]["title"])

    def test_status_flow(self):
        paper = self.make_paper(pass_score=2)
        self.post_json(
            f"/api/v1/papers/{paper.id}/questions/",
            {"question_ids": [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=2)]},
        )

        published = self.post_json(
            f"/api/v1/papers/{paper.id}/status/", {"status": PaperStatus.PUBLISHED},
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["data"]["status"], PaperStatus.PUBLISHED)
        self.assertIn("已发布", published.json()["message"])

        archived = self.post_json(
            f"/api/v1/papers/{paper.id}/status/", {"status": PaperStatus.ARCHIVED},
        )
        self.assertEqual(archived.json()["data"]["status"], PaperStatus.ARCHIVED)

    def test_publish_empty_paper_rejected(self):
        paper = self.make_paper()
        response = self.post_json(
            f"/api/v1/papers/{paper.id}/status/", {"status": PaperStatus.PUBLISHED},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("空卷", response.json()["message"])

    def test_status_rejects_unknown_value(self):
        paper = self.make_paper()
        response = self.post_json(
            f"/api/v1/papers/{paper.id}/status/", {"status": "whatever"},
        )
        self.assertEqual(response.status_code, 400)


# ======================================================================
# 智能组卷
# ======================================================================
class ComposeApiTests(PaperBaseTest):

    def test_pool_matrix(self):
        response = self.client.get(
            f"/api/v1/papers/pool/?subject={SUBJECT}", **self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total"], 144)
        single = next(row for row in data["by_type"] if row["type"] == QuestionType.SINGLE)
        self.assertEqual(single["total"], 24)
        self.assertEqual(single["by_difficulty"]["2"], 6)

    def test_ai_compose_dry_run_creates_nothing(self):
        before = Paper.objects.count()
        response = self.post_json(
            "/api/v1/papers/ai-compose/",
            {"subject": SUBJECT, "seed": 42, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200)
        report = response.json()["data"]
        self.assertEqual(report["picked"]["question_count"], 34)
        self.assertTrue(report["feasible"])
        self.assertEqual(Paper.objects.count(), before, "试算不能建卷子")

    def test_ai_compose_creates_paper(self):
        response = self.post_json(
            "/api/v1/papers/ai-compose/",
            {"subject": SUBJECT, "seed": 42, "generate_notes": True},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]

        self.assertEqual(data["paper"]["question_count"], 34)
        self.assertEqual(data["paper"]["total_score"], 100.0)
        self.assertEqual(data["paper"]["generation_mode"], "ai")
        self.assertEqual(data["report"]["feasible"], True)
        self.assertIn("组卷思路", data["notes"]["notes"])

        # 落库的卷子确实存在
        self.assertTrue(Paper.objects.filter(id=data["paper"]["id"]).exists())

    def test_ai_compose_with_custom_blueprint(self):
        response = self.post_json("/api/v1/papers/ai-compose/", {
            "subject": SUBJECT, "seed": 8,
            "generate_notes": False,
            "blueprint": [
                {"type": "single", "count": 4, "score": 5},
                {"type": "short", "count": 2, "score": 10},
            ],
        })
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["paper"]["question_count"], 6)
        self.assertEqual(data["paper"]["total_score"], 40.0)

    def test_ai_compose_rejects_duplicate_blueprint_type(self):
        response = self.post_json("/api/v1/papers/ai-compose/", {
            "subject": SUBJECT,
            "blueprint": [
                {"type": "single", "count": 2, "score": 2},
                {"type": "single", "count": 3, "score": 2},
            ],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("只能出现一次", response.json()["message"])

    def test_ai_compose_rejects_unknown_type(self):
        response = self.post_json("/api/v1/papers/ai-compose/", {
            "subject": SUBJECT,
            "blueprint": [{"type": "coding", "count": 1, "score": 10}],
        })
        self.assertEqual(response.status_code, 400)

    def test_ai_compose_reports_shortage(self):
        response = self.post_json("/api/v1/papers/ai-compose/", {
            "subject": SUBJECT, "seed": 3, "generate_notes": False,
            "knowledge_point_ids": [self.kp_a.id],
            "blueprint": [{"type": "single", "count": 20, "score": 2}],
        })
        self.assertEqual(response.status_code, 201)
        report = response.json()["data"]["report"]
        self.assertFalse(report["feasible"])
        self.assertTrue(report["shortage"])
        # 题量不足时要体现在提示语里
        self.assertIn("不足", response.json()["message"])

    def test_statistics(self):
        self.make_paper(title="统计卷")
        response = self.client.get("/api/v1/papers/statistics/", **self.auth())
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["by_status"]), 3)
        self.assertEqual(len(data["recent"]), 1)


# ======================================================================
# 分析与导出
# ======================================================================
class AnalysisAndExportApiTests(PaperBaseTest):

    def setUp(self):
        super().setUp()
        self.paper = self.make_paper(title="分析卷", pass_score=2)
        self.post_json(
            f"/api/v1/papers/{self.paper.id}/questions/",
            {"question_ids": [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=3)]},
        )
        self.post_json(
            f"/api/v1/papers/{self.paper.id}/questions/",
            {"question_ids": [q.id for q in self.questions_of(QuestionType.SHORT, 2, limit=1)]},
        )

    def test_analysis(self):
        response = self.client.get(f"/api/v1/papers/{self.paper.id}/analysis/", **self.auth())
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        for key in ("overview", "type_distribution", "difficulty_distribution",
                    "knowledge_coverage", "sections", "warnings"):
            self.assertIn(key, data)
        self.assertEqual(data["overview"]["question_count"], 4)
        self.assertEqual(len(data["difficulty_distribution"]), 4)

    def test_analysis_with_planned_points(self):
        missing = self.kp_b
        response = self.client.get(
            f"/api/v1/papers/{self.paper.id}/analysis/?planned={missing.id}", **self.auth(),
        )
        coverage = response.json()["data"]["knowledge_coverage"]
        self.assertEqual(coverage["planned_count"], 1)
        self.assertIsNotNone(coverage["coverage_ratio"])

    def test_ai_notes_generated_then_cached(self):
        first = self.post_json(f"/api/v1/papers/{self.paper.id}/ai-notes/", {})
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["data"]["cached"])
        self.assertTrue(first.json()["data"]["notes"].strip())

        second = self.post_json(f"/api/v1/papers/{self.paper.id}/ai-notes/", {})
        self.assertTrue(second.json()["data"]["cached"])

        forced = self.post_json(f"/api/v1/papers/{self.paper.id}/ai-notes/", {"force": True})
        self.assertFalse(forced.json()["data"]["cached"])

        self.paper.refresh_from_db()
        self.assertTrue(self.paper.ai_notes)

    def test_ai_notes_on_empty_paper_rejected(self):
        empty = self.make_paper(title="空卷")
        response = self.post_json(f"/api/v1/papers/{empty.id}/ai-notes/", {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("空卷", response.json()["message"])

    def test_export_html(self):
        response = self.client.get(f"/api/v1/papers/{self.paper.id}/export/", **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        content = response.content.decode("utf-8")
        self.assertIn("<!DOCTYPE html>", content)
        self.assertIn("分析卷", content)
        self.assertIn("一、单项选择题", content)
        self.assertIn("@page", content, "打印样式必须带上，否则打印出来没法看")

    def test_export_html_with_answer(self):
        response = self.client.get(
            f"/api/v1/papers/{self.paper.id}/export/?with_answer=true", **self.auth(),
        )
        content = response.content.decode("utf-8")
        self.assertIn("参考答案", content)
        self.assertIn("解析", content)

    def test_export_markdown(self):
        response = self.client.get(
            f"/api/v1/papers/{self.paper.id}/export/?file_format=md&with_answer=true",
            **self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("# 分析卷", content)
        self.assertIn("## 一、单项选择题", content)
        self.assertIn("# 参考答案", content)

    def test_export_sets_attachment_filename(self):
        response = self.client.get(f"/api/v1/papers/{self.paper.id}/export/", **self.auth())
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("UTF-8", response["Content-Disposition"])
