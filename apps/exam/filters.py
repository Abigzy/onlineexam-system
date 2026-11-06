"""考试模块筛选器。"""

import django_filters
from django.db.models import Q
from django.utils import timezone

from apps.common.constants import AttemptStatus, ExamStatus

from .models import Exam, ExamAttempt


class ExamFilter(django_filters.FilterSet):
    """考试列表筛选。"""

    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    subject = django_filters.CharFilter(field_name="subject", lookup_expr="exact")
    paper = django_filters.NumberFilter(field_name="paper_id")
    created_by = django_filters.NumberFilter(field_name="created_by_id")
    open_to_all = django_filters.BooleanFilter(field_name="open_to_all")
    start_from = django_filters.IsoDateTimeFilter(field_name="start_time", lookup_expr="gte")
    start_to = django_filters.IsoDateTimeFilter(field_name="start_time", lookup_expr="lte")
    is_open_now = django_filters.BooleanFilter(
        method="filter_is_open_now", help_text="只看此刻可以开考的（已发布 + 在时间窗内）",
    )

    class Meta:
        model = Exam
        fields = ["status", "subject", "paper", "created_by", "open_to_all"]

    def filter_is_open_now(self, queryset, name, value):
        now = timezone.now()
        within_window = Q(start_time__isnull=True) | Q(start_time__lte=now)
        not_ended = Q(end_time__isnull=True) | Q(end_time__gte=now)
        matched = queryset.filter(
            status__in=[ExamStatus.PUBLISHED, ExamStatus.ONGOING]
        ).filter(within_window & not_ended)
        if value:
            return matched
        return queryset.exclude(pk__in=matched.values("pk"))


class ExamAttemptFilter(django_filters.FilterSet):
    """作答记录筛选。"""

    exam = django_filters.NumberFilter(field_name="exam_id")
    student = django_filters.NumberFilter(field_name="student_id")
    status = django_filters.CharFilter(field_name="status", lookup_expr="exact")
    is_timeout = django_filters.BooleanFilter(field_name="is_timeout")
    only_in_progress = django_filters.BooleanFilter(
        method="filter_only_in_progress", help_text="只看还在答题中的",
    )
    suspicious = django_filters.BooleanFilter(
        method="filter_suspicious", help_text="只看切屏次数超过本场考试阈值的",
    )

    class Meta:
        model = ExamAttempt
        fields = ["exam", "student", "status", "is_timeout"]

    def filter_only_in_progress(self, queryset, name, value):
        if value:
            return queryset.filter(status=AttemptStatus.IN_PROGRESS)
        return queryset

    def filter_suspicious(self, queryset, name, value):
        """跨表比较：作答的切屏次数 > 所属考试设定的阈值。

        用 F 表达式在数据库里比，而不是先取出来在 Python 里筛——
        教师看板一次可能要看几百条记录。
        """
        from django.db.models import F

        matched = queryset.filter(
            exam__detect_switch=True, switch_count__gt=F("exam__max_switch_count"),
        )
        if value:
            return matched
        return queryset.exclude(pk__in=matched.values("pk"))


__all__ = ["ExamAttemptFilter", "ExamFilter"]
