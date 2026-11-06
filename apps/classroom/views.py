"""班级模块接口。

路由总览（``/api/v1/`` 下）：

    GET    /classrooms/                    班级列表（按角色可见范围）
    POST   /classrooms/                    建班（教师 / 管理员）
    GET    /classrooms/options/            班级下拉（供成绩单 / 学情页按班筛选）
    GET    /classrooms/{id}/               班级详情
    PUT    /classrooms/{id}/               全量更新（教师 / 管理员）
    PATCH  /classrooms/{id}/               局部更新（教师 / 管理员）
    DELETE /classrooms/{id}/               归档（软删除，教师 / 管理员）
    GET    /classrooms/{id}/students/      成员列表
    POST   /classrooms/{id}/students/      添加学生（按 id / 学号 / 行政班整班导入）
    DELETE /classrooms/{id}/students/      移出学生

权限边界：
  * 列表 / 详情 / 成员列表：登录即可，但**只能看自己可见范围**（学生=自己所在班，
    教师=可管理或任教的班，管理员=全部）；
  * 建班 / 改班 / 成员增删：教师（仅自己创建或担任班主任的班）+ 管理员；
  * 班级**不存在** → 404；班级存在但超出可见 / 可管理范围 → **403**（业务码
    ``CLASSROOM_PERM_DENIED``）。理由：班级 id 不是敏感资源，老师误点别人的班
    应当得到「权限不足」的明确提示，404 会让人误以为班级被删了。用户 / 成绩
    这类敏感资源仍保持 404，本项目「不拿 403 当存在性探针」的口径没有变。
"""

import logging

from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.common.exceptions import BusinessError
from apps.common.permissions import IsTeacher
from apps.common.response import created, success

from .constants import CLASSROOM_PERM_DENIED
from .models import Classroom
from .serializers import (
    AddStudentsSerializer,
    ClassroomMemberSerializer,
    ClassroomSerializer,
    ClassroomWriteSerializer,
    RemoveStudentsSerializer,
)
from .services import ClassroomService

logger = logging.getLogger("apps.classroom.views")


def _classroom_exists(classroom_id: int) -> bool:
    """班级是否真实存在（含已归档）——只用于区分 404 / 403，不当作业务判断。"""
    return Classroom.objects.filter(pk=classroom_id).exists()


def _get_visible_classroom(user, classroom_id: int) -> Classroom:
    """按可见范围取班级：不存在 → 404，存在但无权 → 403。

    取舍：这里刻意把「无权」暴露成 403 而不是跟着 404。班级 id 不是敏感资源
    （教师本来就能看到全校行政班），把它统一成 404 只会让误点别人班级的老师
    收到「请求的资源不存在」，以为班级被删了。**用户 / 成绩类资源仍保持 404**，
    免得 403 变成「这个 id 存在」的探针。
    """
    classroom = (
        Classroom.objects.visible_to(user).with_relations().filter(pk=classroom_id).first()
    )
    if classroom is not None:
        return classroom
    if not _classroom_exists(classroom_id):
        raise Http404("请求的资源不存在")
    raise BusinessError(
        "你没有权限查看这个班级",
        code=CLASSROOM_PERM_DENIED,
        status_code=status.HTTP_403_FORBIDDEN,
    )


def _get_manageable_classroom(user, classroom_id: int) -> Classroom:
    """按可管理范围取班级：不存在 → 404，存在但无权 → 403。

    这是所有**写操作**的入口：校验在 service 调用之前完成，无权时直接抛出，
    保证不会产生任何写入（成员增删 / 改班 / 归档都不会被越权触发）。
    """
    classroom = (
        Classroom.objects.manageable_by(user).with_relations().filter(pk=classroom_id).first()
    )
    if classroom is not None:
        return classroom
    if not _classroom_exists(classroom_id):
        raise Http404("请求的资源不存在")
    raise BusinessError(
        "你没有权限管理这个班级",
        code=CLASSROOM_PERM_DENIED,
        status_code=status.HTTP_403_FORBIDDEN,
    )


def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


