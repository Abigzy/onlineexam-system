"""题库模块常量。"""

from django.db import models


class QuestionSource(models.TextChoices):
    """题目来源，用于统计「AI 到底贡献了多少题」。"""

    MANUAL = "manual", "手动录入"
    AI = "ai", "AI 生成"
    IMPORT = "import", "批量导入"


class AIGenerateScene(models.TextChoices):
    """AI 出题的素材来源。"""

    TEXT = "text", "粘贴教材文本"
    KNOWLEDGE_ONLY = "knowledge", "仅按知识点生成"


# 各题型的默认分值（教师可覆盖）
DEFAULT_SCORE_BY_TYPE = {
    "single": 2.0,
    "multiple": 3.0,
    "judge": 1.0,
    "fill": 2.0,
    "short": 5.0,
    "essay": 10.0,
}

# 需要选项的题型
CHOICE_TYPES = ("single", "multiple", "judge")

# 判断题的标准选项
JUDGE_OPTIONS = [("A", "正确"), ("B", "错误")]
