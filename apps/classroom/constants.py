"""班级模块的常量与业务枚举。

班级本身没有太多可配阈值，这里主要收「谁有资格进班级」这类角色约束，
避免散落在 service / serializer 里各写一遍。
"""

from django.db import models

from apps.common.constants import Role

# 可担任班主任 / 授课教师的角色（管理员天然拥有教师权限，也一并允许）
TEACHER_ROLES = (Role.TEACHER, Role.ADMIN)

# 学生成员的唯一合法角色
STUDENT_ROLE = Role.STUDENT


class ClassType(models.TextChoices):
    """班级类型。

    * 行政班 —— 学生学籍所在班，如「计算机 1 班」；
    * 学科班 —— 按课程划分的教学班，如「数据结构 1 班」，可挂靠某个行政班。
    """

    ADMIN = "admin", "行政班"
    SUBJECT = "subject", "学科班"


# 归档（软删除）后仍可追溯，与题库 DELETE 的取舍一致
# （班级被归档，历史成绩/学情里的班级归属不能跟着消失）

# 越权访问「确实存在」的班级时返回的业务码（HTTP 403）。
#
# 口径：班级 id 不是敏感资源（老师本来就能在行政班列表里看到全校班级），
# 误点别人的班时应得到「权限不足」的明确提示，而不是被 404 误导成「班级被删了」。
# 用户 / 成绩这类真正敏感的资源仍保持 404 —— 不拿 403 当存在性探针。
CLASSROOM_PERM_DENIED = 14003
