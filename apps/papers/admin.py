"""试卷模块的 Django Admin 配置。

Admin 在这里的定位是**运维兜底**：接口和页面都做不了的事（比如批量修数据、
查某个冗余字段对不对），从这里下手最快。
"""

from django.contrib import admin

from .models import Paper, PaperQuestion, PaperSection


class PaperSectionInline(admin.TabularInline):
    model = PaperSection
    extra = 0
    fields = ["order", "title", "question_type", "score_per_question", "description"]
    ordering = ["order"]


class PaperQuestionInline(admin.TabularInline):
    model = PaperQuestion
    extra = 0
    fields = ["order", "question", "section", "score", "question_type", "difficulty"]
    raw_id_fields = ["question"]
    ordering = ["order"]
    show_change_link = True


@admin.register(Paper)
class PaperAdmin(admin.ModelAdmin):
    list_display = [
        "id", "title", "subject", "status", "generation_mode",
        "question_count", "total_score", "duration_minutes",
        "target_difficulty", "created_by", "created_at",
    ]
    list_filter = ["status", "generation_mode", "subject", "created_at"]
    search_fields = ["title", "subject", "description"]
    readonly_fields = ["total_score", "question_count", "created_at", "updated_at", "published_at"]
    raw_id_fields = ["created_by"]
    date_hierarchy = "created_at"
    inlines = [PaperSectionInline, PaperQuestionInline]
    ordering = ["-created_at"]

    fieldsets = (
        ("卷头", {
            "fields": ("title", "subject", "description", "status", "generation_mode"),
        }),
        ("考试参数", {
            "fields": ("duration_minutes", "total_score", "pass_score",
                       "question_count", "target_difficulty"),
        }),
        ("组卷说明（AI）", {
            "fields": ("ai_notes", "ai_provider", "ai_model"),
            "classes": ("collapse",),
        }),
        ("其他", {
            "fields": ("created_by", "published_at", "created_at", "updated_at"),
        }),
    )


@admin.register(PaperSection)
class PaperSectionAdmin(admin.ModelAdmin):
    list_display = ["id", "paper", "order", "title", "question_type",
                    "score_per_question", "question_count"]
    list_filter = ["question_type"]
    search_fields = ["title", "paper__title"]
    raw_id_fields = ["paper"]
    ordering = ["paper", "order"]


@admin.register(PaperQuestion)
class PaperQuestionAdmin(admin.ModelAdmin):
    list_display = ["id", "paper", "order", "question", "section",
                    "score", "question_type", "difficulty"]
    list_filter = ["question_type", "difficulty"]
    search_fields = ["paper__title", "question__stem"]
    raw_id_fields = ["paper", "section", "question"]
    ordering = ["paper", "order"]
