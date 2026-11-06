"""
初始化演示题库。

用法：
    python manage.py seed_questions            # 建知识点树 + 18 道各题型样题
    python manage.py seed_questions --reset    # 先清空已有演示数据（按学科）

默认造的是《数据结构》的知识点树与样题，覆盖全部六种题型，
方便直接体验组卷 / AI 出题 / 统计等功能。
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.common.constants import Difficulty, QuestionType
from apps.questions.constants import QuestionSource
from apps.questions.models import KnowledgePoint, Question
from apps.questions.services import QuestionService
from apps.users.models import User

SUBJECT = "数据结构"

# (名称, 编号, 父节点名称)
KNOWLEDGE_TREE = [
    ("线性表", "CS-DS-01", None),
    ("顺序表", "CS-DS-01-1", "线性表"),
    ("链表", "CS-DS-01-2", "线性表"),
    ("栈与队列", "CS-DS-02", None),
    ("栈", "CS-DS-02-1", "栈与队列"),
    ("队列", "CS-DS-02-2", "栈与队列"),
    ("树", "CS-DS-03", None),
    ("二叉树", "CS-DS-03-1", "树"),
    ("二叉搜索树", "CS-DS-03-2", "树"),
    ("哈夫曼树", "CS-DS-03-3", "树"),
    ("图", "CS-DS-04", None),
    ("图的遍历", "CS-DS-04-1", "图"),
    ("最短路径", "CS-DS-04-2", "图"),
    ("排序", "CS-DS-05", None),
    ("插入排序", "CS-DS-05-1", "排序"),
    ("快速排序", "CS-DS-05-2", "排序"),
    ("归并排序", "CS-DS-05-3", "排序"),
    ("查找", "CS-DS-06", None),
    ("二分查找", "CS-DS-06-1", "查找"),
    ("哈希表", "CS-DS-06-2", "查找"),
]

# 每道题：题型 / 难度 / 分值 / 知识点 / 题干 / 选项 / 答案 / 解析
QUESTIONS = [
    # ---------------- 单选题 ----------------
    {
        "type": QuestionType.SINGLE, "difficulty": Difficulty.EASY, "score": 2,
        "knowledge_points": ["顺序表"],
        "stem": "在长度为 n 的顺序表中，在第 i 个位置（1 ≤ i ≤ n+1）插入一个元素，"
                "平均需要移动的元素个数约为（　）。",
        "options": [("A", "n/2", True), ("B", "n", False), ("C", "1", False), ("D", "n²/2", False)],
        "analysis": "插入位置等概率分布时，平均移动次数为 n/2，时间复杂度 O(n)。",
    },
    {
        "type": QuestionType.SINGLE, "difficulty": Difficulty.MEDIUM, "score": 2,
        "knowledge_points": ["栈"],
        "stem": "若元素的入栈顺序为 1、2、3、4，则下列哪个序列**不可能**是出栈序列？（　）",
        "options": [("A", "4、3、2、1", False), ("B", "1、2、3、4", False),
                    ("C", "3、1、2、4", True), ("D", "2、4、3、1", False)],
        "analysis": "若 3 先出栈，说明 1、2 已在栈中且 1 在下、2 在上，因此 2 必须先于 1 出栈，"
                    "故 3、1、2、4 不可能。",
    },
    {
        "type": QuestionType.SINGLE, "difficulty": Difficulty.HARD, "score": 2,
        "knowledge_points": ["快速排序"],
        "stem": "对含 n 个元素的无序序列进行快速排序，最坏情况下的时间复杂度是（　）。",
        "options": [("A", "O(n log n)", False), ("B", "O(n)", False),
                    ("C", "O(n²)", True), ("D", "O(log n)", False)],
        "analysis": "当每次划分都极度不平衡（如已基本有序且取首元素为基准）时，"
                    "递归深度退化到 n，时间复杂度为 O(n²)。",
    },
    {
        "type": QuestionType.SINGLE, "difficulty": Difficulty.MEDIUM, "score": 2,
        "knowledge_points": ["二分查找"],
        "stem": "对长度为 1024 的有序表进行二分查找，最多需要比较的次数是（　）。",
        "options": [("A", "10", False), ("B", "11", True), ("C", "1024", False), ("D", "512", False)],
        "analysis": "二分查找的判定树高度为 ⌊log₂n⌋ + 1 = 10 + 1 = 11。",
    },
    # ---------------- 多选题 ----------------
    {
        "type": QuestionType.MULTIPLE, "difficulty": Difficulty.MEDIUM, "score": 3,
        "knowledge_points": ["队列", "栈"],
        "stem": "下列关于栈和队列的说法中，正确的有（　）。",
        "options": [("A", "栈是后进先出（LIFO）的结构", True),
                    ("B", "队列是先进先出（FIFO）的结构", True),
                    ("C", "循环队列可以缓解顺序队列的假溢出问题", True),
                    ("D", "栈和队列都只能顺序存储，不能用链式存储", False)],
        "analysis": "栈和队列既可以用顺序存储也可以用链式存储，D 错误。",
    },
    {
        "type": QuestionType.MULTIPLE, "difficulty": Difficulty.HARD, "score": 3,
        "knowledge_points": ["排序"],
        "stem": "下列排序算法中，属于**稳定排序**的有（　）。",
        "options": [("A", "插入排序", True), ("B", "归并排序", True),
                    ("C", "快速排序", False), ("D", "堆排序", False)],
        "analysis": "插入排序与归并排序稳定；快速排序、堆排序、选择排序都是不稳定的。",
    },
    # ---------------- 判断题 ----------------
    {
        "type": QuestionType.JUDGE, "difficulty": Difficulty.EASY, "score": 1,
        "knowledge_points": ["图的遍历"],
        "stem": "深度优先搜索（DFS）通常借助队列实现，广度优先搜索（BFS）通常借助栈实现。",
        "answer": "B",
        "analysis": "说法相反：DFS 借助栈（或递归），BFS 借助队列。",
    },
    {
        "type": QuestionType.JUDGE, "difficulty": Difficulty.EASY, "score": 1,
        "knowledge_points": ["链表"],
        "stem": "在单链表中，删除某个节点时必须从头结点开始查找其前驱节点。",
        "answer": "A",
        "analysis": "单链表只能单向遍历，要删除节点必须知道其前驱，因此需要从头查找；"
                    "这也是双向链表的一个优势。",
    },
    {
        "type": QuestionType.JUDGE, "difficulty": Difficulty.MEDIUM, "score": 1,
        "knowledge_points": ["哈夫曼树"],
        "stem": "哈夫曼树中不存在度为 1 的节点。",
        "answer": "A",
        "analysis": "哈夫曼树的构造过程每次都合并两棵权值最小的树，因此每个非叶节点都有两个"
                    "子节点，不存在度 1 的节点。",
    },
    {
        "type": QuestionType.JUDGE, "difficulty": Difficulty.MEDIUM, "score": 1,
        "knowledge_points": ["哈希表"],
        "stem": "哈希表的查找时间复杂度在最坏情况下仍然是 O(1)。",
        "answer": "B",
        "analysis": "当所有关键字都冲突到同一个槽位时，查找退化为线性查找，最坏为 O(n)。",
    },
    # ---------------- 填空题 ----------------
    {
        "type": QuestionType.FILL, "difficulty": Difficulty.MEDIUM, "score": 4,
        "knowledge_points": ["二叉树"],
        "stem": "一棵完全二叉树有 1000 个节点，则它的高度为 ______，"
                "叶子节点个数为 ______。",
        "answer": "10\n500",
        "analysis": "⌈log₂(1000+1)⌉ = 10；完全二叉树叶子数为 ⌈n/2⌉ = 500。",
    },
    {
        "type": QuestionType.FILL, "difficulty": Difficulty.HARD, "score": 4,
        "knowledge_points": ["最短路径"],
        "stem": "Dijkstra 算法要求图中所有边的权值 ______，其时间复杂度（邻接矩阵实现）为 ______。",
        "answer": "非负\nO(n²)",
        "analysis": "Dijkstra 不能处理负权边；邻接矩阵实现的朴素版本时间复杂度为 O(n²)。",
    },
    {
        "type": QuestionType.FILL, "difficulty": Difficulty.MEDIUM, "score": 4,
        "knowledge_points": ["二叉搜索树"],
        "stem": "对二叉搜索树进行 ______ 遍历，可以得到一个关键字的递增有序序列。",
        "answer": "中序",
        "analysis": "二叉搜索树的性质：左子树 < 根 < 右子树，因此中序遍历得到有序序列。",
    },
    # ---------------- 简答题 ----------------
    {
        "type": QuestionType.SHORT, "difficulty": Difficulty.MEDIUM, "score": 5,
        "knowledge_points": ["快速排序"],
        "stem": "简述快速排序的基本思想，并说明它在什么情况下会退化为最坏时间复杂度。",
        "answer": "基本思想（分治法）：\n"
                  "1. 从序列中选取一个元素作为基准（pivot）；\n"
                  "2. 将序列划分为两部分，左侧元素均不大于基准，右侧均不小于基准；\n"
                  "3. 对左右两部分递归执行上述过程。\n"
                  "退化条件：当每次划分极度不平衡（例如序列已基本有序且固定选取首元素为基准）时，"
                  "递归深度达到 n，时间复杂度退化为 O(n²)。",
        "analysis": "评分要点：分治思想、基准选取、划分过程、递归、退化条件。",
    },
    {
        "type": QuestionType.SHORT, "difficulty": Difficulty.MEDIUM, "score": 5,
        "knowledge_points": ["栈", "队列"],
        "stem": "请说明栈和队列的主要区别，并各举一个典型应用场景。",
        "answer": "区别：栈是后进先出（LIFO），只能在栈顶插入和删除；"
                  "队列是先进先出（FIFO），在队尾插入、队头删除。\n"
                  "典型应用：栈可用于表达式求值、函数调用栈、括号匹配；"
                  "队列可用于层序遍历、任务调度、缓冲区管理。",
        "analysis": "评分要点：LIFO/FIFO 的区别、操作位置的区别、各自一个应用场景。",
    },
    {
        "type": QuestionType.SHORT, "difficulty": Difficulty.HARD, "score": 5,
        "knowledge_points": ["图的遍历", "最短路径"],
        "stem": "比较 Dijkstra 算法与 Floyd 算法在求解最短路径问题时的适用场景。",
        "answer": "Dijkstra 算法：求**单源**最短路径，即从一个源点到其余所有顶点的最短路径；"
                  "要求边权非负，常用邻接矩阵实现时复杂度 O(n²)，用堆优化可到 O(m log n)。\n"
                  "Floyd 算法：求**多源**最短路径，即任意两点之间的最短路径；"
                  "允许负权边（但不能有负权回路），基于动态规划，复杂度 O(n³)。\n"
                  "选择依据：只需要一个源点时用 Dijkstra；需要任意两点间距离、或图规模较小时用 Floyd。",
        "analysis": "评分要点：单源 vs 多源、复杂度、负权限制、选择依据。",
    },
    # ---------------- 论述题 ----------------
    {
        "type": QuestionType.ESSAY, "difficulty": Difficulty.HARD, "score": 10,
        "knowledge_points": ["排序"],
        "stem": "论述比较插入排序、快速排序、归并排序和堆排序四种算法的时间复杂度、空间复杂度、"
                "稳定性，并结合实际工程场景说明如何选择。",
        "answer": "1. 时间复杂度：插入排序平均 O(n²)、最好 O(n)；快排平均 O(n log n)、最坏 O(n²)；"
                  "归并排序稳定在 O(n log n)；堆排序稳定在 O(n log n)。\n"
                  "2. 空间复杂度：插入排序 O(1)；快排 O(log n)（递归栈）；归并需 O(n) 辅助数组；"
                  "堆排序 O(1)。\n"
                  "3. 稳定性：插入排序、归并排序稳定；快排、堆排序不稳定。\n"
                  "4. 工程选择：数据量小或基本有序时用插入排序（常数小）；"
                  "对稳定性有要求（如多关键字排序）用归并排序；"
                  "内存受限且要求最坏情况可控时用堆排序；"
                  "通用场景优先快排，实际实现常做「三数取中 + 小区间插入排序」的混合优化。",
        "analysis": "评分要点：三个维度的对比、对稳定性原因的理解、结合实际场景的取舍论证。",
    },
    {
        "type": QuestionType.ESSAY, "difficulty": Difficulty.EXPERT, "score": 10,
        "knowledge_points": ["哈希表", "查找"],
        "stem": "论述哈希表的基本原理、常见的冲突解决方法，并分析装载因子对性能的影响。",
        "answer": "1. 基本原理：通过哈希函数将关键字映射到存储位置，理想情况下查找、插入、"
                  "删除的平均时间复杂度均为 O(1)。\n"
                  "2. 冲突解决：\n"
                  "   （1）开放地址法——线性探测、二次探测、双重散列；\n"
                  "   （2）链地址法——每个槽位挂一个链表或红黑树（如 Java HashMap）；\n"
                  "   （3）再哈希法——换一个哈希函数重新计算；\n"
                  "   （4）建立公共溢出区。\n"
                  "3. 装载因子 α = 表中元素数 / 表长：α 越大冲突越严重，平均查找长度越长；"
                  "链地址法下平均查找长度约为 1 + α/2，开放地址法下性能对 α 极其敏感。\n"
                  "4. 工程实践：通常设置阈值（如 0.75）触发扩容 rehash，以空间换时间；"
                  "同时要求哈希函数分布均匀，避免大量关键字聚集。",
        "analysis": "评分要点：原理、四类冲突解决方法、装载因子的定量影响、扩容策略。",
    },
    {
        "type": QuestionType.ESSAY, "difficulty": Difficulty.EXPERT, "score": 10,
        "knowledge_points": ["图的遍历"],
        "stem": "论述深度优先搜索与广度优先搜索的区别，并说明它们各自能解决哪些经典问题。",
        "answer": "1. 实现方式：DFS 用栈或递归，沿一条路径深入到底再回溯；"
                  "BFS 用队列，按层逐层扩展。\n"
                  "2. 复杂度：两者在邻接表下均为 O(V + E)，邻接矩阵下均为 O(V²)。\n"
                  "3. 空间：DFS 最坏 O(V)（递归深度）；BFS 需要保存整层节点，最坏 O(V)。\n"
                  "4. 经典应用：\n"
                  "   DFS——连通性判断、拓扑排序、强连通分量（Tarjan / Kosaraju）、"
                  "割点与桥、回溯类问题（迷宫、八皇后）；\n"
                  "   BFS——无权图单源最短路径、层序遍历、二分图判定、"
                  "状态空间搜索（如最短步数问题）。\n"
                  "5. 选择原则：求「最短步数」优先 BFS；需要枚举所有路径或做回溯时用 DFS。",
        "analysis": "评分要点：实现结构、复杂度、空间、经典应用、选择原则。",
    },
]


class Command(BaseCommand):
    help = "创建《数据结构》演示知识点树与六种题型的样题"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="先清空该学科下的演示数据")

    @transaction.atomic
    def handle(self, *args, **options):
        teacher = (
            User.objects.filter(username="teacher").first()
            or User.objects.filter(role="teacher").first()
            or User.objects.filter(is_superuser=True).first()
        )

        if options["reset"]:
            deleted = Question.objects.filter(subject=SUBJECT).delete()[0]
            KnowledgePoint.objects.filter(subject=SUBJECT).delete()
            self.stdout.write(self.style.WARNING(f"已清空 {SUBJECT} 下的 {deleted} 条相关记录"))

        # ---------------- 知识点 ----------------
        created_points = {}
        for name, code, parent_name in KNOWLEDGE_TREE:
            parent = created_points.get(parent_name) if parent_name else None
            point, _ = KnowledgePoint.objects.get_or_create(
                name=name, subject=SUBJECT, parent=parent,
                defaults={"code": code, "created_by": teacher},
            )
            if not point.code:
                point.code = code
                point.save(update_fields=["code"])
            created_points[name] = point
        self.stdout.write(self.style.SUCCESS(f"知识点：{len(created_points)} 个"))

        # ---------------- 题目 ----------------
        created_count, skipped = 0, 0
        for item in QUESTIONS:
            if Question.objects.filter(subject=SUBJECT, stem=item["stem"]).exists():
                skipped += 1
                continue

            options = [
                {"label": label, "content": content, "is_correct": is_correct}
                for label, content, is_correct in item.get("options", [])
            ]
            data = {
                "type": item["type"],
                "stem": item["stem"],
                "difficulty": item["difficulty"],
                "score": item["score"],
                "analysis": item["analysis"],
                "subject": SUBJECT,
                "source": QuestionSource.MANUAL,
                "options": options,
                "knowledge_point_ids": [
                    created_points[name].id for name in item["knowledge_points"]
                    if name in created_points
                ],
            }
            if item["type"] in (QuestionType.FILL, QuestionType.SHORT, QuestionType.ESSAY):
                data["reference_answer"] = item["answer"]

            QuestionService.save_question(data=data, user=teacher)
            created_count += 1

        self.stdout.write(self.style.SUCCESS(f"题目：新建 {created_count} 道" + (f"，跳过 {skipped} 道已存在" if skipped else "")))
        self.stdout.write("")
        self.stdout.write("浏览入口：http://127.0.0.1:8000/question-bank/")
        self.stdout.write("AI 出题：http://127.0.0.1:8000/question-bank/ai-generate/")
