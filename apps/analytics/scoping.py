# -*- coding: utf-8 -*-
"""学情模块的对象级权限。

口径（与班级模块 ``Classroom.objects.manageable_by()`` 保持一致）：

  * 管理员 —— 可看任何学生；
  * 教师 —— 只能看**自己带的班**（自己创建 / 任班主任 / 在授课名单里）里的学生；
  * 学生 —— 只能看自己。

为什么不直接用 ``visible_to()``：那个方法为了让教师建学科班时能挂靠，
额外放开了「全部行政班」，用它判学情会变成「教师能看全校行政班的学生」，
比班级管理权限还宽，起不到收紧作用。

拒绝时一律 404 而不是 403 —— 与项目「跨人资源不拿 403 当 id 探针」的约定一致。
"""

from apps.classroom.models import Classroom


def can_view_student(user, student) -> bool:
    """当前用户是否可以查看该学生的学情。"""
    if getattr(user, "is_admin", False):
        return True
    if getattr(user, "is_teacher", False):
        if not getattr(user, "is_authenticated", False):
            return False
        return Classroom.objects.manageable_by(user).filter(students=student).exists()
    return getattr(student, "id", None) == getattr(user, "id", None)


def visible_student_ids(user):
    """当前用户可见的学生 id 集合（供需要批量过滤的场景使用）。

    **教师分支必须去重**：一个学生可能同时在教师带的多个班里（行政班 + 学科班、
    或转班后残留），按 ``classrooms__students`` 展开会产生重复行。下游若直接
    ``len()`` 当分母（如学情「应考人数」），人数就会虚高——曾出现同一场考试
    监控页 16、学情页 17 的不一致。
    """
    if getattr(user, "is_admin", False):
        from django.contrib.auth import get_user_model
        from apps.common.constants import Role

        return get_user_model().objects.filter(role=Role.STUDENT).values_list("id", flat=True)
    if getattr(user, "is_teacher", False):
        return (
            Classroom.objects.manageable_by(user)
            # 必须先清空排序：Classroom.Meta.ordering 是 -created_at，Django 会把它
            # 一起塞进 SELECT，于是 DISTINCT 实际作用在 (user_id, created_at) 上，
            # 同一个学生挂在两个创建时间不同的班里照样出现两行——看似去了重其实没去。
            .order_by()
            .values_list("students__id", flat=True)
            .distinct()
        )
    return [getattr(user, "id", None)]


__all__ = ["can_view_student", "visible_student_ids"]
