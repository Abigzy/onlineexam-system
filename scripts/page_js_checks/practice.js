/**
 * 页面内联 JS 的逻辑校验（每日练习）
 *
 * 为什么需要它：`check_inline_js.py` 只能保证「语法不错」，保证不了「交互真的能用」。
 * 曾经 `PracticeModal` 卡片把选择题渲染成了纯文本列表、主观题根本没有输入框，
 * 页面照样 200、语法照样 PASS —— 只有把脚本真的执行一遍才能发现。
 *
 * 做法：从模板里抠出内联脚本，在最小 DOM stub 下用 new Function 执行，
 * 把 IIFE 末尾的 `load(true)` 换成导出纯函数，然后直接调用验证。
 *
 * 用法：python scripts/check_practice_js.py   （内部调用 node）
 */
const fs = require('fs');

const html = fs.readFileSync('templates/recommend/practice.html', 'utf8');
const blocks = [...html.matchAll(/<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let src = blocks.find(b => b.includes('STUDENT_ID'));
if (!src) { console.error('未找到内联脚本'); process.exit(1); }
// 模板占位符替换成真实值（等价于 Django 渲染后的产物）
src = src.replace('{{ student_id }}', '3').replace("'{{ subject|escapejs }}'", "'操作系统'");

// 把 IIFE 结尾的 load(true) 换成导出被测纯函数
const before = src;
src = src.replace(/load\(true\);\s*\}\)\(\);/, [
    '    load(true);',
    '    return { answerArea, cardHtml, currentSubject, footerHtml, judge, normalize, readAnswer, starterBanner, kpiRow, starterKnowledgeCount };',
    '})();',
].join('\n'));
if (src === before) { console.error('导出注入失败：未匹配到 load(true) 结尾'); process.exit(1); }

function stubEl() {
    const el = {
        innerHTML: '', textContent: '', value: '', disabled: false, style: {}, children: [],
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, removeAttribute() {}, setAttribute() {}, appendChild() {},
        insertAdjacentHTML() {}, removeChild() {}, closest() { return null; },
        querySelector() { return null; }, querySelectorAll() { return []; }, getAttribute() { return null; },
    };
    return el;
}

const sandbox = {
    document: {
        // 按 id 缓存：页面里「先写 select.value，再读 currentSubject()」是跨调用的，
        // 每次都返回新 stub 会让这类状态读不回来
        //
        // 学科下拉预置一个 option：服务端渲染的就是「已加入的学科」，
        // 首屏脚本只在这个 option 存在时才把 URL 上的学科选中
        getElementById: (() => {
            const els = {};
            const seed = { 'subject-select': { options: [{ value: '操作系统' }] } };
            return (id) => (els[id] || (els[id] = Object.assign(stubEl(), seed[id] || {})));
        })(),
        createElement: () => stubEl(),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
    },
    window: {},
    UI: { setBusy() {}, success() {}, error() {}, errorText: (e) => String(e) },
    API: { get: async () => ({}), post: async () => ({}) },
    setTimeout, console,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

// 注意：src 开头有换行，直接拼接会触发 ASI（return; ），必须包一层括号
const factory = new Function(
    'document', 'window', 'UI', 'API', 'console',
    'return (' + src.trim().replace(/;$/, '') + ')\n'
);
const mod = factory(sandbox.document, sandbox.window, sandbox.UI, sandbox.API, console);

let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass += 1; console.log('  ✓ ' + name); }
    else { fail += 1; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
}

console.log('=== 作答控件渲染 ===');
const single = { type: 'single', type_display: '单选题', options: [{label:'A',content:'甲'},{label:'B',content:'乙'}] };
const multiple = { type: 'multiple', type_display: '多选题', options: [{label:'A',content:'甲'},{label:'B',content:'乙'},{label:'C',content:'丙'}] };
const judge = { type: 'judge', type_display: '判断题', options: [{label:'A',content:'正确'},{label:'B',content:'错误'}] };
const fill = { type: 'fill', type_display: '填空题', options: [] };
const short = { type: 'short', type_display: '简答题', options: [] };
const essay = { type: 'essay', type_display: '论述题', options: [] };

const htmlSingle = mod.answerArea(single);
const htmlMultiple = mod.answerArea(multiple);
const htmlJudge = mod.answerArea(judge);
const htmlFill = mod.answerArea(fill);
const htmlShort = mod.answerArea(short);
const htmlEssay = mod.answerArea(essay);

check('单选题渲染可点选项行', (htmlSingle.match(/class="option-row"/g) || []).length === 2, htmlSingle.slice(0, 120));
check('单选题不是多选', htmlSingle.includes('data-multiple="false"'));
check('多选题标记为多选', htmlMultiple.includes('data-multiple="true"'));
check('多选题 3 个选项', (htmlMultiple.match(/class="option-row"/g) || []).length === 3);
check('判断题有正确/错误两个选项', htmlJudge.includes('正确') && htmlJudge.includes('错误'));
check('判断题缺选项时用内置选项兜底',
    (mod.answerArea({ type: 'judge', options: [] }).match(/class="option-row"/g) || []).length === 2);
check('填空题有输入框', htmlFill.includes('<textarea') && htmlFill.includes('每个空写一行'));
check('简答题有输入框', htmlShort.includes('<textarea'));
check('论述题有输入框且行数更多', htmlEssay.includes('rows="10"'), htmlEssay.slice(0, 120));

