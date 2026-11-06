"""生成演示考试。

一场好的演示需要覆盖三种「时间形态」——正在考的、还没开始的、已经结束的，
因为它们在学生端会长成完全不同的样子（可作答 / 倒计时预告 / 只能看结果）。

用法：
    python manage.py seed_exams                # 生成 3 场演示考试
    python manage.py seed_exams --reset        # 先清掉旧的演示考试再生成
    python manage.py seed_exams --no-publish   # 只建草稿，不发布
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.common.constants import ExamStatus
from apps.papers.models import Paper

from apps.exam.models import Exam
from apps.exam.services import ExamService

DEMO_PREFIX = "[演示]"


class Command(BaseCommand):
    help = "生成演示考试（进行中 / 待开始 / 已结束各一场）"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="先删除旧的演示考试")
        parser.add_argument("--no-publish", action="store_true", help="只创建草稿，不发布")
        parser.add_argument("--publish-paper", action="store_true", default=True,
                            help="引用的试卷若是草稿，顺手发布它（默认开启）")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            removed, _ = Exam.objects.filter(title__startswith=DEMO_PREFIX).delete()
            self.stdout.write(f"已清理旧演示数据（{removed} 条）")

        papers = self._pick_papers(options["publish_paper"])
        if not papers:
            self.stderr.write(self.style.ERROR(
                "题库/试卷为空，请先执行：python manage.py seed_questions && python manage.py seed_papers"
            ))
            return

        now = timezone.now()
        plans = [
            {
                "title": f"{DEMO_PREFIX}《数据结构》随堂小测（进行中）",
                "paper": papers[0],
                "description": "随堂测验，45 分钟。中途请勿离开页面，切屏超过 3 次会被记录。",
                "start_time": now - timedelta(minutes=20),
                "end_time": now + timedelta(hours=3),
                "duration_minutes": 45,
                "max_attempts": 1,
                "shuffle_questions": False,
                "detect_switch": True,
                "max_switch_count": 3,
                "auto_submit_on_limit": False,
                "allow_resume": True,
                "status": ExamStatus.PUBLISHED,
            },
            {
                "title": f"{DEMO_PREFIX}《数据结构》期中考试（待开始）",
                "paper": papers[min(1, len(papers) - 1)],
                "description": "闭卷考试。题目与选项均随机排序，请每位同学独立作答。",
                "start_time": now + timedelta(days=1),
                "end_time": now + timedelta(days=1, hours=2),
                "duration_minutes": 90,
                "max_attempts": 1,
                "shuffle_questions": True,
                "shuffle_options": True,
                "detect_switch": True,
                "max_switch_count": 2,
                "auto_submit_on_limit": True,
                "fullscreen_required": False,
                "allow_resume": True,
                "status": ExamStatus.PUBLISHED,
            },
            {
                "title": f"{DEMO_PREFIX}《数据结构》补考（已结束）",
                "paper": papers[-1],
                "description": "上学期补考。可以查看成绩与逐题得分。",
                "start_time": now - timedelta(days=3),
                "end_time": now - timedelta(days=3) + timedelta(hours=1),
                "duration_minutes": 60,
                "max_attempts": 2,
                "allow_resume": True,
                "show_score_immediately": True,
                "status": ExamStatus.PUBLISHED,
            },
        ]

        created = []
        for plan in plans:
            status = plan.pop("status")
            exam = ExamService.create(data=plan, user=self._teacher())
            if not options["no_publish"]:
                ExamService.change_status(exam, status)
                if exam.end_time and exam.end_time < now:
                    # 时间已经过去 → 让状态机落到「已结束」
                    ExamService.sync_status(exam, now)
            created.append(exam)

        self.stdout.write(self.style.SUCCESS(f"\n已生成 {len(created)} 场演示考试："))
        for exam in created:
            self.stdout.write(
                f"  #{exam.id} {exam.title}"
                f"  {exam.paper.question_count} 题 / {exam.paper.total_score_float:g} 分"
                f"  / 限时 {exam.effective_duration_minutes} 分钟"
                f"  [{exam.get_status_display()}]"
            )
        self.stdout.write("\n学生用 student / Exam@123456 登录 → http://127.0.0.1:8000/my-exams/")

    # ------------------------------------------------------------------
    def _pick_papers(self, auto_publish: bool) -> list:
        """挑三份试卷；优先用已发布的，不够就把草稿发布掉顶上来。"""
        published = list(Paper.objects.published().exclude(question_count=0).order_by("id")[:3])
        if len(published) >= 3:
            return published

        candidates = list(Paper.objects.exclude(question_count=0).order_by("id")[:6])
        if auto_publish:
            for paper in candidates:
                if paper not in published and paper.status == "draft":
                    paper.status = "published"
                    paper.published_at = timezone.now()
                    paper.save(update_fields=["status", "published_at", "updated_at"])
                    self.stdout.write(f"  · 已顺手发布试卷《{paper.title}》")
                    published.append(paper)
                if len(published) >= 3:
                    break
        return published or candidates[:3]

    def _teacher(self):
        from apps.users.models import User

        return (
            User.objects.filter(role="teacher").order_by("id").first()
            or User.objects.filter(is_superuser=True).first()
        )
