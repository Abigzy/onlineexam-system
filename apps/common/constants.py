"""
项目级常量与枚举。

集中定义，避免同一个枚举在多个 app 里各写一份、日后不一致。
"""

from django.db import models


class Role(models.TextChoices):
    """用户角色。系统里所有权限判断都以这个为准。"""

    ADMIN = "admin", "管理员"
    TEACHER = "teacher", "教师"
    STUDENT = "student", "学生"


class QuestionType(models.TextChoices):
    """题型。客观题可机器判分，主观题交给 AI 辅助批改。"""

    SINGLE = "single", "单选题"
    MULTIPLE = "multiple", "多选题"
    JUDGE = "judge", "判断题"
    FILL = "fill", "填空题"
    SHORT = "short", "简答题"
    ESSAY = "essay", "论述题"

    @classmethod
    def objective_types(cls):
        """可精确匹配判分的题型。"""
        return [cls.SINGLE, cls.MULTIPLE, cls.JUDGE, cls.FILL]

    @classmethod
    def subjective_types(cls):
        """需要 AI / 人工判分的题型。"""
        return [cls.SHORT, cls.ESSAY]


class Difficulty(models.IntegerChoices):
    """难度等级。数值直接用作难度系数，便于加权计算。"""

    EASY = 1, "简单"
    MEDIUM = 2, "中等"
    HARD = 3, "困难"
    EXPERT = 4, "挑战"


class ExamStatus(models.TextChoices):
    """考试状态机：草稿 → 已发布 → 进行中 → 已结束 → 已归档。"""

    DRAFT = "draft", "草稿"
    PUBLISHED = "published", "已发布"
    ONGOING = "ongoing", "进行中"
    FINISHED = "finished", "已结束"
    ARCHIVED = "archived", "已归档"


class AttemptStatus(models.TextChoices):
    """一次作答记录的状态。"""

    IN_PROGRESS = "in_progress", "答题中"
    SUBMITTED = "submitted", "已交卷"
    AUTO_GRADED = "auto_graded", "自动判分完成"
    REVIEWING = "reviewing", "待人工复核"
    GRADED = "graded", "已出分"
    TIMEOUT = "timeout", "超时交卷"


# ------------------------------------------------------------------
# 统一响应体里 code 的取值约定
# ------------------------------------------------------------------
# 0        —— 成功
# 4xxxx    —— 客户端错误，等于 HTTP 状态码 × 100（40000 / 40100 / 40300 / 40400 ...）
# 5xxxx    —— 服务端错误（50000 ...）
# 1xxxx    —— 业务语义错误（由具体模块自行定义，如 10001 = 用户名已存在）
API_SUCCESS_CODE = 0
API_MSG_SUCCESS = "success"

# ------------------------------------------------------------------
# 认证失败的细分业务码（接口 / 中间件共用，避免两处各写一份而漂移）
# ------------------------------------------------------------------
# 注意：DRF 的 ``APIException`` 第二个位置参数是「错误标识字符串」，不是业务码，
# 所以 ``AuthenticationFailed("...", code=40101)`` 里的 40101 从未进过响应体。
# 需要真实透传业务码时，一律抛 ``BusinessError(code=...)``（见 unified_exception_handler）。
CODE_AUTH_FAILED = 40100        # 通用认证失败：账号不存在 / 密码错误
CODE_ACCOUNT_INACTIVE = 40101   # 账号已停用（登录接口与停用踢下线中间件同一个码）
