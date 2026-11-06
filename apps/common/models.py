"""抽象模型基类。新模块的业务模型优先继承这些基类，保证审计字段一致。"""

import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """带创建时间 / 更新时间的抽象基类。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class UUIDModel(models.Model):
    """对外暴露 uuid、对内仍用自增主键。

    好处：API 里可以只暴露 uuid，不泄漏自增 id 的业务规模。
    """

    uuid = models.UUIDField("唯一标识", default=uuid.uuid4, editable=False, unique=True, db_index=True)

    class Meta:
        abstract = True


class BaseModel(TimeStampedModel, UUIDModel):
    """项目通用基类：创建/更新时间 + UUID。"""

    class Meta(TimeStampedModel.Meta):
        abstract = True
