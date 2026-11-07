"""R1 测试轮次发现的缺陷回归（BUG-01 / BUG-02）。

缺陷来源：配套测试项目《功能测试用例》的「8-缺陷与观察项」工作表。
每条用例都注明对应的 BUG 编号，方便与测试台账对账。
"""

from apps.common.constants import QuestionType
from apps.common.exceptions import BusinessError
from apps.papers.constants import PaperStatus
from apps.papers.services import PaperService

from .base import PaperBaseTest


class BatchScoreUpperLimitTest(PaperBaseTest):
    """BUG-01：批量调分不能绕过单题 100 分上限。

    回归前：``batch_set_score`` 只挡 ``<=0``，101 分能直接落库，总分被撑到 107。
    根因是两个入口各写一遍校验，批量那侧漏了上限。
    """

    def setUp(self):
        super().setUp()
        self.paper = self.make_paper()
        PaperService.add_questions(
            paper=self.paper,
            questions=self.questions_of(QuestionType.SINGLE, 2, limit=3),
        )
        self.item = self.paper.paper_questions.first()

    def test_service_rejects_over_100(self):
        with self.assertRaises(BusinessError) as ctx:
            PaperService.batch_set_score(
                paper=self.paper, items=[{"id": self.item.id, "score": 101}],
            )
        self.assertEqual(ctx.exception.biz_code, 13004)
        self.item.refresh_from_db()
        self.assertEqual(float(self.item.score), 2.0, "被拒后分值不应落库")

    def test_api_rejects_over_100(self):
        response = self.post_json(
            f"/api/v1/papers/{self.paper.id}/scores/",
            {"items": [{"id": self.item.id, "score": 101}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 13004)
        self.assertIn("100", response.json()["message"])
        self.item.refresh_from_db()
        self.assertEqual(float(self.item.score), 2.0)

    def test_total_score_not_inflated(self):
        """缺陷描述里的现象：总分由 7 变 107，不能再复现。"""
        PaperService.add_questions(
            paper=self.paper,
            questions=self.questions_of(QuestionType.SINGLE, 2, limit=3),
            score=1,
        )
        before = float(self.paper.total_score_float)
        with self.assertRaises(BusinessError):
            PaperService.batch_set_score(
                paper=self.paper, items=[{"id": self.item.id, "score": 101}],
            )
        self.paper.refresh_from_db()
        self.assertEqual(float(self.paper.total_score_float), before)

    def test_boundary_100_allowed(self):
        """边界值 100 必须放行——修上限不能顺手把合法值也挡了。"""
        PaperService.batch_set_score(
            paper=self.paper, items=[{"id": self.item.id, "score": 100}],
        )
        self.item.refresh_from_db()
        self.assertEqual(float(self.item.score), 100.0)

    def test_two_entrances_agree(self):
        """两个入口对同一个非法值的反应必须一致。"""
        with self.assertRaises(BusinessError):
            PaperService.set_score(paper_question=self.item, score=101)
        with self.assertRaises(BusinessError):
            PaperService.batch_set_score(
                paper=self.paper, items=[{"id": self.item.id, "score": 101}],
            )


class PublishedPaperHeadTest(PaperBaseTest):
    """BUG-02：已发布试卷的卷头信息仍可改（含及格分与时长）。

    回归前：加题 / 调分 / 排序都会走 ``ensure_editable``，唯独改卷头没有，
    导致已发布、正被考试引用的卷子还能改及格分。
    """

    def setUp(self):
        super().setUp()
        self.paper = self.make_paper()
        PaperService.add_questions(
            paper=self.paper,
            questions=self.questions_of(QuestionType.SINGLE, 2, limit=2),
        )
        self.paper.status = PaperStatus.PUBLISHED
        self.paper.save(update_fields=["status"])

    def test_service_rejects_when_published(self):
        with self.assertRaises(BusinessError) as ctx:
            PaperService.update(self.paper, data={"title": "改名", "pass_score": 5})
        self.assertEqual(ctx.exception.biz_code, 13002)
        self.paper.refresh_from_db()
        self.assertNotEqual(self.paper.title, "改名", "被拒后标题不应变化")
        self.assertNotEqual(float(self.paper.pass_score), 5.0)

    def test_api_patch_rejected_when_published(self):
        response = self.client.patch(
            f"/api/v1/papers/{self.paper.id}/",
            {"title": "改名", "pass_score": 5},
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 13002)
        self.assertIn("已发布", response.json()["message"])

    def test_error_message_matches_other_entrances(self):
        """提示语与加题 / 调分保持同一句，避免前端要判两种文案。"""
        with self.assertRaises(BusinessError) as ctx:
            PaperService.update(self.paper, data={"title": "改名"})
        self.assertIn("只有草稿可以修改", str(ctx.exception.detail))

    def test_draft_still_updatable(self):
        """草稿不受影响——别把正常编辑也锁死了。"""
        draft = self.make_paper(title="草稿卷")
        updated = PaperService.update(draft, data={"title": "改过的草稿", "pass_score": 30})
        self.assertEqual(updated.title, "改过的草稿")
        self.assertEqual(float(updated.pass_score), 30.0)
