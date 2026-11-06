"""作弊行为检测模块常量。

模块八要回答两个问题，这个文件把两个问题的「尺子」都固定在一处：

1. **「这个学生可疑吗？」** —— 规则引擎把逐条埋点翻译成可读的「可疑信号」。
   每条规则的触发阈值、分值、等级都写在 ``RULE_CATALOG`` 与其上方的阈值常量里，
   页面、接口、测试、冒烟全部读这一份，避免「看板说可疑、详情页说正常」。
2. **「有没有异常的作答？」** —— 无监督异常检测（孤立森林）在规则之外补一层
   离群发现。训练样本下限 ``ANOMALY_MIN_SAMPLES`` 沿用模块六「样本不足不给结论」
   的思路：样本太少时老实说「不够判」，不硬凑一个异常名单出来。

与模块四的分工
--------------
模块四只在 ``ExamAttempt`` 上做**聚合计数**（切屏 / 粘贴 / 退出全屏），
逐条明细是有意留给模块八的。所以这里**不重复定义**事件类型，
而是直接转出 ``apps.exam.constants.BehaviorEvent``——「切屏」在全系统只有一处定义。
"""

from django.db import models

# 行为事件枚举的唯一来源在考试模块（模块四负责采集，模块八负责分析）
from apps.exam.constants import BehaviorEvent  # noqa: F401  （对外转出）

# ==================================================================
# 风险分档
# ==================================================================
class RiskLevel(models.TextChoices):
    """一次作答的风险档位（由规则引擎总分映射而来）。"""

    NORMAL = "normal", "正常"
    LOW = "low", "低度可疑"
    MEDIUM = "medium", "中度可疑"
    HIGH = "high", "高度可疑"
    SEVERE = "severe", "严重违规"


# （下限分数, 档位）——从高到低排列，命中第一个满足的
RISK_BANDS = (
    (80, RiskLevel.SEVERE),
    (55, RiskLevel.HIGH),
    (30, RiskLevel.MEDIUM),
    (1, RiskLevel.LOW),
    (0, RiskLevel.NORMAL),
)

# 规则加总后封顶——再多规则也不至于把分数顶到天上，档位才稳定
RISK_SCORE_CAP = 100

# 「可疑」的门槛：达到这个档位以上才算进了可疑名单（看板默认只列这些人）
SUSPICIOUS_MIN_LEVEL = RiskLevel.MEDIUM
# 可疑名单里「档位由高到低」的排序权重（越大越靠前）
RISK_ORDER = {
    RiskLevel.NORMAL: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.SEVERE: 4,
}


def level_for(score: int) -> str:
    """风险分 → 档位。分档只在这里做一次，别处不要再写一套。"""
    value = max(0, int(score or 0))
    for lower, level in RISK_BANDS:
        if value >= lower:
            return level
    return RiskLevel.NORMAL


# ==================================================================
# 规则阈值（唯一取值处；考试级覆盖见 RuleEngineService）
# ==================================================================
# 切屏：超过本场考试允许的次数即触发（上限取自 exam.max_switch_count，可逐场配）
# 切屏频率：作答时长达到下限分钟后，每分钟切屏次数达到阈值
SWITCH_FREQUENCY_PER_MINUTE = 2.0
SWITCH_FREQUENCY_MIN_MINUTES = 3
# 单次离开页面的时长（秒）：超过即「长时间离开」，达到 high 档则升级
LONG_ABSENCE_SECONDS = 60
LONG_ABSENCE_HIGH_SECONDS = 180
# 粘贴：达到该次数触发（一次也算，但按次数升级）
PASTE_OVER_LIMIT = 1
# 退出全屏：达到该次数触发
FULLSCREEN_EXIT_OVER_LIMIT = 2
# 答题速度：平均每题秒数低于该值、且作答题数达到下限，判「快得不像在做题」
FAST_ANSWER_SECONDS_PER_QUESTION = 5.0
FAST_ANSWER_MIN_QUESTIONS = 3
# 短时突发：一个滑动窗口内的事件条数达到阈值
BURST_WINDOW_SECONDS = 60
BURST_EVENT_COUNT = 5
# 环境变更：同一次作答里出现多个 IP / 多种设备指纹
ENV_CHANGE_ENABLED = True

# 参与统计的事件类型：心跳太频繁且无信息量，不计入行为分析
ANALYZED_EVENT_TYPES = (
    BehaviorEvent.SWITCH,
    BehaviorEvent.PASTE,
    BehaviorEvent.FULLSCREEN_EXIT,
    BehaviorEvent.BLUR,
)
# 视为「切屏类」的事件（计数器里 blur 也并进 switch_count，规则要对得上）
SWITCH_LIKE_EVENTS = (BehaviorEvent.SWITCH, BehaviorEvent.BLUR)

