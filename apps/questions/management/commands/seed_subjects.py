"""
初始化「学科演示数据」：3 位老师 × 3 门学科 × 各自题库 + 3 个班级（每班 10 名学生）。

用法：
    python manage.py seed_subjects           # 幂等创建（已存在则跳过）
    python manage.py seed_subjects --reset   # 先清空本命令创建的学科数据再重建

产出：
  * 3 位教师（各绑定一门任教学科，登录后只能看到 / 管理自己学科的资源）：
      os_teacher  / Exam@123456   操作系统（王老师）
      net_teacher / Exam@123456   计算机网络（赵老师）
      arch_teacher/ Exam@123456   计算机组成原理（陈老师）
  * 每科一套知识点树 + 覆盖六种题型的样题；
  * 3 个班级（各对应一门学科、由对应老师创建），每班随机 10 名学生。
"""

import random

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.classroom.services import ClassroomService
from apps.common.constants import Difficulty, QuestionType, Role
from apps.questions.constants import QuestionSource
from apps.questions.models import KnowledgePoint, Question
from apps.questions.services import QuestionService
from apps.users.models import User

PASSWORD = "Exam@123456"

# 学科 → 教师 → 知识点树 → 题库
SUBJECTS = [
    {
        "subject": "操作系统",
        "teacher": {"username": "os_teacher", "real_name": "王老师", "student_no": "T2001"},
        "kps": [
            ("进程管理", "OS-01", None),
            ("进程与线程", "OS-01-1", "进程管理"),
            ("处理机调度", "OS-01-2", "进程管理"),
            ("内存管理", "OS-02", None),
            ("虚拟内存", "OS-02-1", "内存管理"),
            ("页面置换", "OS-02-2", "内存管理"),
            ("死锁", "OS-03", None),
        ],
        "questions": [
            dict(type=QuestionType.SINGLE, difficulty=Difficulty.EASY, score=2,
                 kps=["进程与线程"], stem="进程从运行态进入阻塞态，可能的原因是（　）。",
                 options=[("A", "等待某事件发生", True), ("B", "时间片用完", False),
                          ("C", "被更高优先级进程抢占", False), ("D", "进程运行结束", False)],
                 analysis="进程因等待 I/O 等事件而主动放弃 CPU，进入阻塞态。"),
            dict(type=QuestionType.SINGLE, difficulty=Difficulty.MEDIUM, score=2,
                 kps=["死锁"], stem="产生死锁的四个必要条件是互斥、请求与保持、不可剥夺和（　）。",
                 options=[("A", "循环等待", True), ("B", "进程同步", False),
                          ("C", "资源有序分配", False), ("D", "优先级反转", False)],
                 analysis="死锁四必要条件：互斥、请求与保持、不可剥夺、循环等待。"),
            dict(type=QuestionType.MULTIPLE, difficulty=Difficulty.MEDIUM, score=3,
                 kps=["页面置换"], stem="下列属于页面置换算法的有（　）。",
                 options=[("A", "FIFO", True), ("B", "LRU", True), ("C", "OPT", True),
                          ("D", "先来先服务", False)],
                 analysis="FIFO、LRU、OPT 都是页面置换算法；先来先服务是调度算法。"),
            dict(type=QuestionType.JUDGE, difficulty=Difficulty.EASY, score=1,
                 kps=["进程与线程"], stem="同一进程内的多个线程共享该进程的地址空间和打开的文件。",
                 answer="A", analysis="线程是进程内的执行单元，共享进程资源；这是线程与进程的关键区别。"),
            dict(type=QuestionType.JUDGE, difficulty=Difficulty.MEDIUM, score=1,
                 kps=["虚拟内存"], stem="虚拟内存的容量只受硬盘空间限制，与地址位数无关。",
                 answer="B", analysis="虚拟内存容量受 CPU 地址位数（寻址空间）限制，硬盘只是后备存储。"),
            dict(type=QuestionType.FILL, difficulty=Difficulty.MEDIUM, score=4,
                 kps=["处理机调度"], stem="在时间片轮转调度算法中，时间片设置过大时会退化为 ______ 算法。",
                 answer="先来先服务", analysis="时间片过大，每个进程都能一次运行完，效果等同 FCFS。"),
            dict(type=QuestionType.SHORT, difficulty=Difficulty.MEDIUM, score=5,
                 kps=["进程与线程"], stem="简述进程与线程的主要区别。",
                 answer="1. 进程是资源分配的基本单位，线程是 CPU 调度的基本单位；\n"
                        "2. 进程拥有独立的地址空间，同一进程的线程共享地址空间；\n"
                        "3. 进程间切换开销大，线程切换开销小；\n"
                        "4. 进程间通信需借助 IPC 机制，线程间可直接通过共享内存通信。",
                 analysis="评分要点：资源分配 vs 调度、地址空间、切换开销、通信方式。"),
            dict(type=QuestionType.ESSAY, difficulty=Difficulty.HARD, score=10,
                 kps=["死锁"], stem="论述处理死锁的四种基本策略，并各举一种具体方法。",
                 answer="1. 预防：破坏四个必要条件之一，如资源有序分配法（破坏循环等待）；\n"
                        "2. 避免：动态判断是否安全，如银行家算法；\n"
                        "3. 检测与解除：允许死锁发生，用资源分配图检测，再撤销进程或剥夺资源；\n"
                        "4. 鸵鸟策略：忽略死锁，适合发生概率极低的系统。",
                 analysis="评分要点：四种策略的定位、各自一个具体方法、适用场景。"),
        ],
    },
    {
        "subject": "计算机网络",
        "teacher": {"username": "net_teacher", "real_name": "赵老师", "student_no": "T2002"},
        "kps": [
            ("数据链路层", "NET-01", None),
            ("网络层", "NET-02", None),
            ("IP 协议", "NET-02-1", "网络层"),
            ("路由选择", "NET-02-2", "网络层"),
            ("传输层", "NET-03", None),
            ("TCP 协议", "NET-03-1", "传输层"),
            ("UDP 协议", "NET-03-2", "传输层"),
        ],
        "questions": [
            dict(type=QuestionType.SINGLE, difficulty=Difficulty.EASY, score=2,
                 kps=["IP 协议"], stem="IPv4 地址的长度是（　）位。",
                 options=[("A", "16", False), ("B", "32", True), ("C", "64", False), ("D", "128", False)],
                 analysis="IPv4 地址 32 位，IPv6 地址 128 位。"),
            dict(type=QuestionType.SINGLE, difficulty=Difficulty.MEDIUM, score=2,
                 kps=["TCP 协议"], stem="TCP 三次握手建立连接时，第二次握手发送的报文段标志位是（　）。",
                 options=[("A", "SYN", False), ("B", "SYN+ACK", True),
                          ("C", "ACK", False), ("D", "FIN", False)],
                 analysis="第二次握手服务器回 SYN+ACK，既确认又同步序列号。"),
            dict(type=QuestionType.MULTIPLE, difficulty=Difficulty.MEDIUM, score=3,
                 kps=["UDP 协议"], stem="下列关于 UDP 协议的说法正确的有（　）。",
                 options=[("A", "无连接", True), ("B", "不可靠传输", True),
                          ("C", "首部开销小", True), ("D", "提供流量控制", False)],
                 analysis="UDP 无连接、不可靠、首部仅 8 字节；流量控制是 TCP 的特性。"),
            dict(type=QuestionType.JUDGE, difficulty=Difficulty.EASY, score=1,
                 kps=["路由选择"], stem="RIP 协议基于距离向量算法，以跳数作为度量。",
                 answer="A", analysis="RIP 是典型的距离向量路由协议，度量是跳数，最多 15 跳。"),
            dict(type=QuestionType.JUDGE, difficulty=Difficulty.MEDIUM, score=1,
                 kps=["TCP 协议"], stem="TCP 的滑动窗口机制既用于流量控制，也用于可靠传输。",
                 answer="A", analysis="滑动窗口既限制发送方速率（流量控制），也配合确认实现可靠传输。"),
            dict(type=QuestionType.FILL, difficulty=Difficulty.MEDIUM, score=4,
                 kps=["数据链路层"], stem="以太网交换机工作在 OSI 参考模型的第 ______ 层。",
                 answer="二（数据链路层）", analysis="交换机依据 MAC 地址转发，属于数据链路层设备。"),
            dict(type=QuestionType.SHORT, difficulty=Difficulty.MEDIUM, score=5,
                 kps=["TCP 协议", "UDP 协议"], stem="比较 TCP 与 UDP 的主要区别。",
                 answer="1. TCP 面向连接，UDP 无连接；\n"
                        "2. TCP 可靠（确认、重传、序号），UDP 不可靠；\n"
                        "3. TCP 有流量控制与拥塞控制，UDP 没有；\n"
                        "4. TCP 首部 20 字节、开销大，UDP 首部 8 字节、开销小；\n"
                        "5. 应用场景：文件传输、邮件用 TCP；视频直播、DNS 用 UDP。",
                 analysis="评分要点：连接性、可靠性、控制机制、开销、典型场景。"),
            dict(type=QuestionType.ESSAY, difficulty=Difficulty.HARD, score=10,
                 kps=["TCP 协议"], stem="论述 TCP 如何通过「滑动窗口 + 拥塞控制」实现可靠且高效的传输。",
                 answer="可靠传输：序号与确认、超时重传、校验和保证数据不丢不错不乱序。\n"
                        "滑动窗口：连续发送多个报文段而不必逐段等待确认，提高吞吐，"
                        "窗口大小即接收方缓冲区大小（流量控制）。\n"
                        "拥塞控制：慢启动（指数增长）→ 拥塞避免（线性增长）→ 拥塞时乘法减小，"
                        "配合快重传、快恢复，避免网络过载。\n"
                        "二者结合：流量控制防「接收方被淹没」，拥塞控制防「网络被淹没」。",
                 analysis="评分要点：可靠机制、滑动窗口作用、拥塞控制四阶段、两者分工。"),
        ],
    },
    {
        "subject": "计算机组成原理",
        "teacher": {"username": "arch_teacher", "real_name": "陈老师", "student_no": "T2003"},
        "kps": [
            ("数据表示", "ARCH-01", None),
            ("补码", "ARCH-01-1", "数据表示"),
            ("浮点数", "ARCH-01-2", "数据表示"),
            ("存储器", "ARCH-02", None),
            ("Cache", "ARCH-02-1", "存储器"),
            ("指令系统", "ARCH-03", None),
            ("CPU", "ARCH-04", None),
        ],
        "questions": [
            dict(type=QuestionType.SINGLE, difficulty=Difficulty.EASY, score=2,
                 kps=["补码"], stem="8 位补码能表示的整数范围是（　）。",
                 options=[("A", "-128 ~ 127", True), ("B", "-127 ~ 127", False),
                          ("C", "0 ~ 255", False), ("D", "-128 ~ 128", False)],
                 analysis="n 位补码范围 -2^(n-1) ~ 2^(n-1)-1，8 位即 -128 ~ 127。"),
            dict(type=QuestionType.SINGLE, difficulty=Difficulty.MEDIUM, score=2,
                 kps=["Cache"], stem="Cache 的地址映射方式中，块可以放入任意位置的是（　）。",
                 options=[("A", "直接映射", False), ("B", "全相联映射", True),
                          ("C", "组相联映射", False), ("D", "顺序映射", False)],
                 analysis="全相联映射：主存任一块可装入 Cache 任意行，冲突最少、成本最高。"),
            dict(type=QuestionType.MULTIPLE, difficulty=Difficulty.MEDIUM, score=3,
                 kps=["CPU"], stem="CPU 的基本组成部分包括（　）。",
                 options=[("A", "运算器", True), ("B", "控制器", True),
                          ("C", "寄存器", True), ("D", "硬盘", False)],
                 analysis="CPU 由运算器、控制器和寄存器组构成；硬盘是外存设备。"),
            dict(type=QuestionType.JUDGE, difficulty=Difficulty.EASY, score=1,
                 kps=["浮点数"], stem="IEEE 754 单精度浮点数占用 64 位存储空间。",
                 answer="B", analysis="单精度 float 是 32 位，双精度 double 才是 64 位。"),
            dict(type=QuestionType.JUDGE, difficulty=Difficulty.MEDIUM, score=1,
                 kps=["补码"], stem="补码表示中，零有且只有一个编码。",
                 answer="A", analysis="补码统一了 ±0 的表示，零只有一种编码，这是补码优于原码的一点。"),
            dict(type=QuestionType.FILL, difficulty=Difficulty.MEDIUM, score=4,
                 kps=["Cache"], stem="Cache 的三种地址映射方式中，命中率最高的是 ______ 映射。",
                 answer="全相联", analysis="全相联映射冲突最少、命中率最高，但硬件复杂度也最高。"),
            dict(type=QuestionType.SHORT, difficulty=Difficulty.MEDIUM, score=5,
                 kps=["存储器"], stem="简述存储器的层次结构，并说明各层的作用。",
                 answer="存储层次自上而下：寄存器 → Cache → 主存 → 辅存。\n"
                        "寄存器：CPU 内部，速度最快、容量最小；\n"
                        "Cache：缓存主存中近期使用的数据，弥补 CPU 与主存的速度差；\n"
                        "主存：存放运行中的程序与数据；\n"
                        "辅存：容量大、速度慢，长期保存数据。\n"
                        "越靠上速度越快、容量越小、单位成本越高，整体兼顾速度与容量。",
                 analysis="评分要点：层次顺序、各层作用、速度/容量/成本的权衡。"),
            dict(type=QuestionType.ESSAY, difficulty=Difficulty.HARD, score=10,
                 kps=["CPU"], stem="论述一条指令在 CPU 中的完整执行过程（指令周期）。",
                 answer="1. 取指：根据 PC 从主存取出指令，PC 加 1；\n"
                        "2. 译码：指令译码器分析操作码与地址码；\n"
                        "3. 取操作数：按寻址方式从寄存器或主存取出操作数；\n"
                        "4. 执行：运算器完成算术/逻辑运算；\n"
                        "5. 写回：结果写回寄存器或主存，并置状态标志；\n"
                        "6. 中断响应：如有中断请求则保护现场、转入中断处理。\n"
                        "现代 CPU 通过流水线将上述阶段重叠执行以提高吞吐。",
                 analysis="评分要点：指令周期的各阶段、流水线思想、中断处理。"),
        ],
    },
]


