"""初始化演示试卷。

用法：
    python manage.py seed_papers              # 生成 3 份演示卷（含 AI 组卷说明）
    python manage.py seed_papers --no-notes   # 不调大模型，只组卷
    python manage.py seed_papers --reset      # 先删掉同名的演示卷再重建

三份卷子刻意选了不同的「形态」，方便演示时一屏看完各种情况：
    1. 期中测试卷 —— 全知识点、标准难度、草稿状态（可以现场演示加题/调分）
    2. 章节小测   —— 只考两个知识板块、题量小、偏易
    3. 期末综合卷 —— 全知识点、偏难、已发布（用来演示状态流转与打印导出）
"""

from django.core.management.base import BaseCommand

from apps.common.exceptions import BusinessError
from apps.papers.composer import PaperComposeService
from apps.papers.constants import PaperStatus
from apps.papers.models import Paper
from apps.papers.services import PaperService
from apps.questions.models import KnowledgePoint
from apps.users.models import User
from apps.common.constants import Role

DEFAULT_SUBJECT = "数据结构"

DEMO_PAPERS = [
    {
        "title": "《数据结构》期中测试卷",
        "target_difficulty": 2.0,
        "duration_minutes": 120,
        "knowledge_points": None,
        "publish": False,
    },
    {
        "title": "《数据结构》章节小测（线性表与栈队列）",
        "target_difficulty": 1.6,
        "duration_minutes": 60,
        "knowledge_points": ["线性表", "栈与队列"],
        "blueprint": [
            {"type": "single", "count": 5, "score": 4},
            {"type": "multiple", "count": 3, "score": 4},
            {"type": "judge", "count": 5, "score": 2},
            {"type": "fill", "count": 5, "score": 4},
            {"type": "short", "count": 2, "score": 10},
            {"type": "essay", "count": 1, "score": 14},
        ],
        "publish": False,
    },
    {
        "title": "《数据结构》期末综合卷（偏难）",
        "target_difficulty": 3.0,
        "duration_minutes": 150,
        "knowledge_points": None,
        "publish": True,
    },
]


class Command(BaseCommand):
    help = "生成演示用的试卷（AI 智能组卷 + 组卷说明）"

    def add_arguments(self, parser):
        parser.add_argument("--subject", default=DEFAULT_SUBJECT, help="学科/课程")
        parser.add_argument("--reset", action="store_true", help="先删除同名的演示卷")
        parser.add_argument("--no-notes", action="store_true", help="不调用大模型生成组卷说明")

    def handle(self, *args, **options):
        subject = options["subject"]
        teacher = self._teacher()

        titles = [row["title"] for row in DEMO_PAPERS]
        if options["reset"]:
            removed = Paper.objects.filter(title__in=titles).delete()[0]
            self.stdout.write(self.style.WARNING(f"已清除同名演示卷（{removed} 行）\n"))

        existing = set(Paper.objects.filter(title__in=titles).values_list("title", flat=True))
        if len(existing) == len(titles):
            self.stdout.write(self.style.WARNING("演示卷已存在，如需重建请加 --reset"))
            return

        knowledge_map = {
            point.name: point.id
            for point in KnowledgePoint.objects.filter(subject=subject)
        }
        if not knowledge_map:
            self.stderr.write(self.style.ERROR(
                f"学科「{subject}」下没有知识点，请先执行 python manage.py seed_questions"
            ))
            return

        created = []
        for spec in DEMO_PAPERS:
            if spec["title"] in existing:
                self.stdout.write(f"跳过（已存在）：{spec['title']}")
                continue
            created.append(self._build(spec=spec, subject=subject, teacher=teacher,
                                        knowledge_map=knowledge_map, options=options))

        self._report(created, options, subject)

    # ------------------------------------------------------------------
    def _build(self, *, spec, subject, teacher, knowledge_map, options):
        title = spec["title"]
        self.stdout.write(self.style.HTTP_INFO(f"正在组卷：{title}"))

        point_ids = None
        if spec.get("knowledge_points"):
            point_ids = [knowledge_map[name] for name in spec["knowledge_points"]
                         if name in knowledge_map]
            if not point_ids:
                self.stdout.write(self.style.WARNING("  指定的知识点不存在，改为全范围组卷"))

        try:
            result = PaperComposeService.compose(
                user=teacher,
                subject=subject,
                title=title,
                knowledge_point_ids=point_ids,
                blueprint=spec.get("blueprint"),
                target_difficulty=spec["target_difficulty"],
                duration_minutes=spec["duration_minutes"],
                seed=hash(title) % 100000,
            )
        except BusinessError as exc:
            self.stdout.write(self.style.ERROR(f"  组卷失败：{exc}"))
            return None

        paper = result["paper"]
        report = result["report"]
        self.stdout.write(
            f"  {paper.question_count} 题 / {paper.total_score_float:g} 分 / "
            f"实际难度 {report['actual_difficulty']}（目标 {spec['target_difficulty']}）"
            f" / 覆盖 {report['knowledge']['covered']} 个知识点"
        )
        if report["shortage"]:
            for row in report["shortage"]:
                self.stdout.write(self.style.WARNING(f"  ⚠ {row['message']}"))

        if not options["no_notes"]:
            try:
                from apps.papers.ai_services import PaperAIService

                PaperAIService.generate_notes(paper, user=teacher)
                self.stdout.write("  已生成组卷说明")
            except BusinessError as exc:
                self.stdout.write(self.style.WARNING(f"  组卷说明生成失败（不影响卷子）：{exc}"))

        if spec.get("publish"):
            try:
                paper = PaperService.change_status(paper, PaperStatus.PUBLISHED)
                self.stdout.write(f"  状态 → {paper.get_status_display()}")
            except BusinessError as exc:
                self.stdout.write(self.style.WARNING(f"  发布失败：{exc}"))

        return paper

    @staticmethod
    def _teacher():
        user = User.objects.filter(role=Role.TEACHER).order_by("id").first()
        if user is None:
            user = User.objects.filter(is_superuser=True).order_by("id").first()
        if user is None:
            raise BusinessError("数据库里没有教师账号，请先执行 python manage.py seed_users")
        return user

    def _report(self, created, options, subject):
        papers = [paper for paper in created if paper]
        self.stdout.write("\n" + "=" * 52)
        self.stdout.write(self.style.MIGRATE_HEADING("演示试卷已就绪"))
        for paper in papers:
            self.stdout.write(
                f"  #{paper.id}  {paper.title}"
                f"  {paper.question_count} 题 / {paper.total_score_float:g} 分"
                f"  [{paper.get_status_display()}]"
            )
        self.stdout.write(f"\n  试卷列表：  http://127.0.0.1:8000/papers/")
        if papers:
            self.stdout.write(f"  组卷工作台：http://127.0.0.1:8000/papers/{papers[0].id}/compose/")
        if options["no_notes"]:
            self.stdout.write(self.style.WARNING("  （本次跳过了 AI 组卷说明）"))
        self.stdout.write("=" * 52)


__all__ = ["Command"]
