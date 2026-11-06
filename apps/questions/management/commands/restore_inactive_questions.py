"""批量恢复被停用的题目。

为什么需要它：
    题库里有一批题被停用（``is_active=False``）——停用只是「不给学生看」，
    数据本身和学生的作答历史都还在。课程改版、知识点重挂之后经常需要把
    它们重新放回可用池；手工去 Django Admin 里一道道勾「启用」既慢又容易漏。

安全护栏（这是本命令存在的第一原则）：
    题目一旦被试卷 / 作答 / 考场行为日志引用，它的「启用状态」就不再只是
    题库内部的事——恢复它会让历史试卷重新看到这道题、也会影响成绩回放的
    口径。所以默认**跳过**被引用的题并明确提示；确实要恢复时用 ``--force``，
    并且输出里会把风险写清楚。

用法：
    python manage.py restore_inactive_questions --dry-run     # 只看不写
    python manage.py restore_inactive_questions               # 恢复全部未引用的停用题
    python manage.py restore_inactive_questions --subject 数据结构
    python manage.py restore_inactive_questions --show-items  # 顺带列出每道题的题干

命令是**幂等**的：恢复完再跑一次，找不到停用题就什么都不做。
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.exam.models import Answer
from apps.papers.models import PaperQuestion
from apps.questions.models import Question

# 考试行为日志（切屏、掉线等）也会带 question_id，但它是**历史记录**，
# 恢复题目不会篡改历史，因此只提示不拦截。
try:  # pragma: no cover - proctoring 模块是可选依赖关系的兜底
    from apps.proctoring.models import ExamBehaviorLog
except ImportError:  # pragma: no cover
    ExamBehaviorLog = None

UNKNOWN_SUBJECT = "（未填学科）"


class Command(BaseCommand):
    help = "把被停用的题目重新启用（默认跳过已被试卷/作答引用的题，避免污染历史数据）"

    def add_arguments(self, parser):
        parser.add_argument("--subject", default="", help="只处理指定学科（默认：全部学科）")
        parser.add_argument("--dry-run", action="store_true", help="只预览，不写数据库")
        parser.add_argument("--force", action="store_true",
                            help="连被试卷/作答引用的题也一并恢复（会改变历史试卷的口径，慎用）")
        parser.add_argument("--show-items", action="store_true", help="逐条列出将被恢复的题目")

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        subject = (options["subject"] or "").strip()
        dry_run = bool(options["dry_run"])

        queryset = Question.objects.filter(is_active=False)
        if subject:
            queryset = queryset.filter(subject=subject)

        total = queryset.count()
        if not total:
            self.stdout.write(self.style.WARNING(
                f"没有需要恢复的停用题{'（学科：%s）' % subject if subject else ''}。"
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n发现停用题 {total} 道"
            f"{'（学科：%s）' % subject if subject else '（全部学科）'}"
            f"　|　模式：{'DRY-RUN（只预览）' if dry_run else '写入数据库'}"
        ))
        self._print_subject_groups(queryset, title="停用题按学科分布")

        # ---------------- 引用检查 ----------------
        ids = list(queryset.values_list("id", flat=True))
        referenced = self._referenced_ids(ids)
        risky = [qid for qid in ids if qid in referenced]

        candidates = [qid for qid in ids if qid not in referenced]
        if options["force"] and risky:
            self.stdout.write(self.style.WARNING(
                f"--force：{len(risky)} 道被引用的题也纳入恢复范围（引用明细如下）"
            ))
            for qid in risky:
                self.stdout.write(f"    #{qid}：{'、'.join(referenced[qid])}")
            candidates = ids

        if not candidates:
            self.stdout.write(self.style.WARNING("可恢复的题目为 0 道（全部被引用，未加 --force）。"))
            self._print_summary(queryset, [], referenced, dry_run)
            return

        if options["show_items"]:
            self.stdout.write("\n待恢复题目清单：")
            for question in queryset.filter(id__in=candidates).order_by("subject", "id"):
                self.stdout.write(
                    f"    #{question.id}　[{question.get_type_display()}]"
                    f"　{question.stem[:46]}"
                )

        if dry_run:
            self._print_summary(queryset, candidates, referenced, dry_run=True)
            return

        # ---------------- 落库 ----------------
        # ``auto_now`` 只在 save() 时生效，批量 update() 不会动它，这里显式补上，
        # 否则「更新时间」会停留在停用那一刻，后面按时间排查问题会看不明白。
        now = timezone.now()
        with transaction.atomic():
            updated = Question.objects.filter(id__in=candidates, is_active=False).update(
                is_active=True, updated_at=now,
            )

        self.stdout.write(self.style.SUCCESS(f"\n已恢复 {updated} 道题（is_active=True）"))
        self._print_summary(queryset, candidates, referenced, dry_run=False)
        self._print_active_total()

    # ------------------------------------------------------------------
    # 引用检查
    # ------------------------------------------------------------------
    @staticmethod
    def _referenced_ids(ids: list) -> dict:
        """返回 ``{题目 id: [引用来源说明, ...]}``。

        三处引用来源：
          * ``PaperQuestion`` —— 被组进某张试卷；
          * ``Answer`` —— 学生已经对这道题作答过（答卷明细）；
          * ``ExamBehaviorLog`` —— 考场行为日志（仅提示，不阻止恢复）。
        """
        referenced: dict = {}

        paper_map = {}
        for row in (PaperQuestion.objects
                    .filter(question_id__in=ids)
                    .values("question_id", "paper__title")):
            paper_map.setdefault(row["question_id"], set()).add(row["paper__title"] or "未命名试卷")
        for question_id, titles in paper_map.items():
            referenced.setdefault(question_id, []).append(
                f"被 {len(titles)} 张试卷引用（{'、'.join(sorted(titles)[:3])}）"
            )

        answer_ids = set(
            Answer.objects
            .filter(paper_question__question_id__in=ids)
            .values_list("paper_question__question_id", flat=True)
        )
        for question_id in answer_ids:
            referenced.setdefault(question_id, []).append("已有学生作答记录")

        if ExamBehaviorLog is not None:
            behavior_ids = set(
                ExamBehaviorLog.objects
                .filter(question_id__in=ids)
                .values_list("question_id", flat=True)
            )
            for question_id in behavior_ids:
                referenced.setdefault(question_id, []).append("考场行为日志引用了该题（不阻止恢复）")

        return referenced

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def _print_subject_groups(self, queryset, title: str) -> None:
        rows = (queryset.values("subject")
                .annotate(total=Count("id"))
                .order_by("subject"))
        self.stdout.write(f"\n{title}：")
        for row in rows:
            subject = row["subject"] or UNKNOWN_SUBJECT
            self.stdout.write(f"    {subject:<14}{row['total']:>4} 道")

    def _print_summary(self, queryset, candidates: list, referenced: dict, *, dry_run: bool) -> None:
        candidate_set = set(candidates)
        grouped = {}
        for row in queryset.values("subject", "id"):
            if row["id"] not in candidate_set:
                continue
            grouped[row["subject"] or UNKNOWN_SUBJECT] = grouped.get(row["subject"] or UNKNOWN_SUBJECT, 0) + 1

        self.stdout.write("\n" + "-" * 52)
        self.stdout.write(f"  待恢复　　：{len(candidates)} 道" + ("" if dry_run else "（已写入）"))
        self.stdout.write(f"  引用跳过　：{len(referenced)} 道")
        if grouped:
            self.stdout.write("  按学科　　：")
            for subject, number in sorted(grouped.items()):
                self.stdout.write(f"      {subject:<14}{number:>4} 道")
        self.stdout.write("-" * 52)

    def _print_active_total(self) -> None:
        total = Question.objects.count()
        active = Question.objects.filter(is_active=True).count()
        self.stdout.write(f"\n题库现状：总数 {total} 道，启用 {active} 道，停用 {total - active} 道")
        for row in (Question.objects.filter(is_active=True)
                    .values("subject").annotate(total=Count("id")).order_by("subject")):
            self.stdout.write(f"    {row['subject'] or UNKNOWN_SUBJECT:<14}{row['total']:>5} 道")


__all__ = ["Command"]
