"""班级模块数据模型。

设计要点：
  * ``Classroom`` 是系统里**第一个真正的「班级」实体**——此前模块五「班级成绩单」、
    模块六「班级维度」其实都是「某场考试的全部考生」的临时聚合，学生并没有被分班。
    班级模块把「学生 ↔ 班级 ↔ 教师」这条关系落到表里，成绩单 / 学情再按班聚合。
  * 学生用多对多（``students``）：允许一名学生同属多个班（行政班 + 学科班），
    不做「一个学生只能进一个班」的强约束——那是行政制度，不是数据模型该拍板的。
  * 归档用软删除（``is_active=False``），保护历史成绩/学情的可追溯性。
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel

from .constants import ClassType


class ClassroomQuerySet(models.QuerySet):
    """班级查询集，承载常用过滤，视图里不拼长链。"""

    def active(self):
        return self.filter(is_active=True)

    def search(self, keyword: str):
        """按班级名 / 年级 / 学科模糊搜索。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return self
        return self.filter(
            models.Q(name__icontains=keyword)
            | models.Q(grade_year__icontains=keyword)
            | models.Q(subject__icontains=keyword)
        )

    def visible_to(self, user):
        """当前用户可见的班级：
          * 管理员 —— 全部；
          * 教师 —— 自己创建的 / 担任班主任的 / 在授课名单里的，
            外加**全部行政班**（行政班是学籍班，教师建学科班时要能选到它挂靠）；
          * 学生 —— 自己所在班级。
        """
        if getattr(user, "is_admin", False):
            return self
        if getattr(user, "is_teacher", False):
            return self.filter(
                models.Q(created_by=user)
                | models.Q(head_teacher=user)
                | models.Q(teachers=user)
                | models.Q(class_type=ClassType.ADMIN)
            ).distinct()
        return self.filter(students=user)

    def manageable_by(self, user):
        """当前用户可**管理**的班级（建班/改成员/删除）。
          * 管理员 —— 全部；
          * 教师 —— 自己创建的 / 担任班主任的 / **在授课名单里的**（学科老师管自己教的班）；
          * 学生 —— 无。
        """
        if getattr(user, "is_admin", False):
            return self
        if getattr(user, "is_teacher", False):
            return self.filter(
                models.Q(created_by=user)
                | models.Q(head_teacher=user)
                | models.Q(teachers=user)
            ).distinct()
        return self.none()

    def with_relations(self):
        return self.select_related(
            "head_teacher", "created_by", "admin_class",
        ).prefetch_related("teachers", "students")


class Classroom(TimeStampedModel):
    """班级。"""

    name = models.CharField(_("班级名称"), max_length=64)
    grade_year = models.CharField(_("年级 / 届别"), max_length=16, blank=True, default="")
    subject = models.CharField(_("授课学科"), max_length=64, blank=True, default="")
    class_type = models.CharField(
        _("班级类型"), max_length=16, choices=ClassType.choices,
        default=ClassType.ADMIN, db_index=True,
        help_text="行政班（学籍班）或学科班（按课程分的教学班）",
    )
    admin_class = models.ForeignKey(
        "self",
        verbose_name=_("所属行政班"),
        related_name="subject_classes",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        limit_choices_to={"class_type": ClassType.ADMIN},
        help_text="仅学科班可填：该学科班挂靠的行政班",
    )

    head_teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("班主任"),
        related_name="headed_classrooms",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("授课教师"),
        related_name="teaching_classrooms",
        blank=True,
    )
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("学生成员"),
        related_name="classrooms",
        blank=True,
    )

    description = models.TextField(_("班级简介"), blank=True, default="")
    is_active = models.BooleanField(_("启用"), default=True, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("创建人"),
        related_name="created_classrooms",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    objects = ClassroomQuerySet.as_manager()

    class Meta:
        db_table = "classroom"
        verbose_name = "班级"
        verbose_name_plural = "班级"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_active", "-created_at"], name="idx_classroom_active"),
            models.Index(fields=["name"], name="idx_classroom_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def student_count(self) -> int:
        return self.students.count()

    @property
    def teacher_count(self) -> int:
        return self.teachers.count()