@extend_schema(tags=["班级管理"])
class ClassroomListView(APIView):
    """班级列表 + 建班。"""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsTeacher()]
        return [IsAuthenticated()]

    @extend_schema(summary="班级列表", parameters=[
        OpenApiParameter("keyword", str, description="按名称 / 年级 / 学科搜索"),
        OpenApiParameter("class_type", str, description="按类型过滤：admin 行政班 / subject 学科班"),
        OpenApiParameter("include_inactive", bool, description="是否含已归档班级"),
    ])
    def get(self, request):
        queryset = Classroom.objects.visible_to(request.user).search(
            request.query_params.get("keyword", ""),
        )
        class_type = (request.query_params.get("class_type") or "").strip()
        if class_type in ("admin", "subject"):
            queryset = queryset.filter(class_type=class_type)
        if not _truthy(request.query_params.get("include_inactive")):
            queryset = queryset.active()
        rows = ClassroomSerializer(
            queryset.with_relations(), many=True, context={"request": request},
        ).data
        return success(data={"count": len(rows), "results": rows})

    @extend_schema(summary="建班", request=ClassroomWriteSerializer)
    def post(self, request):
        data = ClassroomWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        classroom = ClassroomService.create(
            request.user,
            name=payload["name"],
            grade_year=payload.get("grade_year", ""),
            subject=payload.get("subject", ""),
            description=payload.get("description", ""),
            class_type=payload.get("class_type"),
            admin_class_id=payload.get("admin_class_id"),
            head_teacher_id=payload.get("head_teacher_id"),
            teacher_ids=payload.get("teacher_ids"),
        )
        return created(data=ClassroomSerializer(
            classroom, context={"request": request},
        ).data)


@extend_schema(tags=["班级管理"])
class ClassroomOptionsView(APIView):
    """可分析 / 可管理的班级下拉（供成绩单、学情页按班筛选）。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="班级下拉选项")
    def get(self, request):
        queryset = Classroom.objects.visible_to(request.user).active().order_by("name")
        rows = [{"id": c.id, "name": c.name, "grade_year": c.grade_year, "subject": c.subject}
                for c in queryset[:500]]
        return success(data={"count": len(rows), "results": rows})


@extend_schema(tags=["班级管理"])
class ClassroomDetailView(APIView):
    """班级详情 / 更新 / 归档。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(summary="班级详情")
    def get(self, request, classroom_id):
        classroom = _get_visible_classroom(request.user, classroom_id)
        return success(data=ClassroomSerializer(
            classroom, context={"request": request},
        ).data)

    @extend_schema(summary="全量更新", request=ClassroomWriteSerializer)
    def put(self, request, classroom_id):
        classroom = _get_manageable_classroom(request.user, classroom_id)
        data = ClassroomWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        classroom = ClassroomService.update(classroom, **data.validated_data)
        return success(data=ClassroomSerializer(
            classroom, context={"request": request},
        ).data)

    @extend_schema(summary="局部更新", request=ClassroomWriteSerializer)
    def patch(self, request, classroom_id):
        classroom = _get_manageable_classroom(request.user, classroom_id)
        data = ClassroomWriteSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        classroom = ClassroomService.update(classroom, **data.validated_data)
        return success(data=ClassroomSerializer(
            classroom, context={"request": request},
        ).data)

    @extend_schema(summary="删除班级")
    def delete(self, request, classroom_id):
        classroom = _get_manageable_classroom(request.user, classroom_id)
        ClassroomService.delete(classroom)
        return success(data=None, message="班级已删除")


@extend_schema(tags=["班级管理"])
class ClassroomMembersView(APIView):
    """成员列表 / 添加 / 移出。"""

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated()]
        return [IsTeacher()]

    @extend_schema(summary="成员列表")
    def get(self, request, classroom_id):
        classroom = _get_visible_classroom(request.user, classroom_id)
        members = classroom.students.select_related().order_by("student_no", "id")
        data = ClassroomMemberSerializer(members, many=True).data
        return success(data={"count": len(data), "results": data})

    @extend_schema(summary="添加学生（按 id / 学号 / 行政班整班导入）",
                   request=AddStudentsSerializer)
    def post(self, request, classroom_id):
        classroom = _get_manageable_classroom(request.user, classroom_id)
        data = AddStudentsSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        # 整班导入优先：同时传了 from_classroom_id 与学号时，以整班导入为准
        if payload.get("from_classroom_id"):
            # 源班只要求「存在且启用」——行政班对教师本来就全部可见，不必走
            # manageable_by；写操作的目标班仍是 manageable_by 卡过的。
            # 「是不是行政班」交给 service 判：这样学科班得到的是「只能从行政班
            # 整班导入」，而不是含糊的「不存在」。
            source = Classroom.objects.filter(
                pk=payload["from_classroom_id"], is_active=True,
            ).first()
            if source is None:
                raise BusinessError("行政班不存在或已归档")
            result = ClassroomService.add_students_from_classroom(classroom, source)
        elif payload.get("student_nos"):
            result = ClassroomService.add_students_by_no(classroom, payload["student_nos"])
        else:
            result = ClassroomService.add_students(
                classroom, payload.get("student_ids") or [],
            )
        return success(data=result)

    @extend_schema(summary="移出学生", request=RemoveStudentsSerializer)
    def delete(self, request, classroom_id):
        classroom = _get_manageable_classroom(request.user, classroom_id)
        data = RemoveStudentsSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        removed = ClassroomService.remove_students(
            classroom, data.validated_data["student_ids"],
        )
        return success(data={"removed": removed})
