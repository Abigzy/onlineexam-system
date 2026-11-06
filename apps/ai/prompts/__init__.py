"""
提示词模板加载。

为什么提示词不放数据库而放文件？
  * 版本可控 —— 跟代码一起进 git，改动可追溯；
  * 便于审阅 —— 直接看 .txt 就知道模型被要求做什么；
  * 无需迁移 —— 调提示词不用写 migration。

占位符用 ``{{变量名}}``，用字面量替换而不是 ``str.format``，
这样提示词里可以放心写 JSON 示例（花括号不会冲突）。
"""

from functools import lru_cache
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

PROMPT_DIR = Path(__file__).resolve().parent

# 场景名 → 提示词文件名（不含扩展名）
SCENE_PROMPTS = {
    "generate_questions": "generate_questions",
    "explain_question": "explain_question",
    "grade_subjective": "grade_subjective",
    "analyze_weakness": "analyze_weakness",
    "recommend_practice": "recommend_practice",
    "compose_paper": "compose_paper",
    "test_connection": "test_connection",
}


def available_prompts() -> list:
    """列出所有可用的提示词模板。"""
    return sorted(path.stem for path in PROMPT_DIR.glob("*.txt"))


@lru_cache(maxsize=64)
def _read(name: str) -> str:
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise ImproperlyConfigured(f"提示词模板不存在：{path}")
    return path.read_text(encoding="utf-8")


def load_prompt(name: str, **variables) -> str:
    """读取提示词模板，并替换 ``{{key}}`` 占位符。"""
    template = _read(name)
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template


def split_system_user(text: str) -> tuple:
    """把一份模板拆成 (system, user) 两段。

    约定：模板里用一行 ``---USER---`` 分隔。
    """
    if "---USER---" not in text:
        return text.strip(), ""
    system_part, user_part = text.split("---USER---", 1)
    return system_part.strip(), user_part.strip()
