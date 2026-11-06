"""作弊行为检测模块后台。"""

from django.contrib import admin

from .models import ExamBehaviorLog


@admin.register(ExamBehaviorLog)
class ExamBehaviorLogAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "student", "event_type", "count",
                    "duration_seconds", "occurred_at", "ip_address"]
    list_filter = ["event_type", "occurred_at"]
    search_fields = ["student__username", "student__real_name", "student__student_no",
                     "ip_address", "user_agent"]
    raw_id_fields = ["attempt", "exam", "student", "question"]
    date_hierarchy = "occurred_at"
    readonly_fields = ["created_at", "updated_at"]
