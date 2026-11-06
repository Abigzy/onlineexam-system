"""学情分析模块常量。

学情分析是一个**纯读模型**：它不产生新的判分结果，只把模块五已经算好的分数
按「学生 × 知识点」「学生 × 时间」「班级 × 题目」三个维度重新组织。

这里定义三类东西：

1. **掌握度分档** —— 一个数字（正确率）要变成一个结论（掌握 / 熟练 / 基本 / 薄弱），
   分档阈值必须写在一处，否则雷达图和薄弱点列表会各说各话。
2. **薄弱判定口径** —— 「正确率低于 60% 算薄弱」来自需求文档与
   ``apps/ai/prompts/analyze_weakness.txt``，两边必须一致，否则 AI 的结论会和
   页面上的红色标记对不上。
3. **样本量下限** —— 学生只做过 1 道某知识点的题且答错，正确率 0%，直接标红
   是在制造焦虑。样本太少只能算「数据不足」，不能算「薄弱」。
"""

from django.db import models


# ==================================================================
# 掌握度分档
# ==================================================================
class MasteryLevel(models.TextChoices):
    """知识点掌握度分档（按正确率 / 得分率）。"""

    MASTERED = "mastered", "已掌握"
    PROFICIENT = "proficient", "较熟练"
    BASIC = "basic", "基本掌握"
    WEAK = "weak", "薄弱"
    UNKNOWN = "unknown", "数据不足"


# （下限比例, 档位）——从高到低排列，命中第一个满足的
# 数值与 `apps/ai/prompts/analyze_weakness.txt` 里的「低于 60% 视为薄弱」对齐
MASTERY_BANDS = (
    (0.85, MasteryLevel.MASTERED),
    (0.70, MasteryLevel.PROFICIENT),
    (0.60, MasteryLevel.BASIC),
    (0.00, MasteryLevel.WEAK),
)

# 薄弱线：正确率低于该值视为薄弱（与提示词一致，改动要同步改提示词）
WEAKNESS_RATIO = 0.60

# 判定「薄弱」所需的最小答题量：不足则归为「数据不足」而不是「薄弱」
MIN_SAMPLES_FOR_WEAKNESS = 2

# 主观题的「达标线」：得分率 ≥ 该值就算这道题掌握（沿用模块五的错题本口径）
SUBJECTIVE_MASTERY_RATIO = 0.60

# 掌握度矩阵 / 雷达图最多展示多少个知识点（按答题量降序取）
RADAR_TOP_N = 8
# 薄弱点列表最多返回多少条
MAX_WEAK_POINTS = 20

# ==================================================================
# 成绩趋势
# ==================================================================
# 线性回归至少需要几个点才有意义：少于 3 个点，斜率完全不可信
TREND_MIN_POINTS = 3
# 斜率绝对值小于该值视为「持平」（得分率每次变化 0.5% 以内不算趋势）
TREND_FLAT_SLOPE = 0.005
# 趋势图默认取最近多少次作答
TREND_DEFAULT_LIMIT = 20
TREND_MAX_LIMIT = 50

# ==================================================================
# 班级分析
# ==================================================================
# 分数段（按得分率百分数）
SCORE_BUCKETS = (
    ("90-100", 90),
    ("80-89", 80),
    ("70-79", 70),
    ("60-69", 60),
    ("0-59", 0),
)
# 区分度计算取「总分前 / 后多少比例」的两组（27% 是教育测量学的常用值）
DISCRIMINATION_GROUP_RATIO = 0.27

# ==================================================================
# AI 学情诊断
# ==================================================================
# 喂给大模型的知识点上限——太多会把提示词撑爆，也会让结论发散
AI_MAX_KNOWLEDGE_POINTS = 20
# AI 缓存有效期（小时）：超过之后允许自动重算，避免学生看到的永远是开学时的诊断
AI_REPORT_TTL_HOURS = 24

# 统计口径：哪些状态的作答参与学情分析
# 只算「已判分」的——还在答题中或刚交卷没判分的参与统计会让结论失真
ANALYZABLE_STATUSES = ("auto_graded", "reviewing", "graded")


__all__ = [
    "AI_MAX_KNOWLEDGE_POINTS",
    "AI_REPORT_TTL_HOURS",
    "ANALYZABLE_STATUSES",
    "DISCRIMINATION_GROUP_RATIO",
    "MASTERY_BANDS",
    "MAX_WEAK_POINTS",
    "MIN_SAMPLES_FOR_WEAKNESS",
    "MasteryLevel",
    "RADAR_TOP_N",
    "SCORE_BUCKETS",
    "SUBJECTIVE_MASTERY_RATIO",
    "TREND_DEFAULT_LIMIT",
    "TREND_FLAT_SLOPE",
    "TREND_MAX_LIMIT",
    "TREND_MIN_POINTS",
    "WEAKNESS_RATIO",
]
