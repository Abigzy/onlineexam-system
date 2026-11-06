"""
初始化演示账号。

用法：
    python manage.py seed_users                 # 创建默认演示账号
    python manage.py seed_users --reset         # 已存在则重置密码

默认账号（密码统一为 Exam@123456）：
    admin   / Exam@123456   管理员
    teacher / Exam@123456   教师
    student / Exam@123456   学生
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.common.constants import Role
from apps.users.models import User

DEFAULT_PASSWORD = "Exam@123456"

DEMO_USERS = [
    {
        "username": "admin",
        "email": "admin@example.com",
        "real_name": "系统管理员",
        "role": Role.ADMIN,
        "student_no": "A0001",
        "school": "教务处",
        "is_staff": True,
        "is_superuser": True,
    },
    {
        "username": "teacher",
        "email": "teacher@example.com",
        "real_name": "张老师",
        "role": Role.TEACHER,
        "student_no": "T1001",
        "school": "计算机学院",
    },
    {
        "username": "student",
        "email": "student@example.com",
        "real_name": "李同学",
        "role": Role.STUDENT,
        "student_no": "2021001",
        "school": "计算机学院",
    },
]


class Command(BaseCommand):
    help = "创建演示用的管理员 / 教师 / 学生账号"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="账号已存在时重置密码")
        parser.add_argument("--password", default=DEFAULT_PASSWORD, help="统一设置的初始密码")

    @transaction.atomic
    def handle(self, *args, **options):
        password = options["password"]
        reset = options["reset"]
        created_count = 0

        for item in DEMO_USERS:
            username = item["username"]
            user = User.objects.filter(username=username).first()

            if user is None:
                user = User(**{k: v for k, v in item.items()})
                user.set_password(password)
                user.save()
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"  + 已创建 {username} ({user.get_role_display()})"))
            elif reset:
                user.set_password(password)
                user.save(update_fields=["password"])
                self.stdout.write(self.style.WARNING(f"  ~ 已重置 {username} 的密码"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"完成：新建 {created_count} 个账号"))
        self.stdout.write(f"统一密码：{password}")
        self.stdout.write("登录入口：http://127.0.0.1:8000/login/")
