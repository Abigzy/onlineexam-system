"""
AI 适配层测试。

全部离线运行：真实 provider 用 Mock 替代，不产生任何外网请求。
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.ai import mocks
from apps.ai.client import AIClient, get_ai_client, reset_ai_client
from apps.ai.exceptions import AIResponseFormatError, AIServiceError
from apps.ai.models import AIRequestLog
from apps.ai.providers import build_provider, catalog
from apps.ai.providers.base import ChatResult, build_messages
from apps.ai.providers.mock import MockProvider
from apps.ai.providers.openai_compatible import OpenAICompatibleProvider
from apps.ai.schemas import (
    parse_generated_questions,
    parse_grading_result,
    score_by_threshold,
)
from apps.common.constants import Role

User = get_user_model()
PASSWORD = "Exam@123456"


# ==================================================================
# Provider 选择
# ==================================================================
class ProviderBuildTest(TestCase):
    """没配 Key 时要能优雅降级，不能直接报错。"""

    @override_settings(AI_PROVIDER="deepseek", AI_ALLOW_MOCK=True,
                       AI_PROVIDERS={"deepseek": {"api_key": "", "base_url": "https://api.deepseek.com/v1",
                                                  "model": "deepseek-chat"}})
    def test_fallback_to_mock_without_key(self):
        provider = build_provider()
        self.assertEqual(provider.name, "mock")
        self.assertTrue(provider.is_configured)

    @override_settings(AI_PROVIDER="deepseek", AI_ALLOW_MOCK=False,
                       AI_PROVIDERS={"deepseek": {"api_key": "", "base_url": "https://api.deepseek.com/v1",
                                                  "model": "deepseek-chat"}})
    def test_raise_when_mock_disabled(self):
        with self.assertRaises(AIServiceError):
            build_provider()

    @override_settings(AI_PROVIDER="deepseek", AI_ALLOW_MOCK=True,
                       AI_PROVIDERS={"deepseek": {"api_key": "sk-test", "base_url": "https://api.deepseek.com/v1",
                                                  "model": "deepseek-chat"}})
    def test_use_real_provider_when_key_present(self):
        provider = build_provider()
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertEqual(provider.name, "deepseek")
        self.assertEqual(provider.endpoint, "https://api.deepseek.com/v1/chat/completions")

    @override_settings(AI_PROVIDER="deepseek", AI_ALLOW_MOCK=True, AI_PROVIDERS={})
    def test_unknown_provider_raises(self):
        with self.assertRaises(AIServiceError):
            build_provider("no_such_platform")

    def test_catalog_hides_api_key(self):
        for item in catalog():
            self.assertNotIn("api_key", item)
            self.assertIn("configured", item)


class ProviderInfoTest(TestCase):
    """info() 绝不能泄漏 api_key。"""

    @override_settings(AI_PROVIDER="deepseek", AI_ALLOW_MOCK=True,
                       AI_PROVIDERS={"deepseek": {"api_key": "sk-secret-value",
                                                  "base_url": "https://api.deepseek.com/v1",
                                                  "model": "deepseek-chat"}})
    def test_info_never_leaks_key(self):
        info = build_provider().info()
        self.assertNotIn("sk-secret-value", json.dumps(info))
        self.assertNotIn("api_key", info)


# ==================================================================
# Mock Provider
# ==================================================================
class MockProviderTest(TestCase):
    def setUp(self):
        self.provider = MockProvider()

    def test_chat_generic(self):
        result = self.provider.chat(build_messages("sys", "你好"), scene="generic")
        self.assertTrue(result.is_mock)
        self.assertIn("本地模拟", result.content)
        self.assertGreater(result.total_tokens, 0)

    def test_generate_questions_scene(self):
        prompt = "知识点列表：二叉树、图的遍历\n题型要求：单选题、判断题\n本题需要生成 4 道题。"
        result = self.provider.chat(build_messages("", prompt), scene="generate_questions", json_mode=True)
        payload = result.json(strict=True)
        self.assertEqual(len(payload["questions"]), 4)
        self.assertEqual(payload["questions"][0]["type"], "single")
        self.assertTrue(any(o["is_correct"] for o in payload["questions"][0]["options"]))

    def test_grade_subjective_scene(self):
        prompt = (
            "【参考答案】二叉树的遍历分为前序、中序、后序三种，可用递归实现。\n\n"
            "【学生答案】二叉树遍历有前序中序后序，都能用递归实现。"
        )
        result = self.provider.chat(build_messages("", prompt), scene="grade_subjective", json_mode=True)
        payload = result.json(strict=True)
        self.assertIn("score_ratio", payload)
        self.assertGreater(payload["score_ratio"], 0.3)

    def test_unknown_scene_falls_back_to_generic(self):
        result = self.provider.chat([{"role": "user", "content": "hi"}], scene="not_exist")
        self.assertIn("本地模拟", result.content)


# ==================================================================
# JSON 解析
# ==================================================================
class ChatResultJsonTest(TestCase):
    def _result(self, content):
        return ChatResult(content=content)

    def test_plain_json(self):
        self.assertEqual(self._result('{"a": 1}').json(), {"a": 1})

    def test_json_in_code_fence(self):
        content = '```json\n{"a": 1}\n```'
        self.assertEqual(self._result(content).json(), {"a": 1})

    def test_json_with_surrounding_text(self):
        content = '好的，结果如下：\n{"a": {"b": [1, 2]}}\n以上。'
        self.assertEqual(self._result(content).json(), {"a": {"b": [1, 2]}})

    def test_top_level_array_wrapped(self):
        self.assertEqual(self._result("[1, 2, 3]").json(), {"items": [1, 2, 3]})

    def test_invalid_json_non_strict_returns_empty(self):
        self.assertEqual(self._result("这不是 JSON").json(), {})

    def test_invalid_json_strict_raises(self):
        with self.assertRaises(AIResponseFormatError):
            self._result("这不是 JSON").json(strict=True)

    def test_empty_content_strict_raises(self):
        with self.assertRaises(AIResponseFormatError):
            self._result("").json(strict=True)


# ==================================================================
# 结构化输出解析
# ==================================================================
class ParseGeneratedQuestionsTest(TestCase):
    def test_normal_payload(self):
        payload = {"questions": [{
            "type": "single", "stem": "题干", "difficulty": 2, "score": 3,
            "options": [{"label": "A", "content": "对", "is_correct": True},
                        {"label": "B", "content": "错", "is_correct": False}],
            "reference_answer": "A", "analysis": "解析", "knowledge_points": ["知识点一"],
        }]}
        items = parse_generated_questions(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].type, "single")
        self.assertEqual(items[0].reference_answer, "A")
        self.assertEqual(items[0].knowledge_points, ["知识点一"])

    def test_missing_questions_key_raises(self):
        with self.assertRaises(AIResponseFormatError):
            parse_generated_questions({"data": []})

    def test_question_without_stem_is_dropped(self):
        payload = {"questions": [{"type": "single", "stem": ""}, {"type": "short", "stem": "有效题干"}]}
        items = parse_generated_questions(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].stem, "有效题干")

    def test_difficulty_clamped(self):
        payload = {"questions": [{"stem": "题", "type": "short", "difficulty": 99}]}
        self.assertEqual(parse_generated_questions(payload)[0].difficulty, 4)

    def test_difficulty_from_chinese_text(self):
        payload = {"questions": [{"stem": "题", "type": "short", "difficulty": "困难"}]}
        self.assertEqual(parse_generated_questions(payload)[0].difficulty, 3)

    def test_single_choice_with_multiple_correct_keeps_first(self):
        payload = {"questions": [{
            "stem": "题", "type": "single",
            "options": [{"label": "A", "content": "1", "is_correct": True},
                        {"label": "B", "content": "2", "is_correct": True}],
        }]}
        item = parse_generated_questions(payload)[0]
        self.assertEqual(sum(1 for o in item.options if o.is_correct), 1)
        self.assertTrue(item.warnings)

    def test_answer_backfilled_from_reference(self):
        payload = {"questions": [{
            "stem": "题", "type": "single", "reference_answer": "B",
            "options": [{"label": "A", "content": "1"}, {"label": "B", "content": "2"}],
        }]}
        item = parse_generated_questions(payload)[0]
        self.assertTrue(next(o for o in item.options if o.label == "B").is_correct)

    def test_options_as_dict(self):
        payload = {"questions": [{
            "stem": "题", "type": "multiple",
            "options": {"A": "甲", "B": "乙"}, "reference_answer": "AB",
        }]}
        item = parse_generated_questions(payload)[0]
        self.assertEqual(len(item.options), 2)
        self.assertEqual(sum(1 for o in item.options if o.is_correct), 2)

    def test_type_alias_normalized(self):
        payload = {"questions": [{"stem": "题", "type": "单选题"}]}
        self.assertEqual(parse_generated_questions(payload)[0].type, "single")

    def test_non_choice_types_get_empty_options(self):
        payload = {"questions": [{"stem": "题", "type": "essay",
                                  "options": [{"label": "A", "content": "x"}]}]}
        self.assertEqual(parse_generated_questions(payload)[0].options, [])


class ParseGradingResultTest(TestCase):
    def test_normal(self):
        result = parse_grading_result({"score_ratio": 0.8, "level": "good", "reason": "不错",
                                       "hit_points": ["要点1"], "missing_points": ["要点2"]})
        self.assertAlmostEqual(result.score_ratio, 0.8)
        self.assertEqual(result.score_percent, 80)

    def test_percent_style_ratio(self):
        result = parse_grading_result({"score_ratio": 75})
        self.assertAlmostEqual(result.score_ratio, 0.75)
        self.assertTrue(result.warnings)

    def test_score_out_of_100(self):
        result = parse_grading_result({"score": 60})
        self.assertAlmostEqual(result.score_ratio, 0.6)

    def test_ratio_out_of_range_clamped(self):
        self.assertEqual(parse_grading_result({"score_ratio": 3.5}).score_ratio, 1.0)
        self.assertEqual(parse_grading_result({"score_ratio": -1}).score_ratio, 0.0)

    def test_missing_score_raises(self):
        with self.assertRaises(AIResponseFormatError):
            parse_grading_result({"reason": "无分数"})

    def test_level_derived_when_absent(self):
        self.assertEqual(parse_grading_result({"score_ratio": 0.9}).level, "excellent")
        self.assertEqual(parse_grading_result({"score_ratio": 0.1}).level, "poor")


class ScoreThresholdTest(TestCase):
    """对应需求文档的 0.7 / 0.4 阈值策略。"""

    def test_above_full_threshold(self):
        self.assertEqual(score_by_threshold(0.9, full_score=10), 10.0)

    def test_below_partial_threshold(self):
        self.assertEqual(score_by_threshold(0.2, full_score=10), 0.0)

    def test_partial_linear(self):
        self.assertEqual(score_by_threshold(0.4, full_score=10), 0.0)
        self.assertEqual(score_by_threshold(0.7, full_score=10), 10.0)
        self.assertAlmostEqual(score_by_threshold(0.55, full_score=10), 5.0)


# ==================================================================
# 客户端与记账
# ==================================================================
class AIClientTest(TestCase):
    def setUp(self):
        reset_ai_client()
        self.user = User.objects.create_user(
            username="ai_teacher", email="ai_t@example.com", password=PASSWORD, role=Role.TEACHER
        )
        self.client_ai = AIClient(provider=MockProvider())

    def tearDown(self):
        reset_ai_client()

    def test_chat_writes_log(self):
        self.client_ai.chat(build_messages("sys", "你好"), scene="generic", user=self.user)
        log = AIRequestLog.objects.get()
        self.assertEqual(log.scene, "generic")
        self.assertEqual(log.provider, "mock")
        self.assertTrue(log.success)
        self.assertTrue(log.is_mock)
        self.assertEqual(log.user, self.user)
        self.assertGreater(log.total_tokens, 0)

    def test_failure_is_logged(self):
        class BrokenProvider(MockProvider):
            name = "broken"

            def chat(self, *args, **kwargs):
                raise AIServiceError("模拟失败", provider="broken")

        with self.assertRaises(AIServiceError):
            AIClient(provider=BrokenProvider()).chat([{"role": "user", "content": "x"}], scene="generic")

        log = AIRequestLog.objects.get()
        self.assertFalse(log.success)
        self.assertIn("模拟失败", log.error)

    def test_run_scene_renders_prompt(self):
        result = self.client_ai.run_scene(
            "generate_questions",
            subject="数据结构",
            knowledge_points="二叉树、图",
            question_types="单选题",
            difficulty_desc="中等",
            count=3,
            source_text="教材片段",
        )
        payload = result.json(strict=True)
        self.assertEqual(len(payload["questions"]), 3)
        self.assertEqual(payload["questions"][0]["knowledge_points"], ["二叉树"])

    def test_scene_prompt_placeholder_replaced(self):
        messages = self.client_ai.build_scene_messages("grade_subjective", stem="题干X",
                                                       full_score=10, reference_answer="答案Y",
                                                       student_answer="学生Z")
        joined = json.dumps(messages, ensure_ascii=False)
        self.assertIn("题干X", joined)
        self.assertIn("学生Z", joined)
        self.assertNotIn("{{", joined)

    def test_get_ai_client_cached(self):
        reset_ai_client()
        first = get_ai_client()
        second = get_ai_client()
        self.assertIs(first, second)


# ==================================================================
# 接口
# ==================================================================
class AIApiTest(TestCase):
    def setUp(self):
        reset_ai_client()
        self.teacher = User.objects.create_user(
            username="ai_tea", email="ai_tea@example.com", password=PASSWORD, role=Role.TEACHER
        )
        self.student = User.objects.create_user(
            username="ai_stu", email="ai_stu@example.com", password=PASSWORD, role=Role.STUDENT
        )

    def tearDown(self):
        reset_ai_client()

    def _auth(self, user):
        token = self.client.post(
            "/api/v1/auth/login/", {"username": user.username, "password": PASSWORD},
            content_type="application/json",
        ).json()["data"]["access"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_chat_requires_teacher(self):
        resp = self.client.post("/api/v1/ai/chat/", {"prompt": "你好"},
                                content_type="application/json", **self._auth(self.student))
        self.assertEqual(resp.status_code, 403)

    def test_chat_with_prompt(self):
        resp = self.client.post("/api/v1/ai/chat/", {"prompt": "你好", "scene": "generic"},
                                content_type="application/json", **self._auth(self.teacher))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertTrue(body["is_mock"])
        self.assertIn("本地模拟", body["content"])

    def test_chat_with_scene_variables(self):
        resp = self.client.post(
            "/api/v1/ai/chat/",
            {"scene": "generate_questions", "json_mode": True,
             "variables": {"subject": "操作系统", "knowledge_points": "进程调度",
                           "question_types": "判断题", "difficulty_desc": "中等",
                           "count": 2, "source_text": "素材"}},
            content_type="application/json", **self._auth(self.teacher),
        )
        self.assertEqual(resp.status_code, 200)
        parsed = resp.json()["data"]["parsed"]
        self.assertEqual(len(parsed["questions"]), 2)

    def test_providers_endpoint(self):
        resp = self.client.get("/api/v1/ai/providers/", **self._auth(self.teacher))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        keys = [item["key"] for item in data["providers"]]
        self.assertIn("deepseek", keys)
        self.assertIn("doubao", keys)
        self.assertIn("yuanbao", keys)
        self.assertNotIn("api_key", json.dumps(data))

    def test_usage_endpoint(self):
        self.client.post("/api/v1/ai/chat/", {"prompt": "统计一次"},
                         content_type="application/json", **self._auth(self.teacher))
        resp = self.client.get("/api/v1/ai/usage/", **self._auth(self.teacher))
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["data"]["totals"]["calls"], 1)

    def test_test_connection_endpoint(self):
        resp = self.client.post("/api/v1/ai/test-connection/", {},
                                content_type="application/json", **self._auth(self.teacher))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["ok"])

    def test_logs_endpoint(self):
        self.client.post("/api/v1/ai/chat/", {"prompt": "写条日志"},
                         content_type="application/json", **self._auth(self.teacher))
        resp = self.client.get("/api/v1/ai/logs/", **self._auth(self.teacher))
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["data"]["count"], 1)


class MockHelpersTest(TestCase):
    """mock 的参数提取逻辑（后续模块的测试会依赖它）。"""

    def test_detect_count(self):
        self.assertEqual(mocks.detect_count("请生成 7 道题"), 7)
        self.assertEqual(mocks.detect_count("题量：12"), 12)
        self.assertEqual(mocks.detect_count("没有数字"), 5)

    def test_detect_types(self):
        self.assertEqual(mocks.detect_types("单选题和判断题"), ["single", "judge"])
        self.assertEqual(mocks.detect_types("随便出点"), ["single"])

    def test_detect_knowledge_points(self):
        points = mocks.detect_knowledge_points("知识点列表：二叉树、图的遍历、最短路径")
        self.assertEqual(points, ["二叉树", "图的遍历", "最短路径"])

    def test_similarity_bounds(self):
        self.assertEqual(mocks._similarity("", "abc"), 0.0)
        self.assertEqual(mocks._similarity("完全相同", "完全相同"), 1.0)
