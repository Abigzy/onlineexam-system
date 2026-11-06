"""AI 组卷说明。

分工很明确：
  * **组卷算法**（``composer.py``）负责「选哪些题」——这是可验证的约束求解，交给代码；
  * **大模型**只负责「把为什么这么选讲清楚」——这是自然语言表达，交给模型。

不让模型参与选题，是因为它会编出不存在的题、算不清总分、也不会守规则。
让它做解释性工作则很合适：同一份数据每次都能写出通顺的说明。

生成的说明是 **Markdown 纯文本**，直接存进 ``Paper.ai_notes``，前端做轻量渲染。
"""

import logging
from decimal import Decimal

from apps.ai.client import get_ai_client
from apps.ai.exceptions import AIServiceError
from apps.common.constants import Difficulty
from apps.common.exceptions import BusinessError

from .services import PaperAnalysisService

logger = logging.getLogger("apps.papers.ai")


def _fmt(value) -> str:
    number = float(value or 0)
    return str(int(number)) if number == int(number) else f"{number:g}"


class PaperAIService:
    """为一份试卷生成「组卷说明 / 选题理由」。"""

    @staticmethod
    def generate_notes(paper, *, user=None, provider: str = None, force: bool = False) -> dict:
        """生成并保存组卷说明。

        ``force=False`` 且已有说明时直接返回缓存——教师打开一次页面就重写一遍
        既费钱又会让同一份卷子的说明来回变，没有意义。
        """
        if paper.ai_notes and not force:
            return {
                "notes": paper.ai_notes,
                "provider": paper.ai_provider,
                "model": paper.ai_model,
                "cached": True,
            }

        report = PaperAnalysisService.full_report(paper)
        overview = report["overview"]

        if overview["question_count"] == 0:
            raise BusinessError("空卷无法生成组卷说明，请先添加题目", code=13020)

        variables = {
            "paper_title": paper.title,
            "subject": paper.subject or "（未指定）",
            "overview": PaperAIService._overview_text(overview),
            "type_summary": PaperAIService._type_text(report["type_distribution"]),
            "difficulty_summary": PaperAIService._difficulty_text(report["difficulty_distribution"]),
            "knowledge_summary": PaperAIService._knowledge_text(report["knowledge_coverage"]),
            "warning_summary": PaperAIService._warning_text(report["warnings"]),
        }

        client = get_ai_client(provider)
        try:
            result = client.run_scene(
                "compose_paper", user=user, temperature=0.5, max_tokens=1200, **variables,
            )
        except AIServiceError as exc:
            raise BusinessError(f"生成组卷说明失败：{exc}", code=13020, status_code=502) from exc

        notes = (result.content or "").strip()
        if not notes:
            raise BusinessError("AI 返回了空的组卷说明，请稍后重试", code=13020, status_code=502)

        paper.ai_notes = notes
        paper.ai_provider = client.name
        paper.ai_model = client.provider.model
        paper.save(update_fields=["ai_notes", "ai_provider", "ai_model", "updated_at"])

        logger.info("试卷《%s》生成组卷说明，%s 字", paper.title, len(notes))
        return {
            "notes": notes,
            "provider": client.name,
            "model": client.provider.model,
            "is_mock": client.is_mock,
            "total_tokens": result.total_tokens,
            "latency_ms": result.latency_ms,
            "cached": False,
        }

    # ------------------------------------------------------------------
    # 数据 → 提示词文本
    # ------------------------------------------------------------------
    @staticmethod
    def _overview_text(overview: dict) -> str:
        return (
            f"共 {overview['question_count']} 题，总分 {_fmt(overview['total_score'])} 分，"
            f"及格分 {_fmt(overview['pass_score'])} 分，考试时长 {overview['duration_minutes']} 分钟；"
            f"分 {overview['section_count']} 个大题；"
            f"目标难度系数 {overview['target_difficulty']}，"
            f"实际难度系数 {overview['actual_difficulty']}"
            f"（偏差 {overview['difficulty_deviation']:+}）；"
            f"组卷方式：{overview['generation_mode_display']}"
        )

    @staticmethod
    def _type_text(rows: list) -> str:
        if not rows:
            return "（无）"
        return "；".join(
            f"{row['label']} {row['count']} 题，每题 {_fmt(row['score'] / row['count'])} 分，"
            f"小计 {_fmt(row['score'])} 分"
            for row in rows
        )

    @staticmethod
    def _difficulty_text(rows: list) -> str:
        parts = [
            f"{row['label']} {row['count']} 题（{row['ratio']:.1%}，{_fmt(row['score'])} 分）"
            for row in rows if row["count"]
        ]
        return "；".join(parts) if parts else "（无）"

    @staticmethod
    def _knowledge_text(coverage: dict) -> str:
        items = coverage["items"]
        if not items:
            return "（未标注知识点）"
        names = "、".join(f"{item['name']}（{item['question_count']} 题）" for item in items[:15])
        tail = f"，另还有 {len(items) - 15} 个知识点" if len(items) > 15 else ""
        text = f"共覆盖 {coverage['distinct_count']} 个知识点：{names}{tail}"
        if coverage["missing"]:
            missing = "、".join(item["name"] for item in coverage["missing"][:8])
            text += f"。计划覆盖但未覆盖到：{missing}"
        return text

    @staticmethod
    def _warning_text(warnings: list) -> str:
        if not warnings:
            return "系统未检测到明显问题。"
        return "\n".join(f"- {item['message']}" for item in warnings)


__all__ = ["PaperAIService"]
