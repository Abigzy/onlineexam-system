"""
题库接口。

路由总览（`/api/v1/` 下）：

    知识点
    GET    /knowledge-points/                 列表（支持 subject / parent / 关键字筛选）
    POST   /knowledge-points/                 新建
    GET    /knowledge-points/tree/            树形结构（前端一次拿全）
    GET    /knowledge-points/{id}/            详情
    PUT    /knowledge-points/{id}/            全量更新
    PATCH  /knowledge-points/{id}/            局部更新
    DELETE /knowledge-points/{id}/            删除（级联子知识点，需谨慎）
    GET    /knowledge-points/{id}/questions/  该知识点（含子知识点）下的题目

    题目
    GET    /questions/                        列表（题型/难度/知识点/关键字等筛选）
    POST   /questions/                        新建
    GET    /questions/{id}/                   详情（教师视角，含答案）
    PUT    /questions/{id}/                   全量更新
    PATCH  /questions/{id}/                   局部更新
    DELETE /questions/{id}/                   停用（软删除）
    POST   /questions/ai-generate/            AI 辅助出题（返回草稿，不落库）
    POST   /questions/batch/                  批量入库（教师确认草稿后）
    POST   /questions/{id}/ai-explain/        让 AI 补写解析
    POST   /questions/{id}/ai-grade/          主观题 AI 试评
    POST   /questions/import/                 批量导入（xlsx / csv）
    GET    /questions/export/                 导出（xlsx / csv）
    GET    /questions/import-template/        下载导入模板
    GET    /questions/statistics/             题库统计（题型/难度/知识点分布）

权限：全部要求教师或管理员。学生不直接接触题库，从根上避免答案泄漏。
"""

import logging

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.common.constants import Difficulty, QuestionType
from apps.common.exceptions import BusinessError
from apps.common.pagination import LargePagination, StandardPagination
from apps.common.permissions import IsTeacher
from apps.common.scoping import teacher_subject_scope
from apps.common.response import ApiResponse, success

from .constants import QuestionSource
from .filters import KnowledgePointFilter, QuestionFilter
from .importers import (
    build_export_rows,
    build_template_csv,
    parse_rows,
    read_upload,
    write_csv,
    write_xlsx,
)
from .models import KnowledgePoint, Question
from .serializers import (
    AIGenerateRequestSerializer,
    KnowledgePointSerializer,
    KnowledgePointTreeSerializer,
    QuestionBatchCreateSerializer,
    QuestionDetailSerializer,
    QuestionListSerializer,
    QuestionWriteSerializer,
)
from .services import QuestionAIService, QuestionService

logger = logging.getLogger("apps.questions.views")


