"""试卷模块的过滤配置。"""

import django_filters

from .constants import PaperGenerationMode, PaperStatus
from .models import Paper


class PaperFilter(django_filters.FilterSet):
    """试卷筛选。

    注意 ``status`` 默认**不过滤归档卷**的情况交给视图层处理（见 ``PaperViewSet``）：
    归档是「软下线」，默认列表不该看到，但显式传 ``status=archived`` 时要能看到。
    """

    status = django_filters.ChoiceFilter(choices=PaperStatus.choices)
    generation_mode = django_filters.ChoiceFilter(choices=PaperGenerationMode.choices)
    subject = django_filters.CharFilter(lookup_expr="iexact")
    title = django_filters.CharFilter(lookup_expr="icontains")
    keyword = django_filters.CharFilter(method="filter_keyword", label="关键字")
    created_by = django_filters.NumberFilter(field_name="created_by_id")
    is_published = django_filters.BooleanFilter(method="filter_is_published")
    min_score = django_filters.NumberFilter(field_name="total_score", lookup_expr="gte")
    max_score = django_filters.NumberFilter(field_name="total_score", lookup_expr="lte")
    duration_gte = django_filters.NumberFilter(field_name="duration_minutes", lookup_expr="gte")
    duration_lte = django_filters.NumberFilter(field_name="duration_minutes", lookup_expr="lte")
    created_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Paper
        fields = ["status", "subject", "generation_mode", "created_by"]

    def filter_keyword(self, queryset, name, value):
        return queryset.search(value)

    def filter_is_published(self, queryset, name, value):
        if value:
            return queryset.published()
        return queryset.exclude(status=PaperStatus.PUBLISHED)


__all__ = ["PaperFilter"]
