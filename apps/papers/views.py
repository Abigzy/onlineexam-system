"""
试卷与组卷接口。

路由总览（`/api/v1/` 下）：

    试卷
    GET    /papers/                        列表（状态/学科/组卷方式/分值区间等筛选）
    POST   /papers/                        新建（可带 question_ids 顺手塞题）
    GET    /papers/{id}/                   详情（卷头 + 大题 + 题目，含答案）
    PUT    /papers/{id}/                   全量更新卷头
    PATCH  /papers/{id}/                   局部更新卷头
    DELETE /papers/{id}/                   删除（已发布的不能删，只能归档）
    POST   /papers/{id}/questions/         加题
    POST   /papers/{id}/remove-questions/  移出题目
    POST   /papers/{id}/scores/            批量调分
    POST   /papers/{id}/reorder/           调整题序
    POST   /papers/{id}/clone/             复制（做 A/B 卷）
    POST   /papers/{id}/status/            发布 / 退回草稿 / 归档
    GET    /papers/{id}/preview/           卷面预览（按大题分组，含大题说明与每题分值）
    GET    /papers/{id}/analysis/          组卷质量分析（ECharts 数据 + 预警）
    POST   /papers/{id}/ai-notes/          生成 / 重写组卷说明
    GET    /papers/{id}/export/            导出卷面（file_format=html|md）

    组卷
    POST   /papers/ai-compose/             智能组卷（dry_run=true 时只试算不落库）
    GET    /papers/pool/                   题库家底（各题型 × 各难度库存矩阵）
    GET    /papers/statistics/             试卷统计总览

权限：全部要求教师或管理员。学生不接触试卷管理接口——考试下发走模块四的专用链路。
"""

import logging

from django.db.models import Count
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action

from apps.common.exceptions import BusinessError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacher
from apps.common.response import created, success
from apps.common.scoping import teacher_subject, teacher_subject_scope
from apps.questions.models import Question

from .ai_services import PaperAIService
from .composer import PaperComposeService
from .constants import PaperGenerationMode, PaperStatus
from .exporters import build_html, build_markdown
from .filters import PaperFilter
from .models import Paper, PaperQuestion
from .serializers import (
    AddQuestionsSerializer,
    BatchScoreSerializer,
    CloneSerializer,
    ComposeRequestSerializer,
    ExplainPaperSerializer,
    PaperDetailSerializer,
    PaperListSerializer,
    PaperQuestionSerializer,
    PaperWriteSerializer,
    PoolQuerySerializer,
    RandomComposeRequestSerializer,
    RemoveQuestionsSerializer,
    ReorderSerializer,
    StatusChangeSerializer,
)
from .services import PaperAnalysisService, PaperService

logger = logging.getLogger("apps.papers.views")


def _quote_filename(name: str) -> str:
    """中文文件名要走 RFC 5987，直接塞进 header 会被当成 latin-1 报错。"""
    from urllib.parse import quote

    return f"attachment; filename*=UTF-8''{quote(name)}"


