"""考试模块后台。"""

from django.contrib import admin

from .models import Answer, Exam, ExamAttempt


class ExamAttemptInline(admin.TabularInline):
    model = ExamAttempt
    extra = 0
    fields = ["student", "attempt_no", "status", "started_at", "submitted_at",
              "score", "switch_count", "paste_count"]
    readonly_fields = ["started_at", "submitted_at"]
    show_change_link = True


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "subject", "paper", "status", "start_time", "end_time",
                    "duration_minutes", "max_attempts", "open_to_all", "created_by"]
    list_filter = ["status", "open_to_all", "detect_switch", "detect_paste", "subject"]
    search_fields = ["title", "description", "paper__title"]
    autocomplete_fields = ["paper"]
    raw_id_fields = ["created_by"]
    filter_horizontal = ["participants"]
    date_hierarchy = "created_at"
    inlines = [ExamAttemptInline]
    readonly_fields = ["published_at", "created_at", "updated_at"]


class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 0
    fields = ["paper_question", "selected_labels", "answer_text", "is_answered",
              "is_correct", "score"]
    readonly_fields = ["paper_question"]
    max_num = 0


@admin.register(ExamAttempt)
class ExamAttemptAdmin(admin.ModelAdmin):
    list_display = ["id", "exam", "student", "attempt_no", "status", "score",
                    "answered_count", "switch_count", "paste_count", "is_timeout",
                    "started_at", "submitted_at"]
    list_filter = ["status", "is_timeout", "exam"]
    search_fields = ["student__username", "student__real_name", "student__student_no", "exam__title"]
    raw_id_fields = ["student", "exam"]
    date_hierarchy = "started_at"
    inlines = [AnswerInline]
    readonly_fields = ["started_at", "deadline_at", "submitted_at", "graded_at",
                       "time_spent_seconds", "created_at", "updated_at"]


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "paper_question", "selected_labels", "is_answered",
                    "is_correct", "score", "order"]
    list_filter = ["is_answered", "is_correct", "is_flagged"]
    search_fields = ["answer_text", "selected_labels", "attempt__student__username"]
    raw_id_fields = ["attempt", "paper_question"]
