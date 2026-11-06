"""用户模块的过滤与搜索配置。"""

import django_filters
from django.db.models import Q

from apps.classroom.constants import ClassType
from apps.common.constants import Role

from .models import User


class UserFilter(django_filters.FilterSet):
    """用户列表过滤。配合 ViewSet 的 search_fields 一起用。"""

    role = django_filters.ChoiceFilter(choices=Role.choices)
    is_active = django_filters.BooleanFilter()
    # 学科筛选（U-1）：管理端用户列表按「学科」过滤，不加新接口、不改既有字段。
    # 学生的学科**不是** ``User.subject``（那是教师任教学科，学生恒为空），
    # 而是其所在学科班的 ``Classroom.subject``，故只能交给 ``filter_subject`` 两路合并。
    subject = django_filters.CharFilter(method="filter_subject", label="学科")
    created_after = django_filters.DateFilter(field_name="date_joined", lookup_expr="gte")
    created_before = django_filters.DateFilter(field_name="date_joined", lookup_expr="lte")
    keyword = django_filters.CharFilter(method="filter_keyword", label="关键字")
    # 班级筛选（3b）：学生「在哪个班」来自 M2M ``user.classrooms``，
    # 与 User.subject（教师任教学科）不是一回事，只能按班级维度反查。
    # 行政班与学科班都要支持，所以这里不限定 ``class_type``。
    classroom = django_filters.NumberFilter(method="filter_classroom", label="班级")

    class Meta:
        model = User
        fields = ["role", "is_active", "school"]

    def filter_keyword(self, queryset, name, value):
        return queryset.search(value)

    def filter_subject(self, queryset, name, value):
        """按学科过滤：教师看任教学科，学生看所在学科班的学科。

        学生没有 ``User.subject``（那是教师字段），真实学科挂在学科班上，
        所以必须两路 OR 起来，否则按学科永远筛不出学生。

        * ``distinct()`` 必须有：M2M join 会按班级行数把同一个人重复带出，
          与 ``classroom`` 条件组合时尤其明显；
        * 学生那一路带上 ``class_type=SUBJECT``：行政班的 ``subject`` 通常为空，
          但不能靠「通常」，否则哪天行政班填了学科就会串味。
        """
        value = (value or "").strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(subject__iexact=value)
            | Q(
                classrooms__subject__iexact=value,
                classrooms__class_type=ClassType.SUBJECT,
            )
        ).distinct()

    def filter_classroom(self, queryset, name, value):
        """按「班级」过滤，**行政班与学科班都支持**。

        两个坑：
          * M2M join 会让同一个用户按班级行数重复出现，必须 ``distinct()``；
          * 这里**不再判** ``class_type``：班级 id 全局唯一，能定位到唯一一个班，
            它本身是行政班还是学科班由调用方传参决定，过滤器只管「这个班的学生」。
            早先加 ``class_type=ADMIN`` 是为了只做行政班筛选，现在口径已放开。
        非法入参（``classroom=abc``）由 ``NumberFilter`` 校验拦下，该条件直接不生效。
        """
        if value is None:
            return queryset
        return queryset.filter(classrooms__id=value).distinct()
