"""
权限类。

用法：在 ViewSet 上挂 ``permission_classes = [IsTeacher]``，
或对单个 @action 覆写 ``@action(..., permission_classes=[IsAdmin])``。
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .constants import Role


class IsAdmin(BasePermission):
    """管理员（或 Django 超级用户）。"""

    message = "需要管理员权限"

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_superuser or user.role == Role.ADMIN))


class IsTeacher(BasePermission):
    """教师（管理员天然拥有全部权限）。"""

    message = "需要教师权限"

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_superuser or user.role in (Role.TEACHER, Role.ADMIN))
        )


class IsStudent(BasePermission):
    """学生。"""

    message = "需要学生权限"

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role == Role.STUDENT)


class IsTeacherOrAdmin(IsTeacher):
    """语义别名：教师或管理员。"""


class IsOwnerOrReadOnly(BasePermission):
    """对象级权限：只有创建者能改，其他人只读。

    需要模型上有 ``created_by`` / ``user`` 之类的归属字段，
    通过视图的 ``owner_field`` 指定（默认 ``user``）。
    """

    message = "只能操作属于自己的数据"

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        owner_field = getattr(view, "owner_field", "user")
        owner = getattr(obj, owner_field, None)
        return owner == request.user or IsAdmin().has_permission(request, view)


class IsSelfOrAdmin(BasePermission):
    """对象级权限：本人或管理员。"""

    message = "只能操作自己的账号"

    def has_object_permission(self, request, view, obj):
        user = request.user
        return bool(user and user.is_authenticated and (obj == user or IsAdmin().has_permission(request, view)))
