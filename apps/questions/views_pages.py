"""
题库模块的页面视图。

页面本身只做「壳」：渲染出结构，数据全部通过 fetch 调 RESTful 接口拿。
好处是同一套业务逻辑只写一次（在 API 里），页面与第三方接入共用。
"""

import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.constants import QuestionType
from apps.common.mixins import TeacherPageMixin

from .models import KnowledgePoint, Question
logger = logging.getLogger("apps.questions.pages")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class QuestionBankPageView(TeacherPageMixin, TemplateView):
    """题库列表：筛选 + 分页 + 批量操作。"""

    template_name = "questions/bank.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "题库管理"
        context["summary"] = {
            "total": Question.objects.count(),
            "active": Question.objects.active().count(),
            "ai_generated": Question.objects.ai_generated().count(),
            "knowledge_points": KnowledgePoint.objects.count(),
        }
        context["active_menu"] = "question-bank"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class QuestionEditorPageView(TeacherPageMixin, TemplateView):
    """新建 / 编辑题目（按题型动态渲染表单）。"""

    template_name = "questions/editor.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        question_id = self.kwargs.get("pk")
        context["question_id"] = question_id
        context["page_title"] = "编辑题目" if question_id else "新建题目"
        context["active_menu"] = "question-bank"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class AIGeneratePageView(TeacherPageMixin, TemplateView):
    """AI 辅助出题工作台：生成草稿 → 教师编辑确认 → 批量入库。"""

    template_name = "questions/ai_generate.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "AI 智能出题"
        context["active_menu"] = "ai-generate"
        context["question_types"] = QuestionType.choices
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class KnowledgePointPageView(TeacherPageMixin, TemplateView):
    """知识点树管理。"""

    template_name = "questions/knowledge_points.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "知识点管理"
        context["active_menu"] = "knowledge-points"
        context["subjects"] = list(
            KnowledgePoint.objects.exclude(subject="")
            .values_list("subject", flat=True)
            .distinct()
            .order_by("subject")
        )
        return context