# ==================================================================
# 规则目录（每条规则的分值 / 等级 / 说明，页面与接口直接展示）
# ==================================================================
# ``points`` 是命中一次的基础分；部分规则会按严重程度叠加（见 RuleEngineService）。
RULE_CATALOG = (
    {
        "code": "switch_over_limit",
        "name": "切屏次数超限",
        "metric": "switch_count",
        "points": 30,
        "level": RiskLevel.HIGH,
        "threshold_desc": "切屏/离开页面次数超过本场考试允许的次数（考试级可配）",
        "advice": "回放作答过程，确认是否有查资料的时间窗",
    },
    {
        "code": "switch_frequent",
        "name": "切屏过于频繁",
        "metric": "switch_per_minute",
        "points": 18,
        "level": RiskLevel.MEDIUM,
        "threshold_desc": f"作答时长 ≥{SWITCH_FREQUENCY_MIN_MINUTES} 分钟后，"
                          f"每分钟切屏 ≥{SWITCH_FREQUENCY_PER_MINUTE} 次",
        "advice": "结合逐条时间线判断是「频繁离开」还是「误触」",
    },
    {
        "code": "long_absence",
        "name": "单次离开过久",
        "metric": "max_absence_seconds",
        "points": 25,
        "level": RiskLevel.HIGH,
        "threshold_desc": f"单次离开页面 ≥{LONG_ABSENCE_SECONDS} 秒"
                          f"（≥{LONG_ABSENCE_HIGH_SECONDS} 秒升级为高度可疑）",
        "advice": "长时间离开且期间无作答，重点核对",
    },
    {
        "code": "paste_content",
        "name": "粘贴外部内容",
        "metric": "paste_count",
        "points": 20,
        "level": RiskLevel.HIGH,
        "threshold_desc": f"粘贴次数 ≥{PASTE_OVER_LIMIT} 次（按次数升级）",
        "advice": "查看该题作答是否与外部文本高度雷同",
    },
    {
        "code": "fullscreen_exit",
        "name": "多次退出全屏",
        "metric": "fullscreen_exit_count",
        "points": 12,
        "level": RiskLevel.MEDIUM,
        "threshold_desc": f"退出全屏次数 ≥{FULLSCREEN_EXIT_OVER_LIMIT} 次",
        "advice": "全屏要求下频繁退出，可能是为了切到其他窗口",
    },
    {
        "code": "answer_too_fast",
        "name": "答题速度异常",
        "metric": "seconds_per_question",
        "points": 15,
        "level": RiskLevel.MEDIUM,
        "threshold_desc": f"已答 ≥{FAST_ANSWER_MIN_QUESTIONS} 题且平均每题"
                          f" <{FAST_ANSWER_SECONDS_PER_QUESTION} 秒",
        "advice": "速度远低于同时长考生的正常水平，核对是否抄答案或代答",
    },
    {
        "code": "ip_changed",
        "name": "作答期间 IP 变更",
        "metric": "ip_count",
        "points": 22,
        "level": RiskLevel.HIGH,
        "threshold_desc": "同一次作答的上报来源出现多个 IP",
        "advice": "可能中途换设备/网络，或由他人代考，需核对身份",
    },
    {
        "code": "device_changed",
        "name": "作答期间设备变更",
        "metric": "distinct_user_agents",
        "points": 18,
        "level": RiskLevel.MEDIUM,
        "threshold_desc": "同一次作答的上报来源出现多个 User-Agent",
        "advice": "结合 IP 变更一起判断是否换机作答",
    },
    {
        "code": "event_burst",
        "name": "短时间内密集事件",
        "metric": "burst_max",
        "points": 12,
        "level": RiskLevel.MEDIUM,
        "threshold_desc": f"任意 {BURST_WINDOW_SECONDS} 秒窗口内行为事件 ≥{BURST_EVENT_COUNT} 条",
        "advice": "短时间大量异常动作，可能是脚本或慌乱切屏",
    },
)

RULE_CODES = tuple(rule["code"] for rule in RULE_CATALOG)


def rule_meta(code: str) -> dict:
    """按 code 取规则元信息。取不到返回空字典（调用方自行兜底）。"""
    for rule in RULE_CATALOG:
        if rule["code"] == code:
            return dict(rule)
    return {}


# ==================================================================
# 异常检测（孤立森林）
# ==================================================================
# 训练样本下限：少于这么多份作答，无监督模型学不出「正常长什么样」，不给结论
ANOMALY_MIN_SAMPLES = 8
# 预估离群比例（孤立森林的 contamination）：一场考试里少数人才是真的异常
ANOMALY_CONTAMINATION = 0.15
# 固定随机种子——同样的数据每次跑出同样的名单，教师复查时不会「一次一个样」
ANOMALY_RANDOM_STATE = 42
# 特征向量顺序（顺序即含义，改动要同步改 AnomalyDetectionService.feature_vector）
ANOMALY_FEATURES = (
    "switch_count",
    "paste_count",
    "fullscreen_exit_count",
    "max_absence_seconds",
    "event_per_minute",
    "seconds_per_question",
    "ip_count",
    "device_changed",
    "burst_max",
)
# 未安装 scikit-learn 时的降级算法：稳健 z-score（median + MAD）的离群阈值
ROBUST_Z_THRESHOLD = 3.5
# 兜底标准差系数（1.4826 ≈ 正态分布下 MAD 到 σ 的换算系数）
MAD_TO_SIGMA = 1.4826

# ==================================================================
# 埋点保留策略
# ==================================================================
# 明细埋点是「写多读少」的数据，考完一段时间的明细可以裁剪（归档命令用这个天数）
LOG_RETENTION_DAYS = 180
# 一次抓取的埋点条数上限（避免超大作答把响应体撑爆）
MAX_TIMELINE_EVENTS = 500
