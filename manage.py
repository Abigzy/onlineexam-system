#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    # 默认加载开发配置；也可用环境变量覆盖为 OnlineExam.settings.production
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OnlineExam.settings.development")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "无法导入 Django。请确认已安装依赖，并已激活项目虚拟环境："
            r" D:\OnlineExam\.venv\Scripts\activate"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