# ==================================================================
# 知识点
# ==================================================================
@extend_schema(tags=["题库 · 知识点"])
class KnowledgePointViewSet(viewsets.ModelViewSet):
    """知识点的增删改查。"""

    queryset = KnowledgePoint.objects.all()
    serializer_class = KnowledgePointSerializer
    permission_classes = [IsTeacher]
    pagination_class = StandardPagination
    filterset_class = KnowledgePointFilter
    search_fields = ["name", "code", "description"]
    ordering_fields = ["order", "name", "created_at"]
    ordering = ["subject", "order", "id"]

    def get_queryset(self):
        queryset = (
            KnowledgePoint.objects.select_related("parent")
            .annotate(question_total=Count("questions", distinct=True))
            .ordered()
        )
        return teacher_subject_scope(self.request.user, queryset)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user if self.request.user.is_authenticated else None)

    def destroy(self, request, *args, **kwargs):
        """删除知识点：子树里还有在用题目就拒绝，否则连带子节点一起删。

        检查范围是**整棵子树**而不是只看自己：删父节点会级联删子节点
        （``KnowledgePoint.parent`` 是 CASCADE），若子节点上还挂着题目，
        那些题目就会静默失去知识点归属——不报错、不提示，事后也很难发现。
        """
        point = self.get_object()
        subtree_ids = point.descendant_ids()
        # 只算在用的题目：已停用的题目不该阻塞知识点整理
        has_questions = Question.objects.filter(
            knowledge_points__id__in=subtree_ids, is_active=True,
        ).exists()
        if has_questions:
            raise BusinessError(
                f"知识点「{point.name}」或其子知识点下还有题目，请先移走或删除这些题目",
                code=12030,
            )
        total = len(subtree_ids)
        point.delete()
        return success(message=f"知识点「{point.name}」已删除（含 {total - 1} 个子知识点）")

    # ------------------------------------------------------------------
    @extend_schema(
        summary="知识点树",
        parameters=[
            OpenApiParameter("subject", description="按学科筛选"),
            OpenApiParameter("include_inactive", type=bool, description="是否包含已停用节点"),
        ],
    )
    @action(detail=False, methods=["get"])
    def tree(self, request):
        queryset = teacher_subject_scope(request.user, KnowledgePoint.objects.all())
        subject = request.query_params.get("subject")
        if subject:
            queryset = queryset.filter(subject=subject)
        if request.query_params.get("include_inactive") not in ("true", "1"):
            queryset = queryset.filter(is_active=True)

        points = list(
            queryset.annotate(
                question_total=Count("questions", distinct=True),
                children_total=Count("children", distinct=True),
            ).ordered()
        )

        children_map = {}
        for point in points:
            children_map.setdefault(point.parent_id, []).append(point)

        context = {"request": request, "children_map": children_map}
        roots = children_map.get(None, [])
        return success(data=KnowledgePointTreeSerializer(roots, many=True, context=context).data)

    # ------------------------------------------------------------------
    @extend_schema(summary="知识点下的题目（含子知识点）")
    @action(detail=True, methods=["get"], url_path="questions")
    def questions(self, request, pk=None):
        point = self.get_object()
        queryset = (
            Question.objects.active()
            .filter(knowledge_points__id__in=point.descendant_ids())
            .with_relations()
            .distinct()
        )
        page = self.paginate_queryset(queryset)
        serializer = QuestionListSerializer(page, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data)


