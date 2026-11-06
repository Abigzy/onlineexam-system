"""班级模块服务层：建班 / 改班 / 归档 / 成员管理。

权限口径（与 ``ClassroomQuerySet`` 一致）：
  * 管理员 —— 可管理全部班级；
  * 教师 —— 可管理「自己创建的」或「担任班主任的」班级；
  * 学生 —— 只读，只能看自己所在班级。
"""

import re

from django.db import transaction

from apps.common.constants import Role
from apps.common.exceptions import BusinessError
from django.contrib.auth import get_user_model

from .constants import STUDENT_ROLE, TEACHER_ROLES, ClassType
from .models import Classroom

User = get_user_model()

# 区分「没传」与「传了 null / 空」——局部更新里这是两个语义
_UNSET = object()

# 年级里的「4 位年份」：'2026级' / '2026届' / '2026' / ' 2026 级' 都能抓到 2026
_GRADE_YEAR_RE = re.compile(r"(\d{4})")
# 学科里的空白：半角 + 全角空格（\u3000），用于把「操作系统 」和「操作系统」视为同一门课
_SPACE_RE = re.compile(r"[\s\u3000]+")


def _normalize_grade(value) -> str:
    """年级归一化：``'2026'`` / ``'2026级'`` / ``'2026届'`` / ``' 2026级 '`` 都归一为 ``'2026'``。

    优先取第一个 4 位数字年份；取不到就退化为 ``strip + lower``（不硬猜，
    宁可让两个奇怪的写法被判成不同年级，也不要把它们错并成一个）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    matched = _GRADE_YEAR_RE.search(text)
    if matched:
        return matched.group(1)
    return text.lower()


def _normalize_subject(value) -> str:
    """学科归一化：去掉所有空白（含全角空格 ``\\u3000``）并 ``lower``。

    库里存的是同一门课、只是录入时多了个空格或大小写不同（如 ``'操作系统 '``），
    归一化后应当被视为同一学科。
    """
    return _SPACE_RE.sub("", str(value or "")).lower()


class ClassroomService:
    """班级的业务操作，视图只做参数校验 + 序列化。"""

    # ------------------------------------------------------------------
    @staticmethod
    def can_manage(user, classroom) -> bool:
        """是否可管理（建/改/成员增删/删除）。

        学科老师只要在该班的授课名单里，就算「自己教的班」，可管理。
        """
        if getattr(user, "is_admin", False):
            return True
        if not getattr(user, "is_teacher", False):
            return False
        return (
            classroom.created_by_id == user.id
            or classroom.head_teacher_id == user.id
            or classroom.teachers.filter(id=user.id).exists()
        )

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def create(actor, *, name, grade_year="", subject="", description="",
               class_type=None, admin_class_id=None,
               head_teacher_id=None, teacher_ids=None, student_ids=None) -> Classroom:
        """建班。班主任/授课教师必须是教师角色，学生成员必须是学生角色。

        学科班（``class_type=subject``）可挂靠一个已有的行政班（``admin_class_id``）；
        行政班不能挂靠别的班。

        **学科老师建学科班时，自动把自己加入授课名单**（与传入的 ``teacher_ids``
        合并去重，不覆盖）。管理员 / 命令行代建（``actor`` 为 None）不自动绑定，
        授课教师仍完全由 ``teacher_ids`` 决定。
        """
        class_type = class_type or ClassType.ADMIN
        # 学科班不设班主任：学科老师通过「授课名单」归属，不属于班主任制度
        if class_type == ClassType.SUBJECT and head_teacher_id:
            raise BusinessError("学科班不设班主任，学科老师请通过授课教师名单归属")
        head_teacher = ClassroomService._resolve_teacher(head_teacher_id) \
            if head_teacher_id else None
        # 学科老师建班即授课：自动进入授课名单，省掉「建完再勾自己」这一步。
        # 管理员 / 命令行代建不自动绑定——他们不是这门课的老师，
        # 授课教师仍由 teacher_ids 指定。合并而非覆盖：传了其他老师也要保留。
        if (class_type == ClassType.SUBJECT
                and actor is not None
                and getattr(actor, "is_teacher", False)
                and not getattr(actor, "is_admin", False)):
            teacher_ids = sorted({actor.id, *(teacher_ids or [])})
        classroom = Classroom.objects.create(
            name=name,
            grade_year=grade_year,
            subject=subject,
            class_type=class_type,
            admin_class=ClassroomService._resolve_admin_class(class_type, admin_class_id),
            description=description,
            head_teacher=head_teacher,
            created_by=actor,
        )
        if teacher_ids:
            classroom.teachers.set(ClassroomService._resolve_teachers(teacher_ids))
        if student_ids:
            ClassroomService.add_students(classroom, student_ids)
        return classroom

    # ------------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def update(classroom, *, name=_UNSET, grade_year=_UNSET, subject=_UNSET,
               description=_UNSET, class_type=_UNSET, admin_class_id=_UNSET,
               head_teacher_id=_UNSET, teacher_ids=_UNSET) -> Classroom:
        """改班（局部更新语义：未传的字段不动）。"""
        if name is not _UNSET:
            classroom.name = name
        if grade_year is not _UNSET:
            classroom.grade_year = grade_year
        if subject is not _UNSET:
            classroom.subject = subject
        if description is not _UNSET:
            classroom.description = description
        if class_type is not _UNSET:
            classroom.class_type = class_type
            # 改成行政班后不能再挂靠别的班；改成学科班则清空班主任
            if class_type != ClassType.SUBJECT:
                classroom.admin_class = None
            else:
                classroom.head_teacher = None
        if admin_class_id is not _UNSET:
            classroom.admin_class = ClassroomService._resolve_admin_class(
                classroom.class_type, admin_class_id,
            )
        if head_teacher_id is not _UNSET:
            if classroom.class_type == ClassType.SUBJECT and head_teacher_id:
                raise BusinessError("学科班不设班主任，学科老师请通过授课教师名单归属")
            classroom.head_teacher = (
                ClassroomService._resolve_teacher(head_teacher_id)
                if head_teacher_id else None
            )
        classroom.save()
        if teacher_ids is not _UNSET:
            classroom.teachers.set(ClassroomService._resolve_teachers(teacher_ids or []))
        return classroom

    # ------------------------------------------------------------------
    @staticmethod
    def archive(classroom) -> Classroom:
        """归档（软删除）。历史成绩/学情里的班级归属不受影响。"""
        classroom.is_active = False
        classroom.save(update_fields=["is_active", "updated_at"])
        return classroom

    @staticmethod
    def delete(classroom) -> None:
        """删除班级（**物理删除**）。

        班级与考试/成绩之间没有外键，删除不会破坏历史成绩的可追溯性；
        成员与授课教师的多对多关系随之清理，学生账号本身不受影响。
        若有学科班挂靠它作为行政班，``admin_class`` 会被置空（SET_NULL）。
        """
        classroom.delete()

    # ------------------------------------------------------------------
    @staticmethod
    def add_students(classroom, student_ids) -> dict:
        """添加学生成员。返回实际加入的 id、未找到的 id、因冲突跳过的学生。

        * ``missing`` 只表示「查无此人 / 不是有效学生」，**冲突学生不进去**；
        * 冲突口径见 :meth:`_subject_conflicts`：同一老师 + 同学科 + 同年级的
          另一个学科班已有该学生时跳过（学生进了两个老师的同类班是正常的，
          进了同一个老师的两个同名班才是重复）。
        """
        student_ids = [int(i) for i in (student_ids or [])]
        students = list(
            User.objects.filter(id__in=student_ids, role=STUDENT_ROLE, is_active=True)
        )
        found = {s.id for s in students}
        missing = [i for i in student_ids if i not in found]
        conflicts = ClassroomService._subject_conflicts(classroom, students)
        to_add = [s for s in students if s.id not in conflicts]
        if to_add:
            classroom.students.add(*to_add)
        return {
            "added": sorted(s.id for s in to_add),
            "missing": missing,
            "conflicts": ClassroomService._conflict_items(conflicts, students),
        }

    @staticmethod
    def add_students_by_no(classroom, student_nos) -> dict:
        """按学号批量导入：找到的加进来，找不到的如实返回（不打断整批）。

        冲突口径与 :meth:`add_students` 完全一致；``conflicts`` 里的 ``student_no``
        用库里的**真实学号**（输入值可能带空格或大小写差异）。
        """
        student_nos = [str(n).strip() for n in (student_nos or []) if str(n).strip()]
        students = list(
            User.objects.filter(student_no__in=student_nos, role=STUDENT_ROLE, is_active=True)
        )
        found_nos = {s.student_no for s in students}
        missing = [n for n in student_nos if n not in found_nos]
        conflicts = ClassroomService._subject_conflicts(classroom, students)
        to_add = [s for s in students if s.id not in conflicts]
        if to_add:
            classroom.students.add(*to_add)
        return {
            "added": sorted(s.student_no for s in to_add),
            "missing": missing,
            "conflicts": ClassroomService._conflict_items(conflicts, students),
        }

    @staticmethod
    @transaction.atomic
    def add_students_from_classroom(classroom, source_classroom) -> dict:
        """从行政班整班导入：把源班全部学生加入本班，已在班内的跳过。

        * 源班必须是**行政班**——学科班是按课程分的教学班，整班导入会串课；
        * 只读源班成员，不修改源班；去重用 set 差集，不做逐条 ``exists()`` 查询；
        * ``skipped`` 的**两个来源**：① 已在目标班里的学生；② 撞上
          :meth:`_subject_conflicts` 冲突口径的学生。跳过的明细只在
          ``conflicts`` 里给出（「已在班内」无需提示，本来就没打算重复加）；
        * 返回 ``{"added", "skipped", "source", "total", "conflicts"}``，
          恒有 ``added + skipped == total``（total = 源班有效学生数）。
        """
        if source_classroom.class_type != ClassType.ADMIN:
            raise BusinessError("只能从行政班整班导入")
        source_students = list(source_classroom.students.filter(is_active=True))
        existing_ids = set(classroom.students.values_list("id", flat=True))
        candidates = [s for s in source_students if s.id not in existing_ids]
        conflicts = ClassroomService._subject_conflicts(classroom, candidates)
        to_add = [s for s in candidates if s.id not in conflicts]
        if to_add:
            classroom.students.add(*to_add)
        return {
            "added": len(to_add),
            # 已在班内 + 冲突跳过，两者都算 skipped，保证恒等式成立
            "skipped": len(source_students) - len(to_add),
            "source": source_classroom.name,
            "total": len(source_students),
            "conflicts": ClassroomService._conflict_items(conflicts, candidates),
        }

    @staticmethod
    def remove_students(classroom, student_ids) -> int:
        """移出学生成员。返回实际移出的人数。"""
        student_ids = [int(i) for i in (student_ids or [])]
        if not student_ids:
            return 0
        existing = list(classroom.students.filter(id__in=student_ids))
        if existing:
            classroom.students.remove(*existing)
        return len(existing)

    # ------------------------------------------------------------------
    # 内部工具：学科班「同一老师 + 同学科 + 同年级」的去重
    # ------------------------------------------------------------------
    @staticmethod
    def _subject_conflicts(classroom, students) -> dict:
        """找出已经在「同老师 / 同学科 / 同年级」的**另一个学科班**里的学生。

        返回 ``{student_id: {"classroom_id": ..., "classroom_name": ...}}``，
        表示该学生不该再进 ``classroom``（一个学生不该在同一老师的同名课里占两个座位）。

        判定不了就不拦——以下情况直接返回 ``{}``，宁可放过也不误伤：
          * 目标班不是学科班（行政班不参与）；
          * 目标班的 ``teachers`` / ``subject`` / ``grade_year`` 任一为空；
          * 没有候选学生。

        实现要点：
          * 「老师」取 ``Classroom.teachers``（授课教师），**不是** ``head_teacher``
            ——班主任是行政班制度的角色，学科班的归属靠授课名单；
          * 学科与年级**先归一化再比较**：库里存在 ``'2026级'`` 与 ``'2026'`` 混写、
            学科带尾随空格的情况，精确相等会漏拦（见 ``_normalize_grade`` /
            ``_normalize_subject``）；
          * SQL 只负责**收窄候选**（学科班 + 生效 + 同老师 + 含这些学生 + 排除自身），
            学科/年级的宽松比较放到 Python 做——学科班总量是几十量级，代价可忽略，
            而 ``teachers__in`` + ``students__in`` 保证了不会拉全表；
          * 一次查询拿全，不逐学生 ``exists()``；
          * 一个学生撞上多个冲突班时取 ``id`` 最小的那个，保证结果确定。
        """
        if classroom.class_type != ClassType.SUBJECT:
            return {}
        subject = _normalize_subject(classroom.subject)
        grade_year = _normalize_grade(classroom.grade_year)
        teacher_ids = list(classroom.teachers.values_list("id", flat=True))
        if not subject or not grade_year or not teacher_ids:
            return {}
        student_ids = [s.id for s in students]
        if not student_ids:
            return {}

        rows = (
            Classroom.objects
            .filter(
                class_type=ClassType.SUBJECT,
                is_active=True,
                teachers__in=teacher_ids,
            )
            .exclude(pk=classroom.pk)
            .filter(students__in=student_ids)
            .values_list(
                "students__id", "id", "name", "subject", "grade_year",
            )
            .distinct()
            .order_by("id", "students__id")
        )
        conflicts = {}
        for student_id, classroom_id, classroom_name, row_subject, row_grade in rows:
            # 归一化后再比：'2026' == '2026级'，'操作系统 ' == '操作系统'
            if _normalize_subject(row_subject) != subject:
                continue
            if _normalize_grade(row_grade) != grade_year:
                continue
            conflicts.setdefault(student_id, {
                "classroom_id": classroom_id,
                "classroom_name": classroom_name,
            })
        return conflicts

    @staticmethod
    def _conflict_items(conflicts: dict, students) -> list:
        """把 :meth:`_subject_conflicts` 的结果拼成给前端的列表（顺序沿用入参）。"""
        items = []
        for student in students:
            hit = conflicts.get(student.id)
            if not hit:
                continue
            items.append({
                "student_id": student.id,
                "student_no": student.student_no or "",
                "student_name": student.display_name,
                "classroom_id": hit["classroom_id"],
                "classroom_name": hit["classroom_name"],
            })
        return items

    # ------------------------------------------------------------------
    # 内部工具：把「教师 / 学生」角色的校验收在一处
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_teacher(user_id):
        teacher = User.objects.filter(id=user_id, is_active=True).first()
        if teacher is None or teacher.role not in TEACHER_ROLES:
            raise BusinessError("班主任 / 授课教师必须是有效的教师账号")
        return teacher

    @staticmethod
    def _resolve_admin_class(class_type, admin_class_id):
        """校验「学科班挂靠行政班」：只有学科班能关联，且目标必须是行政班。"""
        if not admin_class_id:
            return None
        if class_type != ClassType.SUBJECT:
            raise BusinessError("只有学科班才能关联行政班")
        admin_class = Classroom.objects.filter(
            pk=admin_class_id, class_type=ClassType.ADMIN,
        ).first()
        if admin_class is None:
            raise BusinessError("所属行政班不存在，或该班级不是行政班")
        return admin_class

    @staticmethod
    def _resolve_teachers(user_ids) -> list:
        teachers = list(
            User.objects.filter(id__in=user_ids, role__in=TEACHER_ROLES, is_active=True)
        )
        if len(teachers) != len(set(user_ids)):
            raise BusinessError("授课教师中包含无效的账号")
        return teachers
