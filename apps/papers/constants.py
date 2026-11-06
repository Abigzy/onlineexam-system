"""试卷与组卷模块常量。"""

from django.db import models


class PaperStatus(models.TextChoices):
    """试卷生命周期。

    草稿可以随便改；已发布的试卷会被考试引用（模块四），改动要谨慎；
    归档 = 软下线，不再出现在默认列表里，但历史考试仍能正常查看。
    """

    DRAFT = "draft", "草稿"
    PUBLISHED = "published", "已发布"
    ARCHIVED = "archived", "已归档"


class PaperGenerationMode(models.TextChoices):
    """这份卷子是怎么来的——便于统计「AI 组卷到底省了多少事」。"""

    MANUAL = "manual", "手动组卷"
    AI = "ai", "AI 智能组卷"
    RANDOM = "random", "随机组卷"
    HYBRID = "hybrid", "AI + 手动调整"


# 各大题的标准标题，自动建分区时用。
# 用「一二三」的中文序号是为了直接印在卷子上时不用再改。
SECTION_TITLE_BY_TYPE = {
    "single": "单项选择题",
    "multiple": "多项选择题",
    "judge": "判断题",
    "fill": "填空题",
    "short": "简答题",
    "essay": "论述题",
}

# 中文大题序号
SECTION_ORDINALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

# 各大题固定附带的答题说明，印在卷面上
SECTION_HINT_BY_TYPE = {
    "single": "每题只有一个正确答案，请将正确选项的字母填入答题处。",
    "multiple": "每题有多个正确答案，多选、少选、错选均不得分。",
    "judge": "判断下列说法是否正确，正确的选 A，错误的选 B。",
    "fill": "请将答案写在题目中的横线处。",
    "short": "请简要回答，答出要点即可。",
    "essay": "请展开论述，要求条理清晰、论证充分。",
}

# 生成「大题标题」时，按题型固定这个顺序排，避免每次组卷分区顺序都不一样
SECTION_TYPE_ORDER = ["single", "multiple", "judge", "fill", "short", "essay"]

# 随机组卷时各题型的默认每题分值（与 DEFAULT_BLUEPRINT 保持同一套口径）
RANDOM_SCORE_BY_TYPE = {
    "single": 2,
    "multiple": 3,
    "judge": 1,
    "fill": 2,
    "short": 10,
    "essay": 15,
}

# 随机组卷默认题量
RANDOM_DEFAULT_COUNT = 20


# 组卷预警的阈值（放在这里方便统一调整）
class AnalysisThreshold:
    """组卷质量分析的预警阈值。"""

    # 平均难度与目标难度的偏差超过这个值就提醒
    DIFFICULTY_TOLERANCE = 0.35
    # 知识点覆盖率的及格线（选中题目涉及的知识点 / 计划覆盖的知识点）
    MIN_KNOWLEDGE_COVERAGE = 0.6
    # 同一道题在一份卷子里最多出现几次（去重后必然是 1，这里防的是不同分区重复放）
    MAX_DUPLICATE_PER_PAPER = 1


__all__ = [
    "AnalysisThreshold",
    "PaperGenerationMode",
    "PaperStatus",
    "RANDOM_DEFAULT_COUNT",
    "RANDOM_SCORE_BY_TYPE",
    "SECTION_HINT_BY_TYPE",
    "SECTION_ORDINALS",
    "SECTION_TITLE_BY_TYPE",
    "SECTION_TYPE_ORDER",
]