console.log('=== 判题口径 ===');
function J(item, value) { return mod.judge(item, value); }

check('单选选对 → right',
    J({ type: 'single', reference_answer: 'C' }, 'C').status === 'right');
check('单选选错 → wrong',
    J({ type: 'single', reference_answer: 'C' }, 'A').status === 'wrong');
check('多选全对（顺序无关）→ right',
    J({ type: 'multiple', reference_answer: 'ABD' }, 'BAD').status === 'right');
check('多选漏选 → wrong',
    J({ type: 'multiple', reference_answer: 'ABD' }, 'AB').status === 'wrong');
check('多选多选了 → wrong',
    J({ type: 'multiple', reference_answer: 'ABD' }, 'ABCD').status === 'wrong');
check('判断题答对 → right',
    J({ type: 'judge', reference_answer: 'A' }, 'A').status === 'right');
check('未作答 → wrong',
    J({ type: 'single', reference_answer: 'A' }, '').status === 'wrong');
check('填空全对 → right',
    J({ type: 'fill', reference_answer: '栈\n队列' }, '栈\n队列').status === 'right');
check('填空忽略空格大小写 → right',
    J({ type: 'fill', reference_answer: 'Stack' }, ' stack ').status === 'right');
check('填空少答一个空 → wrong',
    J({ type: 'fill', reference_answer: '栈\n队列' }, '栈').status === 'wrong');
check('填空答错 → wrong',
    J({ type: 'fill', reference_answer: '栈\n队列' }, '栈\n树').status === 'wrong');
check('填空忽略标点差异 → right',
    J({ type: 'fill', reference_answer: '先进后出' }, '先进后出，').status === 'right');
check('简答题不假装机器判 → self',
    J({ type: 'short', reference_answer: '要点一\n要点二' }, '我的作答').status === 'self');
check('论述题不假装机器判 → self',
    J({ type: 'essay', reference_answer: '要点' }, '我的作答').status === 'self');

console.log('=== KPI 文案（starter / normal）===');
const starterData = {
    mode: 'starter', target_difficulty: 2,
    items: [{ knowledge_point: '进程与线程' }, { knowledge_point: '死锁' },
            { knowledge_point: '进程与线程' }],
    summary: {
        mode: 'starter', returned: 3, candidate_count: 12, starter_knowledge_points: 2,
        avg_match: 0.8, cf_available: false, answered_count: 0,
    },
};
const starterHtml = mod.kpiRow(starterData);
check('starter KPI 显示「入门练习 · 覆盖 2 个知识点」',
    starterHtml.includes('入门练习') && starterHtml.includes('覆盖 2 个知识点'),
    starterHtml.slice(0, 200));
check('starter KPI 不出现「薄弱知识点」', !starterHtml.includes('薄弱知识点'));
check('starter 协同过滤说明不误导',
    starterHtml.includes('入门练习不依赖协同过滤'));

const normalData = {
    mode: 'normal', target_difficulty: 3, items: [],
    summary: {
        mode: 'normal', returned: 6, candidate_count: 10, weak_point_count: 3,
        avg_match: 0.7, cf_available: true, answered_count: 40,
    },
};
const normalHtml = mod.kpiRow(normalData);
check('normal KPI 保持「薄弱知识点 / 驱动本次推荐」',
    normalHtml.includes('薄弱知识点') && normalHtml.includes('驱动本次推荐'));
check('normal KPI 不出现 starter 文案', !normalHtml.includes('入门练习'));

check('starter 覆盖数缺失时按 items 去重兜底',
    mod.starterKnowledgeCount({
        summary: {},
        items: [{ knowledge_point: 'A' }, { knowledge_point: 'A' }, { knowledge_point: 'B' }],
    }) === 2);

console.log('=== 页脚口径（starter / normal）===');
const starterFooter = mod.footerHtml(starterData);
check('starter 页脚说明入门口径且不含「薄弱」',
    starterFooter.includes('入门练习按难度梯度与知识点覆盖抽取') && !starterFooter.includes('薄弱'),
    starterFooter);
check('starter 页脚不再出现薄弱度公式',
    !starterFooter.includes('知识点薄弱度') && !starterFooter.includes('证据充分度'));

const normalFooter = mod.footerHtml(normalData);
check('normal 页脚仍写薄弱度 × 证据充分度公式',
    normalFooter.includes('知识点薄弱度') && normalFooter.includes('证据充分度'),
    normalFooter.slice(0, 200));
check('normal 页脚不出现 starter 措辞',
    !normalFooter.includes('入门练习'));

console.log('=== 首屏学科入口与提示条 ===');
check('首屏 currentSubject() 等于 URL 上的 INITIAL_SUBJECT',
    mod.currentSubject() === '操作系统', mod.currentSubject());
check('starter 提示条不含「薄弱」', !mod.starterBanner().includes('薄弱'), mod.starterBanner());
check('starter 提示条说明按掌握度推荐',
    mod.starterBanner().includes('按你的掌握度做个性化推荐'));
check('starter KPI 用「该学科答题」而非「已判分题数」',
    starterHtml.includes('该学科答题') && !starterHtml.includes('已判分题数'));
check('normal KPI 保持「已判分题数」',
    normalHtml.includes('已判分题数') && !normalHtml.includes('该学科答题'));

console.log('\n通过 ' + pass + ' 项，失败 ' + fail + ' 项');
process.exit(fail ? 1 : 0);
