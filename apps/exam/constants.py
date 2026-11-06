"""在线考试模块常量。

``ExamStatus`` / ``AttemptStatus`` 是全项目共用的状态枚举，定义在 ``apps.common.constants``，
这里只做转出，保证「考试状态」在整个系统里只有一份定义。
"""

from apps.common.constants import AttemptStatus, ExamStatus  # noqa: F401  （对外转出）

from django.db import models


class SubmitReason(models.TextChoices):
    """交卷原因。事后复盘时能分清「学生自己交的」和「系统代交的」。"""

    MANUAL = "manual", "学生主动交卷"
    TIMEOUT = "timeout", "超时自动交卷"
    FORCED = "forced", "违规强制交卷"
    SYSTEM = "system", "系统代交"


class BehaviorEvent(models.TextChoices):
    """前端行为埋点类型（模块四只做聚合计数，明细事件留到模块八）。"""

    SWITCH = "switch", "切屏/离开页面"
    PASTE = "paste", "粘贴"
    FULLSCREEN_EXIT = "fullscreen_exit", "退出全屏"
    BLUR = "blur", "窗口失焦"
    HEARTBEAT = "heartbeat", "心跳"


class MultipleChoicePolicy(models.TextChoices):
    """多选题给分策略（考试级配置）。

    多选题天然有「漏选」「错选」两种情况，怎么给分是教学策略而不是技术问题，
    所以做成每场考试可配。判分实现在模块五（``apps.grading``），
    枚举定义放这里是因为它是 ``Exam`` 上的一列。

    * ``STRICT``  —— 全对才得分，错一个或多选一个都是 0 分（最保守，默认）；
    * ``PARTIAL`` —— 漏选按「选对的个数 / 正确项个数」给部分分，**错选不得分**；
    * ``PENALTY`` —— 漏选给部分分，错选倒扣（按错选项数从正确项里减），最低 0 分。
    """

    STRICT = "strict", "严格：全对才得分"
    PARTIAL = "partial", "漏选给部分分，错选不得分"
    PENALTY = "penalty", "漏选给部分分，错选倒扣"


# ------------------------------------------------------------------
# 默认值（考试创建时的兜底参数）
# ------------------------------------------------------------------
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_MAX_SWITCH_COUNT = 5
DEFAULT_DURATION_MINUTES = 90
# 多选题默认按最严格策略给分（全对才得分）
DEFAULT_MULTIPLE_CHOICE_POLICY = MultipleChoicePolicy.STRICT

# ------------------------------------------------------------------
# 计时相关
# ------------------------------------------------------------------
# 前端自动保存间隔（秒）。接口契约的一部分，前端按这个节奏打，服务端不做强制。
AUTOSAVE_INTERVAL_SECONDS = 30
# 心跳间隔（秒）：既要能及时发现断线，又不能把服务器打满
HEARTBEAT_INTERVAL_SECONDS = 20
# 交卷宽限期（秒）。
# 为什么需要它：网络抖动 + 最后一秒点交卷，很容易刚好越过 deadline。
# 给一个很短的白名单窗口，既不让「超时后继续答题」变得有利可图，也不至于因为
# 一次网络慢就判学生超时。
SUBMIT_GRACE_SECONDS = 30

# 全部行为计数允许上报的字段（防作弊统计口径）
BEHAVIOR_COUNTER_FIELDS = {
    BehaviorEvent.SWITCH: "switch_count",
    BehaviorEvent.PASTE: "paste_count",
    BehaviorEvent.FULLSCREEN_EXIT: "fullscreen_exit_count",
}


__all__ = [
    "AUTOSAVE_INTERVAL_SECONDS",
    "BEHAVIOR_COUNTER_FIELDS",
    "AttemptStatus",
    "BehaviorEvent",
    "DEFAULT_DURATION_MINUTES",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_SWITCH_COUNT",
    "DEFAULT_MULTIPLE_CHOICE_POLICY",
    "ExamStatus",
    "HEARTBEAT_INTERVAL_SECONDS",
    "MultipleChoicePolicy",
    "SUBMIT_GRACE_SECONDS",
    "SubmitReason",
]
