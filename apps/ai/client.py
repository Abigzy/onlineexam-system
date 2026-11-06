"""
AI 客户端 —— 业务代码唯一需要打交道的入口。

典型用法：

    from apps.ai.client import get_ai_client

    client = get_ai_client()
    result = client.run_scene(
        "generate_questions",
        user=request.user,
        subject="数据结构",
        knowledge_points="二叉树、二叉搜索树",
        question_types="单选题、判断题",
        difficulty_desc="以中等难度为主",
        count=5,
        source_text="……教材段落……",
    )
    payload = result.json(strict=True)      # {'questions': [...]}

好处：
  * 业务代码不出现 requests、不拼 URL、不管重试与超时；
  * 没法解析的返回自动抛 AIResponseFormatError，而不是静默返回空字典；
  * 每次调用都自动记账（AIRequestLog），换 provider 无需改动业务。
"""

import logging

from django.conf import settings

from apps.ai.exceptions import AIServiceError
from apps.ai.prompts import SCENE_PROMPTS, load_prompt, split_system_user
from apps.ai.providers import BaseProvider, ChatResult, build_messages, build_provider

logger = logging.getLogger("apps.ai.client")


class AIClient:
    """对 Provider 的薄封装：加提示词渲染、日志记账、统一异常。"""

    def __init__(self, provider: BaseProvider = None, provider_name: str = None):
        self.provider = provider or build_provider(provider_name)

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def is_mock(self) -> bool:
        return self.provider.name == "mock"

    def info(self) -> dict:
        data = self.provider.info()
        data["is_mock"] = data["provider"] == "mock"
        data["scene_prompts"] = sorted(SCENE_PROMPTS)
        return data

    # ------------------------------------------------------------------
    # 基础调用
    # ------------------------------------------------------------------
    def chat(self, messages, *, scene: str = "generic", user=None,
             temperature: float = 0.7, max_tokens: int = None,
             json_mode: bool = False, log: bool = True) -> ChatResult:
        try:
            result = self.provider.chat(
                messages,
                scene=scene,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        except AIServiceError as exc:
            if log:
                self._log(scene=scene, user=user, messages=messages, error=str(exc))
            raise
        except Exception as exc:  # 兜底：任何意外都包装成统一异常，避免把栈泄漏给用户
            logger.exception("调用大模型出现未预期异常")
            wrapped = AIServiceError("调用大模型失败", provider=self.name, detail=str(exc))
            if log:
                self._log(scene=scene, user=user, messages=messages, error=str(wrapped))
            raise wrapped from exc

        if log:
            self._log(scene=scene, user=user, messages=messages, result=result)
        return result

    def chat_json(self, messages, *, scene: str = "generic", user=None, **kwargs) -> ChatResult:
        """要求模型输出 JSON。调用方再用 ``result.json(strict=True)`` 取结构化结果。"""
        kwargs.pop("json_mode", None)
        return self.chat(messages, scene=scene, user=user, json_mode=True, **kwargs)

    # ------------------------------------------------------------------
    # 场景化调用：一行搞定「渲染提示词 → 调模型 → 记账」
    # ------------------------------------------------------------------
    def build_scene_messages(self, scene: str, **variables) -> list:
        """按场景渲染出 messages。"""
        prompt_name = SCENE_PROMPTS.get(scene, scene)
        template = load_prompt(prompt_name, **variables)
        system_text, user_text = split_system_user(template)
        if user_text:
            return build_messages(system_text, user_text)
        return build_messages("", system_text)

    def run_scene(self, scene: str, *, user=None, json_mode: bool = None,
                  temperature: float = None, max_tokens: int = None, **variables) -> ChatResult:
        """渲染提示词并调用模型。

        ``json_mode`` 不传时，自动按「该场景是否需要 JSON」决定
        （generate_questions / grade_subjective / analyze_weakness / recommend_practice 需要）。
        """
        messages = self.build_scene_messages(scene, **variables)

        if json_mode is None:
            json_mode = scene in JSON_SCENES
        if temperature is None:
            temperature = 0.2 if json_mode else 0.7

        return self.chat(
            messages,
            scene=scene,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )

    def check_connection(self) -> ChatResult:
        """测试连通性——给「测试连接」接口用。"""
        return self.run_scene("test_connection", json_mode=False, max_tokens=32, temperature=0.0)

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    @staticmethod
    def _preview(text: str, limit: int = 500) -> str:
        text = (text or "").replace("\n", " ").strip()
        return text[:limit]

    def _log(self, *, scene, user, messages, result: ChatResult = None, error: str = ""):
        """写调用流水。记账失败绝不能影响主流程，所以整段包了 try。"""
        try:
            from apps.ai.models import AIRequestLog

            request_preview = " | ".join(
                self._preview(str(m.get("content", ""))[:200], 200)
                for m in messages if isinstance(m, dict)
            )[:500]

            AIRequestLog.objects.create(
                provider=self.name,
                model=self.provider.model,
                scene=scene,
                user=user if getattr(user, "is_authenticated", False) else None,
                success=bool(result),
                is_mock=bool(result and result.is_mock),
                prompt_tokens=getattr(result, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(result, "completion_tokens", 0) or 0,
                total_tokens=getattr(result, "total_tokens", 0) or 0,
                latency_ms=getattr(result, "latency_ms", 0) or 0,
                error=(error or "")[:2000],
                request_preview=request_preview,
                response_preview=self._preview(getattr(result, "content", ""), 500),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 AI 调用记录失败（不影响主流程）：%s", exc)


# 需要 JSON 输出的场景
JSON_SCENES = {
    "generate_questions",
    "grade_subjective",
    "analyze_weakness",
    "recommend_practice",
}

_client_cache: dict = {}


def get_ai_client(provider_name: str = None) -> AIClient:
    """获取 AI 客户端。

    按 provider 名缓存实例（requests 自身有连接池，复用实例可省去重复构造）。
    """
    key = (provider_name or settings.AI_PROVIDER or "").lower() or "__default__"
    if key not in _client_cache:
        _client_cache[key] = AIClient(provider_name=provider_name)
    return _client_cache[key]


def reset_ai_client() -> None:
    """清空缓存（测试里切换 provider / 改配置后调用）。"""
    _client_cache.clear()