@extend_schema(tags=["试卷 · 组卷"])
class PaperViewSet(viewsets.ModelViewSet):
    """试卷的增删改查与组卷编排。"""

    queryset = Paper.objects.all()
    serializer_class = PaperListSerializer
    permission_classes = [IsTeacher]
    pagination_class = StandardPagination
    filterset_class = PaperFilter
    search_fields = ["title", "subject", "description"]
    ordering_fields = ["created_at", "updated_at", "total_score", "question_count", "published_at"]
    ordering = ["-created_at"]

    # ------------------------------------------------------------------
    def get_queryset(self):
        queryset = Paper.objects.with_relations()
        queryset = teacher_subject_scope(self.request.user, queryset)
        # 归档 = 软下线。默认列表不显示，但显式传 status=archived 时要能看到。
        if self.action == "list" and not self.request.query_params.get("status"):
            queryset = queryset.active()
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return PaperListSerializer
        if self.action in ("create", "update", "partial_update"):
            return PaperWriteSerializer
        return PaperDetailSerializer

    def get_serializer_context(self):
        return {"request": self.request, "format": self.kwargs.get("format")}

    def _reload(self, paper: Paper) -> Paper:
        """写操作之后重新取一遍，避免带着失效的 prefetch 缓存去序列化。"""
        return Paper.objects.with_relations().get(pk=paper.pk)

    # ------------------------------------------------------------------
    # 基础 CRUD
    # ------------------------------------------------------------------
    @extend_schema(summary="新建试卷", request=PaperWriteSerializer, responses=PaperDetailSerializer)
    def create(self, request, *args, **kwargs):
        serializer = PaperWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        paper = PaperService.create(data=serializer.validated_data, user=request.user)
        data = PaperDetailSerializer(self._reload(paper), context=self.get_serializer_context()).data
        return created(data=data, message=f"试卷《{paper.title}》已创建")

    @extend_schema(summary="修改试卷卷头", request=PaperWriteSerializer, responses=PaperDetailSerializer)
    def update(self, request, *args, **kwargs):
        paper = self.get_object()
        serializer = PaperWriteSerializer(data=request.data, partial=kwargs.get("partial", False))
        serializer.is_valid(raise_exception=True)
        paper = PaperService.update(paper, data=serializer.validated_data)
        data = PaperDetailSerializer(self._reload(paper), context=self.get_serializer_context()).data
        return success(data=data, message="已保存")

    @extend_schema(summary="删除试卷")
    def destroy(self, request, *args, **kwargs):
        paper = self.get_object()
        title = paper.title
        PaperService.delete(paper)
        return success(message=f"试卷《{title}》已删除")

    # ------------------------------------------------------------------
    # 题目增删改
    # ------------------------------------------------------------------
    @extend_schema(summary="往试卷里加题", request=AddQuestionsSerializer)
    @action(detail=True, methods=["post"], url_path="questions")
    def add_questions(self, request, pk=None):
        paper = self.get_object()
        serializer = AddQuestionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payload = serializer.validated_data
        # 保持传入顺序：教师勾选的顺序就是期望的题序
        pool = {
            question.id: question
            for question in Question.objects.filter(id__in=payload["question_ids"], is_active=True)
        }
        questions = [pool[qid] for qid in payload["question_ids"] if qid in pool]

        result = PaperService.add_questions(
            paper=paper, questions=questions,
            score=payload.get("score"), strict=payload["strict"],
        )

        message = f"已添加 {result['added_count']} 道题"
        if result["duplicated_count"]:
            message += f"，跳过 {result['duplicated_count']} 道已在卷内的题"
        if result["invalid_count"]:
            message += f"，忽略 {result['invalid_count']} 道已停用的题"

        paper = self._reload(paper)
        return success(
            data={
                "paper": PaperDetailSerializer(paper, context=self.get_serializer_context()).data,
                "added_count": result["added_count"],
                "duplicated_count": result["duplicated_count"],
                "invalid_count": result["invalid_count"],
                "total_score": paper.total_score_float,
                "question_count": paper.question_count,
            },
            message=message,
        )

    @extend_schema(summary="把题目移出试卷", request=RemoveQuestionsSerializer)
    @action(detail=True, methods=["post"], url_path="remove-questions")
    def remove_questions(self, request, pk=None):
        paper = self.get_object()
        serializer = RemoveQuestionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        removed = PaperService.remove_questions(paper=paper, paper_question_ids=serializer.validated_data["ids"])
        paper = self._reload(paper)
        return success(
            data={
                "removed_count": removed,
                "total_score": paper.total_score_float,
                "question_count": paper.question_count,
            },
            message=f"已移出 {removed} 道题",
        )

    @extend_schema(summary="批量调分", request=BatchScoreSerializer)
    @action(detail=True, methods=["post"], url_path="scores")
    def set_scores(self, request, pk=None):
        paper = self.get_object()
        serializer = BatchScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated = PaperService.batch_set_score(paper=paper, items=serializer.validated_data["items"])
        paper = self._reload(paper)
        return success(
            data={
                "updated_count": updated,
                "total_score": paper.total_score_float,
                "question_count": paper.question_count,
            },
            message=f"已调整 {updated} 道题的分值" if updated else "分值没有变化",
        )

    @extend_schema(summary="调整题目顺序", request=ReorderSerializer)
    @action(detail=True, methods=["post"])
    def reorder(self, request, pk=None):
        paper = self.get_object()
        serializer = ReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        PaperService.reorder(paper=paper, ordered_ids=serializer.validated_data["ordered_ids"])
        return success(message="题序已更新")

    # ------------------------------------------------------------------
    # 复制 / 状态
    # ------------------------------------------------------------------
    @extend_schema(summary="复制试卷", request=CloneSerializer)
    @action(detail=True, methods=["post"])
    def clone(self, request, pk=None):
        paper = self.get_object()
        serializer = CloneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_paper = PaperService.clone(
            paper, user=request.user, title=serializer.validated_data.get("title") or None,
        )
        data = PaperDetailSerializer(
            self._reload(new_paper), context=self.get_serializer_context(),
        ).data
        return created(data=data, message=f"已复制为《{new_paper.title}》")

    @extend_schema(summary="变更试卷状态（发布 / 退回草稿 / 归档）", request=StatusChangeSerializer)
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        paper = self.get_object()
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        paper = PaperService.change_status(paper, serializer.validated_data["status"])
        return success(
            data=PaperListSerializer(self._reload(paper)).data,
            message=f"已切换到「{paper.get_status_display()}」",
        )

    # ------------------------------------------------------------------
    # 预览 / 分析
    # ------------------------------------------------------------------
    @extend_schema(summary="卷面预览（按大题分组）")
    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        paper = self.get_object()
        context = self.get_serializer_context()

        by_section = {}
        orphans = []
        for item in sorted(paper.paper_questions.all(), key=lambda x: (x.order, x.id)):
            if item.section_id:
                by_section.setdefault(item.section_id, []).append(item)
            else:
                orphans.append(item)

        sections = []
        for section in sorted(paper.sections.all(), key=lambda s: (s.order, s.id)):
            rows = by_section.get(section.id, [])
            sections.append({
                "id": section.id,
                "title": section.title,
                "label": section.section_label(),
                "question_type": section.question_type,
                "description": section.description,
                "order": section.order,
                "score_per_question": (
                    float(section.score_per_question) if section.score_per_question else None
                ),
                "question_count": len(rows),
                "subtotal": float(section.subtotal),
                "questions": PaperQuestionSerializer(rows, many=True, context=context).data,
            })

        return success(data={
            "paper": PaperListSerializer(paper, context=context).data,
            "sections": sections,
            "orphan_questions": PaperQuestionSerializer(orphans, many=True, context=context).data,
            "total_score": paper.total_score_float,
            "question_count": paper.question_count,
        })

    @extend_schema(
        summary="组卷质量分析",
        parameters=[
            OpenApiParameter(
                "planned", type=int, many=True,
                description="计划覆盖的知识点 id，用于计算覆盖率（可重复传）",
            ),
        ],
    )
    @action(detail=True, methods=["get"])
    def analysis(self, request, pk=None):
        paper = self.get_object()
        planned = request.query_params.getlist("planned") or None
        return success(data=PaperAnalysisService.full_report(paper, planned_ids=planned))

    @extend_schema(summary="生成 / 重写组卷说明", request=ExplainPaperSerializer)
    @action(detail=True, methods=["post"], url_path="ai-notes")
    def ai_notes(self, request, pk=None):
        paper = self.get_object()
        serializer = ExplainPaperSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        result = PaperAIService.generate_notes(
            paper, user=request.user,
            provider=payload.get("provider") or None, force=payload["force"],
        )
        return success(
            data=result,
            message="已返回缓存的组卷说明" if result.get("cached") else "组卷说明已生成",
        )

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    @extend_schema(
        summary="导出卷面",
        parameters=[
            OpenApiParameter("file_format", description="html（可打印）或 md（Markdown）"),
            OpenApiParameter("with_answer", type=bool, description="是否附加参考答案页"),
            OpenApiParameter("with_analysis", type=bool, description="答案页是否带解析"),
        ],
    )
    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        paper = self.get_object()
        # 参数名刻意避开 ?format= —— 那是 DRF 的内容协商参数，撞名会导致 404
        file_format = (request.query_params.get("file_format") or "html").lower()
        with_answer = request.query_params.get("with_answer") in ("true", "1")
        with_analysis = request.query_params.get("with_analysis", "true") not in ("false", "0")

        if file_format in ("md", "markdown"):
            content = build_markdown(paper, with_answer=with_answer, with_analysis=with_analysis)
            response = HttpResponse(content, content_type="text/plain; charset=utf-8")
            filename = f"{paper.title}.md"
        else:
            content = build_html(paper, with_answer=with_answer, with_analysis=with_analysis)
            response = HttpResponse(content, content_type="text/html; charset=utf-8")
            filename = f"{paper.title}.html"

        response["Content-Disposition"] = _quote_filename(filename)
        return response

    # ------------------------------------------------------------------
    # 智能组卷
    # ------------------------------------------------------------------
    @extend_schema(summary="智能组卷（dry_run=true 时只试算）", request=ComposeRequestSerializer)
    @action(detail=False, methods=["post"], url_path="ai-compose")
    def ai_compose(self, request):
        serializer = ComposeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        # 有任教学科的教师，组卷学科被强制锁定为自己的学科（不能跨学科抽题）
        forced_subject = teacher_subject(request.user)
        common = {
            "subject": forced_subject or (payload.get("subject") or ""),
            "knowledge_point_ids": payload.get("knowledge_point_ids"),
            "blueprint": payload.get("blueprint"),
            "target_difficulty": payload["target_difficulty"],
            "exclude_ids": payload.get("exclude_ids"),
            "seed": payload.get("seed"),
        }

        # 试算：教师先看清「能不能组出来、什么难度」，再决定要不要真生成
        if payload["dry_run"]:
            plan = PaperComposeService.preview(**common)
            return success(data=plan["report"], message="试算完成（未落库）")

        result = PaperComposeService.compose(
            user=request.user,
            title=payload.get("title") or "",
            duration_minutes=payload["duration_minutes"],
            pass_score=payload.get("pass_score"),
            **common,
        )
        paper = result["paper"]
        data = {
            "paper": PaperDetailSerializer(
                self._reload(paper), context=self.get_serializer_context(),
            ).data,
            "report": result["report"],
        }

        notes_error = ""
        if payload.get("generate_notes"):
            try:
                data["notes"] = PaperAIService.generate_notes(paper, user=request.user)
                data["paper"] = PaperDetailSerializer(
                    self._reload(paper), context=self.get_serializer_context(),
                ).data
            except BusinessError as exc:
                # 说明生成失败不该让整次组卷失败——题已经组好了，说明可以稍后补
                notes_error = str(exc)
                logger.warning("组卷说明生成失败（不影响组卷结果）：%s", exc)

        message = f"已生成《{paper.title}》，共 {paper.question_count} 题 / {paper.total_score_float:g} 分"
        if notes_error:
            message += f"（组卷说明生成失败：{notes_error}）"
        if result["report"]["shortage"]:
            message += f"，有 {len(result['report']['shortage'])} 个题型题量不足"

        return created(data=data, message=message)

    @extend_schema(
        summary="随机组卷（按所选知识点，蓝图自动生成，不调 AI）",
        request=RandomComposeRequestSerializer,
    )
    @action(detail=False, methods=["post"], url_path="random-compose")
    def random_compose(self, request):
        serializer = RandomComposeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        forced_subject = teacher_subject(request.user)
        subject = forced_subject or (payload.get("subject") or "")

        # 蓝图由题库库存自动生成，教师不用配题型题数
        blueprint = PaperComposeService.auto_blueprint(
            subject=subject,
            knowledge_point_ids=payload.get("knowledge_point_ids"),
            total_count=payload["total_count"],
        )
        if not blueprint:
            raise BusinessError(
                "所选知识点范围内没有可用题目，请放宽知识点选择或调整学科",
                code=13010,
            )

        common = {
            "subject": subject,
            "knowledge_point_ids": payload.get("knowledge_point_ids"),
            "blueprint": blueprint,
            "target_difficulty": payload["target_difficulty"],
            "exclude_ids": payload.get("exclude_ids"),
            "seed": payload.get("seed"),
        }

        if payload["dry_run"]:
            plan = PaperComposeService.preview(**common)
            return success(data=plan["report"], message="试算完成（未落库）")

        # 随机组卷不调 AI 写说明：generate_notes 不在此链路，说明可稍后手动补
        result = PaperComposeService.compose(
            user=request.user,
            title=payload.get("title") or "",
            duration_minutes=payload["duration_minutes"],
            pass_score=payload.get("pass_score"),
            generation_mode=PaperGenerationMode.RANDOM,
            title_label="随机组卷",
            **common,
        )
        paper = result["paper"]
        data = {
            "paper": PaperDetailSerializer(
                self._reload(paper), context=self.get_serializer_context(),
            ).data,
            "report": result["report"],
        }
        message = f"已随机生成《{paper.title}》，共 {paper.question_count} 题 / {paper.total_score_float:g} 分"
        if result["report"]["shortage"]:
            message += f"，有 {len(result['report']['shortage'])} 个题型题量不足"

        return created(data=data, message=message)

    @extend_schema(
        summary="题库家底（各题型 × 各难度库存）",
        parameters=[
            OpenApiParameter("subject", description="按学科筛选"),
            OpenApiParameter("knowledge_point_ids", type=int, many=True, description="按知识点筛选"),
        ],
    )
    @action(detail=False, methods=["get"])
    def pool(self, request):
        serializer = PoolQuerySerializer(data={
            "subject": request.query_params.get("subject", ""),
            "knowledge_point_ids": request.query_params.getlist("knowledge_point_ids"),
        })
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        forced_subject = teacher_subject(request.user)
        return success(data=PaperComposeService.describe_pool(
            subject=forced_subject or (payload.get("subject") or ""),
            knowledge_point_ids=payload.get("knowledge_point_ids"),
        ))

    # ------------------------------------------------------------------
    @extend_schema(summary="试卷统计总览")
    @action(detail=False, methods=["get"])
    def statistics(self, request):
        queryset = Paper.objects.all()
        total = queryset.count()

        by_status = [
            {"value": code, "label": label, "count": queryset.filter(status=code).count()}
            for code, label in PaperStatus.choices
        ]
        by_mode = [
            {"value": code, "label": label, "count": queryset.filter(generation_mode=code).count()}
            for code, label in PaperGenerationMode.choices
        ]

        usage = PaperQuestion.objects.aggregate(total=Count("id"))["total"] or 0
        avg_questions = round(usage / total, 1) if total else 0

        return success(data={
            "total": total,
            "by_status": by_status,
            "by_mode": by_mode,
            "question_usage_total": usage,
            "avg_question_count": avg_questions,
            "recent": PaperListSerializer(
                queryset.with_relations().order_by("-created_at")[:5], many=True,
                context=self.get_serializer_context(),
            ).data,
        })
