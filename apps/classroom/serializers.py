"""班级模块序列化器。

读 / 写分离：读序列化器给页面和列表，写序列化器只收白名单字段并做角色校验。
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .constants import STUDENT_ROLE, TEACHER_ROLES, ClassType
from .models import Classroom
from .services import ClassroomService

User = get_user_model()


def _user_brief(user):
    """用户摘要（避免把敏感字段透出去）。"""
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "student_no": getattr(user, "student_no", "") or "",
        "role": user.role,
    }


class ClassroomSerializer(serializers.ModelSerializer):
    """班级读序列化器。"""

    head_teacher = serializers.SerializerMethodField()
    teachers = serializers.SerializerMethodField()
    student_count = serializers.IntegerField(read_only=True)
    teacher_count = serializers.IntegerField(read_only=True)
    created_by = serializers.SerializerMethodField()
    class_type_display = serializers.CharField(source="get_class_type_display", read_only=True)
    admin_class = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Classroom
        fields = [
            "id", "name", "grade_year", "subject", "description",
            "class_type", "class_type_display", "admin_class",
            "is_active", "created_at", "updated_at",
            "head_teacher", "teachers", "student_count", "teacher_count",
            "created_by", "can_manage",
        ]

    def get_head_teacher(self, obj):
        return _user_brief(obj.head_teacher)

    def get_teachers(self, obj):
        return [_user_brief(t) for t in obj.teachers.all()]

    def get_created_by(self, obj):
        return _user_brief(obj.created_by)

    def get_admin_class(self, obj):
        if obj.admin_class is None:
            return None
        return {"id": obj.admin_class.id, "name": obj.admin_class.name}

    def get_can_manage(self, obj):
        request = self.context.get("request")
        if request is None:
            return False
        return ClassroomService.can_manage(request.user, obj)


class ClassroomWriteSerializer(serializers.Serializer):
    """建班 / 改班写序列化器（不绑 model，字段语义自控）。"""

    name = serializers.CharField(max_length=64, allow_blank=False)
    grade_year = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    subject = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    class_type = serializers.ChoiceField(
        choices=ClassType.choices, required=False, default=ClassType.ADMIN,
    )
    admin_class_id = serializers.IntegerField(required=False, allow_null=True)
    head_teacher_id = serializers.IntegerField(required=False, allow_null=True)
    teacher_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
    )

    def validate(self, attrs):
        # 只有学科班能挂靠行政班；行政班传了 admin_class_id 直接报错。
        # PATCH 时 class_type 可能没传，取实例现值判断。
        class_type = attrs.get("class_type") or getattr(self.instance, "class_type", None)
        if attrs.get("admin_class_id") and class_type != ClassType.SUBJECT:
            raise serializers.ValidationError({"admin_class_id": "只有学科班才能关联行政班"})
        return attrs

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("班级名称不能为空")
        return value

    def validate_teacher_ids(self, value):
        ids = set(value)
        teachers = User.objects.filter(id__in=ids, role__in=TEACHER_ROLES, is_active=True)
        if len(teachers) != len(ids):
            raise serializers.ValidationError("授课教师中包含无效账号")
        return list(ids)


class AddStudentsSerializer(serializers.Serializer):
    """添加学生：按 id / 按学号 / 从行政班整班导入（三者至少提供一个）。

    ``from_classroom_id`` 只做「是不是整数」的形状校验；「是不是启用的行政班」
    由视图查库判断（那里的错误是 400 业务提示，比字段级报错更贴近用户操作）。
    """

    student_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
    )
    student_nos = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True,
    )
    from_classroom_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if (not attrs.get("student_ids")
                and not attrs.get("student_nos")
                and not attrs.get("from_classroom_id")):
            raise serializers.ValidationError(
                "请提供 from_classroom_id、student_ids 或 student_nos 之一",
            )
        return attrs


class RemoveStudentsSerializer(serializers.Serializer):
    """移出学生。"""

    student_ids = serializers.ListField(
        child=serializers.IntegerField(), required=True, allow_empty=False,
    )


class ClassroomMemberSerializer(serializers.ModelSerializer):
    """学生成员摘要（成员列表用）。"""

    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "display_name", "student_no", "email", "is_active"]
