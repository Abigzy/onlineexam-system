"""学情分析模块后台。"""

from django.contrib import admin

from .models import StudentAnalysisReport


@admin.register(StudentAnalysisReport)
class StudentAnalysisReportAdmin(admin.ModelAdmin):
    list_display = ["id", "student", "scope_key", "overall_level", "weak_point_count",
                    "knowledge_point_count", "sample_size", "provider", "is_mock",
                    "generated_at"]
    list_filter = ["is_mock", "provider", "scope_key", "overall_level"]
    search_fields = ["summary", "student__username", "student__real_name", "student__student_no"]
    raw_id_fields = ["student"]
    date_hierarchy = "generated_at"
    readonly_fields = ["created_at", "updated_at"]
