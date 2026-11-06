"""跨模块的查询范围约束。

「每个老师只能管理自己的学科」的落地：教师账号一旦填了 ``subject``（任教学科），
其能看到的题库 / 试卷 / 考试就只限该学科；未填 subject 的教师（如内置演示账号）与
管理员不受限。这样既实现了学科隔离，又不破坏已有的演示数据与测试。
"""

from .constants import Role


def teacher_subject_scope(user, queryset, field: str = "subject"):
    """非管理员教师只看自己任教学科的资源；其余（管理员 / 无学科教师）看全部。"""
    if getattr(user, "role", None) == Role.TEACHER and getattr(user, "subject", ""):
        return queryset.filter(**{field: user.subject})
    return queryset


def teacher_subject(user) -> str:
    """教师的任教学科；非教师或未填学科时返回空串（表示不限）。"""
    if getattr(user, "role", None) == Role.TEACHER:
        return getattr(user, "subject", "") or ""
    return ""


__all__ = ["teacher_subject", "teacher_subject_scope"]
