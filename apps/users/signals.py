"""
用户相关信号。

注册位置固定在 ``UsersConfig.ready()``，保证只注册一次、不会有副作用被意外触发。
"""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.common.constants import Role

from .models import User

logger = logging.getLogger("apps.users.signals")


@receiver(pre_save, sender=User)
def normalize_user_fields(sender, instance: User, **kwargs):
    """保存前做数据清洗，避免各处重复写同一段逻辑。"""
    if instance.email:
        instance.email = instance.email.strip().lower()
    if instance.username:
        instance.username = instance.username.strip()
    for field in ("real_name", "phone", "student_no", "school"):
        value = getattr(instance, field, None)
        if isinstance(value, str):
            setattr(instance, field, value.strip())


@receiver(post_save, sender=User)
def grant_admin_permissions(sender, instance: User, created: bool, **kwargs):
    """管理员角色自动获得后台入口权限。

    只做单向放开（角色 → is_staff），不反向收回，
    避免把手工调试用的账号被意外降权。
    """
    if instance.role == Role.ADMIN and not (instance.is_staff and instance.is_superuser):
        User.objects.filter(pk=instance.pk).update(is_staff=True)
        logger.debug("用户 %s 因角色为管理员，已自动开启 is_staff", instance.username)
