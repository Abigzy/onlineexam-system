"""Provider 抽象层。

所有大模型平台（DeepSeek / 豆包 / 腾讯元宝 / OpenAI / 自建网关）都实现同一个接口，
上层业务只认 `BaseProvider`，换平台不用改一行业务代码。
"""

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional

from apps.ai.exceptions import AIResponseFormatError


# ==================================================================
# 消息
# ==================================================================
def system(content: str) -> dict:
    """构造 system 消息。"""
    return {"role": "system", "content": content}


def user(content: str) -> dict:
    """构造 user 消息。"""
    return {"role": "user", "content": content}


def assistant(content: str) -> dict:
    """构造 assistant 消息（多轮对话时把历史回答塞回去）。"""
    return {"role": "assistant", "content": content}


def build_messages(system_prompt: str = "", user_prompt: str = "") -> list:
    """最常见的两段式消息。"""
    messages = []
    if system_prompt:
        messages.append(system(system_prompt))
    if user_prompt:
        messages.append(user(user_prompt))
    return messages


def normalize_messages(messages: Iterable) -> list:
    """把 str / dict 混搭的输入统一成 OpenAI 消息格式。"""
    normalized = []
    for item in messages:
        if isinstance(item, str):
            normalized.append(user(item))
        elif isinstance(item, dict) and "role" in item:
            normalized.append({"role": item["role"], "content": str(item.get("content", ""))})
        else:
            raise ValueError(f"无法识别的消息格式：{item!r}")
    return normalized


# ==================================================================
# 调用结果
# ==================================================================
@dataclass
class ChatResult:
    """一次模型调用的结果。"""

    content: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)
    is_mock: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("raw", None)  # 原始响应体体积大，不往外传
        return data

    # ---------------- 结构化输出 ----------------
    def json(self, *, strict: bool = False) -> dict:
        """把 content 解析成 JSON 对象。

        大模型经常在 JSON 外面套 ```json 代码块或加一段解释，
        这里做「尽力而为」的清洗；``strict=True`` 时解析失败直接抛异常。
        """
        text = (self.content or "").strip()
        if not text:
            raise AIResponseFormatError("模型返回内容为空", raw=text, provider=self.provider)

        candidate = _strip_code_fence(text)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            extracted = _extract_json_block(candidate)
            if extracted is None:
                if strict:
                    raise AIResponseFormatError("模型返回内容不是合法 JSON", raw=text, provider=self.provider)
                return {}
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError as exc:
                if strict:
                    raise AIResponseFormatError(f"模型返回内容不是合法 JSON：{exc}", raw=text, provider=self.provider)
                return {}

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        if strict:
            raise AIResponseFormatError("模型返回的 JSON 顶层不是对象", raw=text, provider=self.provider)
        return {}


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹。"""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]  # 丢掉起始的 ```json
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_block(text: str) -> Optional[str]:
    """从混杂文本里截取第一个完整的 JSON 对象/数组。"""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
    return None


# ==================================================================
# Provider 基类
# ==================================================================
class BaseProvider(ABC):
    """大模型 provider 统一接口。"""

    name = "base"
    display_name = "基础 Provider"

    def __init__(self, *, api_key: str = "", base_url: str = "", model: str = "",
                 timeout: int = 60, max_retries: int = 2):
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = timeout
        self.max_retries = max_retries

    # ---------------- 状态 ----------------
    @property
    def is_configured(self) -> bool:
        """是否具备真实调用条件（缺 Key 的 provider 视为未配置）。"""
        return bool(self.api_key and self.base_url and self.model)

    def info(self) -> dict:
        """对外暴露的配置信息——**绝不返回 api_key**。"""
        return {
            "provider": self.name,
            "display_name": self.display_name,
            "model": self.model,
            "base_url": self.base_url,
            "configured": self.is_configured,
        }

    # ---------------- 核心接口 ----------------
    @abstractmethod
    def chat(
        self,
        messages,
        *,
        scene: str = "generic",
        temperature: float = 0.7,
        max_tokens: int = None,
        json_mode: bool = False,
    ) -> ChatResult:
        """发起一次对话补全。子类必须实现。

        参数
        ----
        messages     : list[dict]，OpenAI 消息格式；也接受 list[str]
        scene        : 业务场景标识（如 generate_questions），用于记账与 mock 分派
        json_mode    : 要求模型输出 JSON（OpenAI 兼容端点的 response_format）
        """
        raise NotImplementedError

    # ---------------- 便捷封装 ----------------
    def chat_json(self, messages, *, scene: str = "generic", **kwargs) -> ChatResult:
        """要求返回 JSON 的一次调用。"""
        return self.chat(messages, scene=scene, json_mode=True, **kwargs)

    def check_connection(self) -> ChatResult:
        """连通性自检——给「测试连接」按钮用。"""
        return self.chat(
            build_messages("你是一个连通性测试助手。", "请只回复两个字：正常"),
            scene="test_connection",
            temperature=0.0,
            max_tokens=16,
        )

    # ---------------- 计时工具 ----------------
    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)
