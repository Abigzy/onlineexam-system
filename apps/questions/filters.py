"""题库模块的过滤配置。"""

import django_filters

from apps.common.constants import Difficulty, QuestionType

from .constants import QuestionSource
from .models import KnowledgePoint, Question


class KnowledgePointFilter(django_filters.FilterSet):
    """知识点筛选。"""

    subject = django_filters.CharFilter(lookup_expr="iexact")
    is_active = django_filters.BooleanFilter()
    is_root = django_filters.BooleanFilter(method="filter_is_root", label="只看根节点")
    parent = django_filters.NumberFilter(field_name="parent_id")
    keyword = django_filters.CharFilter(method="filter_keyword", label="关键字")

    class Meta:
        model = KnowledgePoint
        fields = ["subject", "is_active", "parent"]

    def filter_is_root(self, queryset, name, value):
        return queryset.filter(parent__isnull=True) if value else queryset.filter(parent__isnull=False)

    def filter_keyword(self, queryset, name, value):
        return queryset.search(value)


class QuestionFilter(django_filters.FilterSet):
    """题目筛选。

    ``knowledge_point`` 支持「含子知识点」：选中「树」时，其下「二叉树」「红黑树」的题目也会被命中。
    """

    type = django_filters.ChoiceFilter(choices=QuestionType.choices)
    difficulty = django_filters.ChoiceFilter(choices=Difficulty.choices)
    difficulty_gte = django_filters.NumberFilter(field_name="difficulty", lookup_expr="gte")
    difficulty_lte = django_filters.NumberFilter(field_name="difficulty", lookup_expr="lte")
    subject = django_filters.CharFilter(lookup_expr="iexact")
    source = django_filters.ChoiceFilter(choices=QuestionSource.choices)
    is_active = django_filters.BooleanFilter()
    is_reviewed = django_filters.BooleanFilter()
    created_by = django_filters.NumberFilter(field_name="created_by_id")

    knowledge_point = django_filters.NumberFilter(method="filter_knowledge_point",
                                                  label="知识点（含子知识点）")
    knowledge_point_exact = django_filters.NumberFilter(field_name="knowledge_points__id",
                                                        label="知识点（精确匹配）")
    has_ai = django_filters.BooleanFilter(method="filter_has_ai", label="只看 AI 生成")
    keyword = django_filters.CharFilter(method="filter_keyword", label="关键字")

    class Meta:
        model = Question
        fields = ["type", "difficulty", "subject", "source", "is_active", "is_reviewed"]

    def filter_knowledge_point(self, queryset, name, value):
        point = KnowledgePoint.objects.filter(pk=value).first()
        if point is None:
            return queryset.none()
        return queryset.filter(knowledge_points__id__in=point.descendant_ids()).distinct()

    def filter_has_ai(self, queryset, name, value):
        return queryset.ai_generated() if value else queryset.exclude(source=QuestionSource.AI)

    def filter_keyword(self, queryset, name, value):
        return queryset.search(value)