class Command(BaseCommand):
    help = "创建 3 位学科教师 × 各自题库 + 3 个班级（每班 10 名学生）"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="先清空本命令创建的数据再重建")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            self._reset()

        teachers = {}
        for subject in SUBJECTS:
            teachers[subject["subject"]] = self._ensure_teacher(subject)

        # 3 个班级（每班随机 10 名学生）
        self._ensure_classrooms(teachers)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("完成。账号密码统一 Exam@123456："))
        for subject in SUBJECTS:
            self.stdout.write(f"  {subject['teacher']['username']:>14}  ({subject['subject']})")
        self.stdout.write("班级入口：http://127.0.0.1:8000/classrooms/")

    # ------------------------------------------------------------------
    def _ensure_teacher(self, subject) -> User:
        t = subject["teacher"]
        user = User.objects.filter(username=t["username"]).first()
        if user is None:
            user = User.objects.create_user(
                username=t["username"], email=f"{t['username']}@example.com",
                password=PASSWORD, role=Role.TEACHER, real_name=t["real_name"],
                student_no=t["student_no"], subject=subject["subject"],
            )
            self.stdout.write(self.style.SUCCESS(f"  + 教师 {user.username}（{subject['subject']}）"))
        elif not user.subject:
            user.subject = subject["subject"]
            user.save(update_fields=["subject"])
            self.stdout.write(self.style.WARNING(f"  ~ 为教师 {user.username} 绑定学科 {subject['subject']}"))

        # 知识点
        created_points = {}
        for name, code, parent_name in subject["kps"]:
            parent = created_points.get(parent_name) if parent_name else None
            point, created = KnowledgePoint.objects.get_or_create(
                name=name, subject=subject["subject"], parent=parent,
                defaults={"code": code, "created_by": user},
            )
            created_points[name] = point

        # 题目（按题干去重，幂等）
        created = skipped = 0
        for item in subject["questions"]:
            if Question.objects.filter(subject=subject["subject"], stem=item["stem"]).exists():
                skipped += 1
                continue
            options = [{"label": l, "content": c, "is_correct": ok}
                       for l, c, ok in item.get("options", [])]
            data = {
                "type": item["type"], "stem": item["stem"], "difficulty": item["difficulty"],
                "score": item["score"], "analysis": item.get("analysis", ""),
                "subject": subject["subject"], "source": QuestionSource.MANUAL,
                "options": options,
                "knowledge_point_ids": [created_points[n].id for n in item["kps"] if n in created_points],
            }
            if item["type"] in (QuestionType.FILL, QuestionType.SHORT, QuestionType.ESSAY):
                data["reference_answer"] = item["answer"]
            QuestionService.save_question(data=data, user=user)
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"    知识点 {len(created_points)} 个 / 题目新建 {created} 道"
            + (f"（跳过 {skipped} 道已存在）" if skipped else "")))
        return user

    # ------------------------------------------------------------------
    def _ensure_classrooms(self, teachers: dict):
        def ensure_students(start: int, end: int) -> list:
            out = []
            for i in range(start, end + 1):
                user, created = User.objects.get_or_create(
                    username=f"s_{i:03d}",
                    defaults={
                        "email": f"s{i:03d}@example.com",
                        "role": Role.STUDENT,
                        "real_name": f"学生{i:02d}",
                        "student_no": f"2026{i:04d}",
                    },
                )
                # get_or_create 不会设密码：新建的、以及早期建出来密码为空的学生都要补上
                if created or not user.password:
                    user.set_password(PASSWORD)
                    user.save(update_fields=["password"])
                out.append(user)
            return out

        # 3 个行政班，各 15 名学生（s_001~s_045）
        batches = [
            ("计算机 1 班", ensure_students(1, 15)),
            ("计算机 2 班", ensure_students(16, 30)),
            ("计算机 3 班", ensure_students(31, 45)),
        ]
        admin_user = User.objects.filter(is_superuser=True).first() \
            or teachers[SUBJECTS[0]["subject"]]
        admin_classes = []
        for name, batch in batches:
            classroom = Classroom.objects.filter(name=name).first()
            if classroom is None:
                classroom = ClassroomService.create(
                    admin_user, name=name, grade_year="2026级",
                    class_type=ClassType.ADMIN,
                )
                self.stdout.write(self.style.SUCCESS(f"  + 行政班 {name}"))
            classroom.students.clear()  # seed 幂等：成员按批次重建
            ClassroomService.add_students(classroom, [s.id for s in batch])
            self.stdout.write(self.style.SUCCESS(
                f"    {name}：{len(batch)} 名学生"))
            admin_classes.append(classroom)

        # 随机给三个行政班分配班主任（3 位教师、不重复；固定种子，可复现）
        pool = [u for u in (
            User.objects.filter(username=u).first()
            for u in ("teacher", "os_teacher", "net_teacher", "arch_teacher")
        ) if u is not None]
        heads = random.Random(7).sample(pool, min(3, len(pool)))
        for classroom, head in zip(admin_classes, heads):
            if classroom.head_teacher_id != head.id:
                classroom.head_teacher = head
                classroom.save(update_fields=["head_teacher", "updated_at"])
            self.stdout.write(self.style.SUCCESS(
                f"    {classroom.name} 班主任：{head.display_name}"))

        # 学科班（教学班）：不设班主任，学科老师进「授课教师」名单（据此可管理该班）
        for idx, subject in enumerate(SUBJECTS):
            name = f"{subject['subject']} {idx + 1} 班"
            admin_index = idx % len(admin_classes)
            admin_class = admin_classes[admin_index]
            teacher = teachers[subject["subject"]]
            classroom = Classroom.objects.filter(name=name).first()
            if classroom is None:
                classroom = ClassroomService.create(
                    teacher, name=name, grade_year="2026级",
                    subject=subject["subject"], class_type=ClassType.SUBJECT,
                    admin_class_id=admin_class.id, teacher_ids=[teacher.id],
                )
                self.stdout.write(self.style.SUCCESS(
                    f"  + 学科班 {name}（挂靠 {admin_class.name}，授课 {teacher.display_name}）"))
            else:
                # 已有数据：修正类型与挂靠关系，并清掉误设的班主任
                ClassroomService.update(
                    classroom, class_type=ClassType.SUBJECT,
                    admin_class_id=admin_class.id, head_teacher_id=None,
                )
                classroom.teachers.add(teacher)
                self.stdout.write(self.style.SUCCESS(
                    f"    {name} 已修正为学科班（挂靠 {admin_class.name}，不设班主任）"))
            batch = batches[admin_index][1][:10]
            classroom.students.clear()  # seed 幂等：学科班成员按行政班前 10 人重建
            ClassroomService.add_students(classroom, [s.id for s in batch])
            self.stdout.write(self.style.SUCCESS(f"    {name}：{len(batch)} 名学生"))

    # ------------------------------------------------------------------
    def _reset(self):
        for subject in SUBJECTS:
            Question.objects.filter(subject=subject["subject"]).delete()
            KnowledgePoint.objects.filter(subject=subject["subject"]).delete()
            Classroom.objects.filter(subject=subject["subject"]).delete()
        Classroom.objects.filter(
            name__in=("计算机 1 班", "计算机 2 班", "计算机 3 班"),
        ).delete()
        for subject in SUBJECTS:
            User.objects.filter(username=subject["teacher"]["username"]).delete()
        self.stdout.write(self.style.WARNING("已清空本命令创建的学科数据"))
