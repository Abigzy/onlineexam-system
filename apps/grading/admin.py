"""判分与成绩模块后台。"""

from django.contrib import admin

from .models import GradingFeedback


@admin.register(GradingFeedback)
class GradingFeedbackAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "answer", "question_type", "source", "action",
                    "ai_score", "original_score", "final_score", "delta", "reviewer",
                    "created_at"]
    list_filter = ["action", "source", "question_type"]
    search_fields = ["comment", "attempt__student__username", "attempt__exam__title"]
    raw_id_fields = ["answer", "attempt", "reviewer"]
    date_hierarchy = "created_at"
    readonly_fields = ["created_at", "updated_at"]
