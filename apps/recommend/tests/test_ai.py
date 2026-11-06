"""AI 推荐理由（``RecommendationService.explain``）的测试。

三条纪律逐条验：

1. **选题是代码干的**：AI 只改写「推荐理由」，「推哪几道题」不因为模型而改变；
2. **失败必须降级**：模型不可用、平台没配、抛异常——页面都得照常打开，
   只是理由回到本地话术，并且 ``degraded=True`` 如实告知；
3. **不重复计费**：同一份推荐集在有效期内复用缓存（AI 是花钱的动作）。
"""

from unittest.mock import patch

from apps.ai.exceptions import AIServiceError
from apps.recommend.services import RecommendationService

from .base import RecommendBaseTest

TODAY = "2026-09-30"


class ExplainBaseTest(RecommendBaseTest):
    """先备一份真实的推荐清单，再让 AI 改写它的理由。"""

    def setUp(self):
        super().setUp()
        self.make_weak()
        self.payload = RecommendationService.daily_practice(self.student, size=3, today=TODAY)
        self.items = self.payload["items"]
        self.assertTrue(self.items)
        self.local_reasons = {item["question_id"]: item["reason"] for item in self.items}


class ExplainNoopTest(ExplainBaseTest):
    def test_empty_items_never_call_the_model(self):
        with patch("apps.recommend.services.get_ai_client") as client_factory:
            result = RecommendationService.explain(self.student, [], provider="mock")
        client_factory.assert_not_called()
        self.assertEqual(result["items"], [])
        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "local")


class ExplainSuccessTest(ExplainBaseTest):
    def test_mock_provider_rewrites_reasons(self):
        result = RecommendationService.explain(
            self.student, self.items, provider="mock", user=self.student,
        )
        self.assertFalse(result["degraded"])
        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["provider"], "mock")
        self.assertTrue(result["is_mock"])
        self.assertFalse(result["cached"])
        self.assertTrue(result["reason"])

        # 选题没变：题目与顺序完全一致，只有理由被改写
        self.assertEqual([item["question_id"] for item in result["items"]],
                         [item["question_id"] for item in self.items])
        for item in result["items"]:
            self.assertEqual(item["source"], "ai")
            self.assertIn("薄弱环节", item["reason"])
            self.assertNotEqual(item["reason"], self.local_reasons[item["question_id"]])

    def test_second_call_hits_cache(self):
        first = RecommendationService.explain(self.student, self.items, provider="mock")
        second = RecommendationService.explain(self.student, self.items, provider="mock")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual([item["reason"] for item in first["items"]],
                         [item["reason"] for item in second["items"]])


class ExplainDegradeTest(ExplainBaseTest):
    def test_unknown_provider_degrades_and_keeps_local_reasons(self):
        result = RecommendationService.explain(
            self.student, self.items, provider="不存在的平台",
        )
        self.assertTrue(result["degraded"])
        self.assertIn("AI 调用失败", result["degraded_reason"])
        self.assertFalse(result["cached"])
        for item in result["items"]:
            self.assertEqual(item["source"], "local")
            self.assertEqual(item["reason"], self.local_reasons[item["question_id"]])

    def test_unexpected_exception_still_degrades(self):
        with patch("apps.recommend.services.get_ai_client",
                   side_effect=RuntimeError("boom")):
            result = RecommendationService.explain(self.student, self.items, provider="mock")
        self.assertTrue(result["degraded"])
        self.assertIn("AI 调用异常", result["degraded_reason"])
        self.assertTrue(all(item["source"] == "local" for item in result["items"]))

    def test_unparsable_response_degrades(self):
        class FakeResult:
            is_mock = False

            def json(self, strict=True):
                return {}          # 模型没给出可用 JSON

        class FakeClient:
            name = "fake"
            is_mock = False

            class provider:                    # noqa: N801 —— 只是承载 model 属性
                model = "fake-model"

            def run_scene(self, scene, **kwargs):
                return FakeResult()

        with patch("apps.recommend.services.get_ai_client", return_value=FakeClient()):
            result = RecommendationService.explain(self.student, self.items, provider="mock")
        self.assertTrue(result["degraded"])
        self.assertIn("AI 调用失败", result["degraded_reason"])

    def test_degrade_is_not_cached(self):
        # 降级结果不进缓存，避免「模型恢复了却一直返回旧降级结论」
        RecommendationService.explain(self.student, self.items, provider="不存在的平台")
        retry = RecommendationService.explain(self.student, self.items, provider="mock")
        self.assertFalse(retry["cached"])
        self.assertFalse(retry["degraded"])

    def test_ai_service_error_is_the_contract(self):
        # 降级只捕获 AIServiceError / Exception，不该把异常抛给调用方
        with patch("apps.recommend.services.get_ai_client",
                   side_effect=AIServiceError("boom", provider="mock")):
            result = RecommendationService.explain(self.student, self.items)
        self.assertTrue(result["degraded"])
