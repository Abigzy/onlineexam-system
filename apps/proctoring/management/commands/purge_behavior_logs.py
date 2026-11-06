"""裁剪过期的行为埋点。

逐条埋点是典型的「写多读少」数据：一场考试下来每个学生几十上百条，
一个学期就能堆到百万级。考完一段时间的明细对事后复盘已无价值，
按保留策略裁掉，控制表体积与索引开销。

用法：
    python manage.py purge_behavior_logs                 # 按默认保留天数裁剪
    python manage.py purge_behavior_logs --days 90       # 自定义保留天数
    python manage.py purge_behavior_logs --dry-run       # 只看会删多少，不真删
"""

from django.core.management.base import BaseCommand

from apps.proctoring.constants import LOG_RETENTION_DAYS
from apps.proctoring.services import BehaviorLogService


class Command(BaseCommand):
    help = "裁剪超过保留期的行为埋点（ExamBehaviorLog）"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=LOG_RETENTION_DAYS,
                            help=f"保留最近多少天的埋点，默认 {LOG_RETENTION_DAYS}")
        parser.add_argument("--dry-run", action="store_true",
                            help="只统计会删除多少条，不实际删除")

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        if days <= 0:
            self.stderr.write(self.style.ERROR("保留天数必须为正整数"))
            return

        if dry_run:
            count = BehaviorLogService.purge_old_logs(days, dry_run=True)
            self.stdout.write(f"[dry-run] 超过 {days} 天的埋点共 {count} 条将被删除")
            return

        deleted = BehaviorLogService.purge_old_logs(days)
        self.stdout.write(self.style.SUCCESS(f"已裁剪超过 {days} 天的行为埋点 {deleted} 条"))
