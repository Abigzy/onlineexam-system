"""
试卷与组卷模块的页面视图。

和题库模块同一个套路：页面只做「壳」，数据全部 fetch RESTful 接口拿。
组卷工作台尤其如此——试算、生成、加题、调分、分析全都是现成的接口，
页面如果自己算一遍，就等于把组卷算法实现了两遍，早晚会不一致。
"""

import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.constants import Difficulty, QuestionType
from apps.common.mixins import TeacherPageMixin
from apps.questions.models import KnowledgePoint

from .models import Paper

logger = logging.getLogger("apps.papers.pages")


def _subjects() -> list:
    return list(
        KnowledgePoint.objects.exclude(subject="")
        .values_list("subject", flat=True)
        .distinct()
        .order_by("subject")
    )


@method_decorator(ensure_csrf_cookie, name="dispatch")
class PaperListPageView(TeacherPageMixin, TemplateView):
    """试卷列表：筛选、状态流转、进入工作台。"""

    template_name = "papers/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "试卷与组卷"
        context["active_menu"] = "papers"
        context["subjects"] = _subjects()
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class PaperComposePageView(TeacherPageMixin, TemplateView):
    """组卷工作台。

    带 ``pk`` 时是「编辑已有试卷」，不带时是「新建智能组卷」——
    两种模式共用一套界面，区别只在初始数据从哪来。
    """

    template_name = "papers/compose.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper_id = self.kwargs.get("pk")
        paper = Paper.objects.filter(pk=paper_id).first() if paper_id else None

        context["paper_id"] = paper.id if paper else None
        context["paper_title"] = paper.title if paper else ""
        context["paper_subject"] = paper.subject if paper else ""
        context["page_title"] = f"组卷工作台 · {paper.title}" if paper else "智能组卷"
        context["active_menu"] = "papers"
        context["subjects"] = _subjects()
        context["question_types"] = [
            {"value": code, "label": label} for code, label in QuestionType.choices
        ]
        context["difficulty_choices"] = [
            {"value": value, "label": label} for value, label in Difficulty.choices
        ]
        return context
