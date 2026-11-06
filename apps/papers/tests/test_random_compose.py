"""随机组卷（按所选知识点，蓝图自动生成，不调 AI）。"""

from apps.common.constants import QuestionType
from apps.papers.composer import PaperComposeService
from apps.papers.models import Paper
from apps.questions.models import KnowledgePoint, Question

from .base import SUBJECT, PaperBaseTest, make_question


class AutoBlueprintTests(PaperBaseTest):
    """auto_blueprint：按题库库存自动分配题型题量。"""

    def test_distributes_across_available_types(self):
        blueprint = PaperComposeService.auto_blueprint(subject=SUBJECT, total_count=20)
        self.assertEqual(sum(row["count"] for row in blueprint), 20)
        types = {row["type"] for row in blueprint}
        self.assertEqual(len(types), 6, "库存覆盖六种题型，蓝图也应六种都有")

    def test_caps_at_total_stock(self):
        # 库存 144 道，请求 1000 题应封顶到库存，而不是报「不足」
        blueprint = PaperComposeService.auto_blueprint(subject=SUBJECT, total_count=1000)
        self.assertEqual(sum(row["count"] for row in blueprint), 144)

    def test_only_available_types(self):
        # 造一个只有单选的知识点，蓝图只应包含单选
        kp_only = KnowledgePoint.objects.create(name="只单选", subject=SUBJECT, code="T-X")
        for _ in range(5):
            make_question(QuestionType.SINGLE, 2, kp=kp_only)

        blueprint = PaperComposeService.auto_blueprint(
            subject=SUBJECT, knowledge_point_ids=[kp_only.id], total_count=20,
        )
        self.assertEqual(len(blueprint), 1, "只有单选有库存，蓝图不应出现别的题型")
        self.assertEqual(blueprint[0]["type"], QuestionType.SINGLE)
        self.assertEqual(blueprint[0]["count"], 5, "库存只有 5 道就只出 5 道")

    def test_empty_pool_returns_empty(self):
        blueprint = PaperComposeService.auto_blueprint(
            subject="不存在的学科", total_count=20,
        )
        self.assertEqual(blueprint, [])


class RandomComposeApiTests(PaperBaseTest):
    """POST /papers/random-compose/ 接口行为。"""

    def test_dry_run_creates_nothing(self):
        before = Paper.objects.count()
        response = self.post_json("/api/v1/papers/random-compose/", {
            "subject": SUBJECT, "total_count": 20, "seed": 42, "dry_run": True,
        })
        self.assertEqual(response.status_code, 200)
        report = response.json()["data"]
        self.assertEqual(report["picked"]["question_count"], 20)
        self.assertTrue(report["feasible"])
        self.assertEqual(Paper.objects.count(), before, "试算不能建卷子")

    def test_creates_paper_without_ai_notes(self):
        response = self.post_json("/api/v1/papers/random-compose/", {
            "subject": SUBJECT, "total_count": 20, "seed": 7,
        })
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(data["paper"]["generation_mode"], "random")
        self.assertEqual(data["paper"]["question_count"], 20)
        # 随机组卷不调 AI，返回里不应有组卷说明
        self.assertNotIn("notes", data)

    def test_caps_at_stock(self):
        # serializer 上限 200，但库存只有 144 → 实际抽 144 道
        response = self.post_json("/api/v1/papers/random-compose/", {
            "subject": SUBJECT, "total_count": 200,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["paper"]["question_count"], 144)

    def test_reproducible_with_seed(self):
        def ids(seed):
            response = self.post_json("/api/v1/papers/random-compose/", {
                "subject": SUBJECT, "total_count": 20, "seed": seed,
            })
            paper = Paper.objects.get(id=response.json()["data"]["paper"]["id"])
            return list(paper.paper_questions.order_by("order").values_list("question_id", flat=True))

        self.assertEqual(ids(99), ids(99), "同一种子应复现同一份卷")
        self.assertNotEqual(ids(99), ids(100), "不同种子应抽出不同题目")

    def test_respects_knowledge_point_scope(self):
        response = self.post_json("/api/v1/papers/random-compose/", {
            "subject": SUBJECT, "knowledge_point_ids": [self.kp_a.id],
            "total_count": 10,
        })
        self.assertEqual(response.status_code, 201)
        paper = Paper.objects.get(id=response.json()["data"]["paper"]["id"])
        for qid in paper.paper_questions.values_list("question_id", flat=True):
            kps = set(
                Question.objects.get(id=qid).knowledge_points.values_list("id", flat=True)
            )
            self.assertIn(self.kp_a.id, kps, "只选 kp_a，抽出的题都应挂在 kp_a")

    def test_empty_scope_errors(self):
        response = self.post_json("/api/v1/papers/random-compose/", {
            "subject": "不存在的学科", "total_count": 10,
        })
        self.assertEqual(response.status_code, 400)
