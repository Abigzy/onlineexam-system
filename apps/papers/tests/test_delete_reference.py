"""BUG-01 回归：删除「被考试引用的草稿试卷」必须返回 4xx，不能 500。

来源：UI 自动化阶段（``D:/onlineexam-ui-autotest/docs/缺陷管理.md`` BUG-01）。

复现：建卷 → 发布 → 建考试引用它 → 退回草稿 → ``DELETE /papers/{id}/``
原本返回 ``HTTP 500 / 50000``。

根因：``PaperService.delete`` 只判了 ``is_published``，**没查引用**。而
``Exam.paper`` 是 ``on_delete=PROTECT``，退回草稿后直接落到 ORM 删除，
``ProtectedError`` 被统一异常处理器兜成 500——错误处理违反接口规范，
UI 上表现为整页错误。
"""

from apps.common.constants import QuestionType
from apps.papers.constants import PaperStatus
from apps.exam.services import ExamService
from apps.papers.models import Paper
from apps.papers.services import PaperService

from .base import PaperBaseTest

PASSWORD = "Exam@123456"


class PaperDeleteReferenceTest(PaperBaseTest):
    def setUp(self):
        super().setUp()
        self.paper = self.make_paper(pass_score=2)
        self.post_json(
            f"/api/v1/papers/{self.paper.id}/questions/",
            {"question_ids": [q.id for q in self.questions_of(QuestionType.SINGLE, 2, limit=1)]},
        )

    # ------------------------------------------------------------------
    def _publish(self):
        self.post_json(
            f"/api/v1/papers/{self.paper.id}/status/", {"status": PaperStatus.PUBLISHED},
        )
        self.paper.refresh_from_db()

    def _make_exam(self):
        """建一场引用该试卷的考试（建考试要求卷已发布）。"""
        return ExamService.create(
            data={
                "title": "引用该卷的考试",
                "paper": self.paper,
                "duration_minutes": 60,
                "max_attempts": 1,
                "allow_resume": True,
            },
            user=self.teacher,
        )

    def _delete(self):
        return self.client.delete(f"/api/v1/papers/{self.paper.id}/", **self.auth())

    # ------------------------------------------------------------------
    def test_referenced_draft_paper_returns_4xx_not_500(self):
        """核心回归：完整走一遍缺陷里的 5 步。"""
        self._publish()
        self._make_exam()
        # 退回草稿——绕过「已发布不能删」这道关
        self.post_json(
            f"/api/v1/papers/{self.paper.id}/status/", {"status": PaperStatus.DRAFT},
        )
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.status, PaperStatus.DRAFT)

        response = self._delete()
        self.assertEqual(response.status_code, 400, "被引用时应返回 4xx，不是 500")
        body = response.json()
        self.assertEqual(body["code"], 13008)
        self.assertIn("场考试引用", body["message"])
        self.assertTrue(Paper.objects.filter(pk=self.paper.pk).exists(), "试卷不能被删掉")

    def test_unreferenced_draft_paper_still_deletable(self):
        """没被引用的草稿卷照常可以删——别把正常功能一起拦住。"""
        response = self._delete()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Paper.objects.filter(pk=self.paper.pk).exists())

    def test_archived_exam_still_blocks_deletion(self):
        """考试归档后仍持有外键引用，试卷还是删不掉。

        这一点容易误解成「把考试归档了就能删卷」——实际不行，``PROTECT`` 只看
        有没有行指向它，不看那行的状态。所以提示语才建议「改为归档试卷」。
        """
        self._publish()
        exam = self._make_exam()
        ExamService.change_status(exam, "archived")

        self.post_json(
            f"/api/v1/papers/{self.paper.id}/status/", {"status": PaperStatus.DRAFT},
        )
        response = self._delete()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 13008)

    def test_published_paper_reports_13001(self):
        """已发布仍走 13001，文案要提示「若被引用还需处理考试」。"""
        self._publish()
        response = self._delete()
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], 13001)
        self.assertIn("引用", body["message"])

    def test_service_level_also_guarded(self):
        """直接用 service 删也要拦住（不能只靠视图校验）。"""
        self._publish()
        self._make_exam()
        self.post_json(
            f"/api/v1/papers/{self.paper.id}/status/", {"status": PaperStatus.DRAFT},
        )
        self.paper.refresh_from_db()

        from apps.common.exceptions import BusinessError

        with self.assertRaises(BusinessError) as ctx:
            PaperService.delete(self.paper)
        self.assertEqual(ctx.exception.biz_code, 13008)
