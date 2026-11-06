"""用户模块常量与派发规则。"""

from django.utils import timezone


def grade_year_choices(years_back: int = 3) -> list:
    """注册可选的入学年级：当前年往前若干年。

    返回 ``[("2026", "2026级"), ("2025", "2025级"), ...]``。
    学生选年级后由系统据此派发学号（学号 = 年级 + 顺位）。
    """
    current = timezone.now().year
    return [(str(year), f"{year}级") for year in range(current, current - years_back - 1, -1)]
