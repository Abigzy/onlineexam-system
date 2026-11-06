"""班级模块 Django 后台配置。"""

from django.contrib import admin

from .models import Classroom


@admin.register(Classroom)
class ClassroomAdmin(admin.ModelAdmin):
    list_display = ("name", "grade_year", "subject", "head_teacher", "is_active", "created_at")
    list_filter = ("is_active", "subject")
    search_fields = ("name", "grade_year", "subject")
    filter_horizontal = ("teachers", "students")
    raw_id_fields = ("head_teacher", "created_by")
    list_select_related = ("head_teacher", "created_by")