# ==================================================================
# 题目
# ==================================================================
@extend_schema(tags=["题库 · 题目"])
class QuestionViewSet(viewsets.ModelViewSet):
    """题目资源。"""

    queryset = Question.objects.all()
    permission_classes = [IsTeacher]
    pagination_class = StandardPagination
    filterset_class = QuestionFilter
    search_fields = ["stem", "analysis"]
    ordering_fields = ["created_at", "difficulty", "score", "usage_count", "correct_count"]
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = Question.objects.with_relations()
        return teacher_subject_scope(self.request.user, queryset)

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return QuestionWriteSerializer
        if self.action == "list":
            return QuestionListSerializer
        return QuestionDetailSerializer

    # ------------------------------------------------------------------
    # 创建 / 更新（选项与知识点交给 service 处理）
    # ------------------------------------------------------------------
    def create(self, request, *args, **kwargs):
        serializer = QuestionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        question = QuestionService.save_question(data=dict(serializer.validated_data), user=request.user)
        return ApiResponse(
            data=QuestionDetailSerializer(question, context={"request": request}).data,
            message="题目创建成功",
            status_code=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        question = self.get_object()
        serializer = QuestionWriteSerializer(question, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        question = QuestionService.save_question(
            data=dict(serializer.validated_data), user=request.user, instance=question
        )
        return success(
            data=QuestionDetailSerializer(question, context={"request": request}).data,
            message="题目已更新",
        )

    def destroy(self, request, *args, **kwargs):
        question = self.get_object()
        QuestionService.soft_delete(question)
        return success(message="题目已停用（不会影响已有试卷与成绩记录）")

    # ------------------------------------------------------------------
    # AI 辅助出题（草稿，不落库）
    # ------------------------------------------------------------------
    @extend_schema(summary="AI 辅助出题（返回草稿）", request=AIGenerateRequestSerializer)
    @action(detail=False, methods=["post"], url_path="ai-generate")
    def ai_generate(self, request):
        serializer = AIGenerateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = QuestionAIService.generate_drafts(
            user=request.user,
            subject=data.get("subject", ""),
            knowledge_points=data.get("knowledge_points") or [],
            question_types=data.get("question_types") or [QuestionType.SINGLE],
            difficulty=data.get("difficulty", Difficulty.MEDIUM),
            count=data.get("count", 5),
            source_text=data.get("source_text", ""),
            provider=data.get("provider") or None,
        )
        return success(
            data=result,
            message="草稿已生成，请确认后入库" + ("（本地模拟，未调用真实大模型）"
                                              if result["meta"].get("is_mock") else ""),
        )

    # ------------------------------------------------------------------
    # 批量入库
    # ------------------------------------------------------------------
    @extend_schema(summary="批量入库（确认 AI 草稿 / 导入结果）", request=QuestionBatchCreateSerializer)
    @action(detail=False, methods=["post"])
    def batch(self, request):
        serializer = QuestionBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        created, errors = QuestionService.bulk_create(
            items=data["items"],
            user=request.user,
            default_subject=data.get("subject", ""),
        )
        # 统一标记来源与审核状态
        Question.objects.filter(id__in=[q.id for q in created]).update(
            source=data.get("source", QuestionSource.AI),
            is_reviewed=data.get("is_reviewed", True),
        )

        return ApiResponse(
            data={
                "created_count": len(created),
                "created_ids": [q.id for q in created],
                "errors": errors,
            },
            message=f"成功入库 {len(created)} 道题" + (f"，{len(errors)} 道失败" if errors else ""),
            status_code=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------
    # AI 补解析
    # ------------------------------------------------------------------
    @extend_schema(summary="AI 生成 / 重写题目解析")
    @action(detail=True, methods=["post"], url_path="ai-explain", url_name="ai-explain")
    def ai_explain(self, request, pk=None):
        question = self.get_object()
        analysis = QuestionAIService.explain_question(
            question=question, user=request.user, provider=request.data.get("provider") or None
        )
        if request.data.get("save", True):
            question.analysis = analysis
            question.save(update_fields=["analysis", "updated_at"])
        return success(data={"analysis": analysis}, message="解析已生成")

    # ------------------------------------------------------------------
    # AI 试评（主观题）
    # ------------------------------------------------------------------
    @extend_schema(summary="主观题 AI 试评")
    @action(detail=True, methods=["post"], url_path="ai-grade", url_name="ai-grade")
    def ai_grade(self, request, pk=None):
        question = self.get_object()
        if not question.is_subjective:
            raise BusinessError("只有简答题 / 论述题才需要 AI 评分", code=12031)
        student_answer = request.data.get("student_answer", "")
        if not student_answer.strip():
            raise BusinessError("请提供学生答案", code=12032)

        result = QuestionAIService.grade_subjective(
            question=question, student_answer=student_answer,
            user=request.user, provider=request.data.get("provider") or None,
        )
        return success(data=result, message="评分完成")

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    @extend_schema(summary="批量导入题目（xlsx / csv）")
    @action(detail=False, methods=["post"], url_path="import",
            parser_classes=[MultiPartParser, FormParser, JSONParser])
    def import_questions(self, request):
        uploaded = request.FILES.get("file")
        if uploaded is None:
            raise BusinessError("请通过 file 字段上传 .xlsx 或 .csv 文件", code=12033)

        subject = request.data.get("subject", "")
        dry_run = str(request.data.get("dry_run", "")).lower() in ("true", "1", "yes")

        rows = read_upload(uploaded)
        if not rows:
            raise BusinessError("文件中没有可解析的数据行", code=12034)

        items, errors = parse_rows(rows, default_subject=subject)

        # 判重发生在入库之前：文件内重复 + 题库内已有（含已停用的题）
        unique_items, skipped = QuestionService.filter_duplicates(
            items, default_subject=subject
        )

        created_ids = []
        if unique_items and not dry_run:
            created, bulk_errors = QuestionService.bulk_create(
                items=unique_items, user=request.user, default_subject=subject
            )
            created_ids = [q.id for q in created]
            errors.extend(bulk_errors)

        if dry_run:
            message = "解析完成（未入库）"
        else:
            message = f"已导入 {len(created_ids)} 道题"
        if skipped:
            message += f"，跳过 {len(skipped)} 道重复"
        if errors:
            message += f"，{len(errors)} 行有问题"

        return success(
            data={
                "total_rows": len(rows),
                "parsed": len(items),
                "created_count": len(created_ids),
                "created_ids": created_ids,
                "dry_run": dry_run,
                "skipped_count": len(skipped),
                "skipped": skipped[:50],
                "errors": errors[:50],
                "error_count": len(errors),
            },
            message=message,
        )

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    @extend_schema(
        summary="导出题目",
        parameters=[
            OpenApiParameter("file_format", description="xlsx（默认）或 csv；"
                                                        "注意不要用 format，那是 DRF 的内容协商参数"),
        ],
    )
    @action(detail=False, methods=["get"])
    def export(self, request):
        queryset = self.filter_queryset(self.get_queryset())[:2000]  # 单次导出上限，防止拖库
        rows = build_export_rows(queryset)
        # 参数名故意不叫 format —— DRF 会用 ?format= 做内容协商，撞名会导致 404
        export_format = (request.query_params.get("file_format") or "xlsx").lower()

        if export_format == "csv":
            content, content_type, filename = write_csv(rows), "text/csv; charset=utf-8", "questions.csv"
        else:
            content = write_xlsx(rows)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            filename = "questions.xlsx"

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    # ------------------------------------------------------------------
    @extend_schema(summary="下载导入模板")
    @action(detail=False, methods=["get"], url_path="import-template")
    def import_template(self, request):
        response = HttpResponse(build_template_csv(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="question_import_template.csv"'
        return response

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    @extend_schema(summary="题库科目分类（各科目题数，供筛选下拉）")
    @action(detail=False, methods=["get"])
    def subjects(self, request):
        """当前教师可见范围内的科目分类。

        只统计启用中的题目；``get_queryset`` 已按学科隔离，所以学科教师
        只会拿到自己科目的那一项。
        """
        queryset = self.get_queryset().filter(is_active=True)
        rows = (
            queryset.values("subject")
            .annotate(count=Count("id"))
            .order_by("-count", "subject")
        )
        results = [
            {"subject": row["subject"] or "", "label": row["subject"] or "未分类",
             "count": row["count"]}
            for row in rows
        ]
        return success(data={"count": sum(r["count"] for r in results), "results": results})

    @extend_schema(summary="题库统计（供 ECharts 出题质量分析用）")
    @action(detail=False, methods=["get"])
    def statistics(self, request):
        queryset = self.filter_queryset(self.get_queryset())

        total = queryset.count()
        by_type = list(
            queryset.values("type").annotate(count=Count("id")).order_by("-count")
        )
        type_labels = dict(QuestionType.choices)
        by_type = [{"type": row["type"], "label": type_labels.get(row["type"], row["type"]),
                    "count": row["count"]} for row in by_type]

        by_difficulty = {int(level): 0 for level, _ in Difficulty.choices}
        for row in queryset.values("difficulty").annotate(count=Count("id")):
            by_difficulty[int(row["difficulty"])] = row["count"]

        by_source = list(
            queryset.values("source").annotate(count=Count("id")).order_by("-count")
        )
        source_labels = dict(QuestionSource.choices)
        by_source = [{"source": row["source"], "label": source_labels.get(row["source"], row["source"]),
                      "count": row["count"]} for row in by_source]

        by_knowledge_point = list(
            queryset.filter(knowledge_points__isnull=False)
            .values("knowledge_points__id", "knowledge_points__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:15]
        )
        by_knowledge_point = [
            {"id": row["knowledge_points__id"], "name": row["knowledge_points__name"],
             "count": row["count"]}
            for row in by_knowledge_point
        ]

        # 难度系数（加权平均，1~4）：组卷时用来评估题库难度结构
        difficulty_counts = {int(row["difficulty"]): row["count"]
                             for row in queryset.values("difficulty").annotate(count=Count("id"))}
        weighted = sum(level * count for level, count in difficulty_counts.items())
        total_for_avg = sum(difficulty_counts.values()) or 0

        return success(data={
            "total": total,
            "by_type": by_type,
            "by_difficulty": [
                {"level": level, "label": Difficulty(level).label, "count": count}
                for level, count in sorted(by_difficulty.items())
            ],
            "by_source": by_source,
            "by_knowledge_point": by_knowledge_point,
            "average_difficulty": round(weighted / total_for_avg, 2) if total_for_avg else 0.0,
            "reviewed_rate": round(
                queryset.filter(is_reviewed=True).count() / total, 4
            ) if total else 0.0,
            "subjective_count": queryset.filter(
                type__in=QuestionType.subjective_types()
            ).count(),
        })
