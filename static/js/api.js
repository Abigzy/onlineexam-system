/* ============================================================
 * 前端 API 客户端 —— 页面通过 RESTful 接口拿数据，不重复写业务逻辑
 *
 * 后端统一响应体：{ code, message, data }
 * 这里把它抹平成「成功直接给 data，失败直接抛 Error(message)」，
 * 页面里只需要 try/catch 一次。
 * ============================================================ */

(function (global) {
    'use strict';

    const BASE = '/api/v1';

    function getCookie(name) {
        const match = document.cookie.match(new RegExp('(^|;\\s*)' + name + '=([^;]*)'));
        return match ? decodeURIComponent(match[2]) : '';
    }

    function buildUrl(path, params) {
        // 约定：页面里的 path 一律相对 /api/v1（如 "/papers/"）。
        // 若误写了全路径（"/api/v1/..."），这里兜底不重复拼，避免 /api/v1/api/v1/... 的 404
        const base = path.startsWith("/api/") ? "" : BASE;
        const url = new URL(base + path, global.location.origin);
        if (params) {
            Object.keys(params).forEach(function (key) {
                const value = params[key];
                if (value === undefined || value === null || value === '') return;
                if (Array.isArray(value)) {
                    value.forEach(function (item) { url.searchParams.append(key, item); });
                } else {
                    url.searchParams.append(key, value);
                }
            });
        }
        return url.toString();
    }

    async function request(path, options) {
        options = options || {};
        const method = (options.method || 'GET').toUpperCase();
        const headers = { 'Accept': 'application/json' };

        let body;
        if (options.formData) {
            body = options.formData;                        // 文件上传：浏览器自己设 Content-Type
        } else if (options.body !== undefined && method !== 'GET') {
            headers['Content-Type'] = 'application/json';
            body = JSON.stringify(options.body);
        }

        if (method !== 'GET' && method !== 'HEAD') {
            headers['X-CSRFToken'] = getCookie('csrftoken');
        }

        const response = await fetch(buildUrl(path, options.params), {
            method: method,
            headers: headers,
            body: body,
            credentials: 'same-origin',
        });

        // 401：会话过期，回登录页
        if (response.status === 401) {
            const next = encodeURIComponent(global.location.pathname + global.location.search);
            global.location.href = '/login/?next=' + next;
            throw new Error('登录状态已失效');
        }

        let payload;
        try {
            payload = await response.json();
        } catch (err) {
            throw new Error('服务器返回了无法解析的内容（HTTP ' + response.status + '）');
        }

        if (payload && typeof payload.code !== 'undefined') {
            if (payload.code !== 0) {
                const error = new Error(payload.message || '请求失败');
                error.code = payload.code;
                error.data = payload.data;
                throw error;
            }
            return payload.data;
        }
        return payload;
    }

    const API = {
        base: BASE,
        get: function (path, params) { return request(path, { params: params }); },
        post: function (path, body) { return request(path, { method: 'POST', body: body }); },
        put: function (path, body) { return request(path, { method: 'PUT', body: body }); },
        patch: function (path, body) { return request(path, { method: 'PATCH', body: body }); },
        del: function (path, body) { return request(path, { method: 'DELETE', body: body }); },
        upload: function (path, formData) { return request(path, { method: 'POST', formData: formData }); },
        getCookie: getCookie,

        // ---------- 下载文件（导出、模板） ----------
        download: function (path, params) {
            global.location.href = buildUrl(path, params);
        },
    };

    /* ============================================================
     * UI 小工具
     * ============================================================ */
    const UI = {
        toast: function (message, type) {
            const container = document.getElementById('toast-container');
            if (!container) { global.alert(message); return; }
            const el = document.createElement('div');
            el.className = 'alert alert-' + (type || 'info') + ' shadow-sm mb-2 py-2 small';
            el.textContent = message;
            container.appendChild(el);
            setTimeout(function () { el.remove(); }, 3600);
        },
        error: function (message) {
            // 兼容直接传 Error / 异常对象：API 层抛的就是 Error，直接塞进 toast 会
            // 显示成「Error: 账号已被停用…」这种带前缀的别扭文案。
            const text = (message && typeof message === 'object' && message.message)
                ? message.message
                : message;
            UI.toast(text, 'danger');
        },
        success: function (message) { UI.toast(message, 'success'); },

        /** 把 DRF 的字段级错误渲染成「字段：原因」的一行文字。
         *
         * DRF 的错误可能是**多层嵌套**的，比如选项列表校验失败时：
         *   `{"options": [{}, {}, {"content": ["该字段不能为空。"]}]}`
         * 老实现直接 `String(item)`，渲染出来是一串 `[object Object]`（BUG-02）。
         * 这里递归展平：列表项标注序号（用户要知道是第几行），
         * 内层对象只取错误文案、不再重复字段名。
         */
        errorText: function (err) {
            const flatten = function (value) {
                if (value === null || value === undefined) return '';
                if (typeof value === 'string') return value;
                if (Array.isArray(value)) {
                    return value.map(function (item, index) {
                        const text = flatten(item);
                        if (!text) return '';
                        // 只有多项时才标序号——单项加「第 1 项」反而啰嗦
                        return value.length > 1 ? '第 ' + (index + 1) + ' 项：' + text : text;
                    }).filter(Boolean).join('；');
                }
                if (typeof value === 'object') {
                    // 内层不再拼字段名：外层已经说了是 options，再套 "content：" 太绕
                    return Object.keys(value).map(function (key) {
                        return flatten(value[key]);
                    }).filter(Boolean).join('；');
                }
                return String(value);
            };

            if (err && err.data && typeof err.data === 'object') {
                const parts = Object.keys(err.data).map(function (key) {
                    const text = flatten(err.data[key]);
                    return text ? key + '：' + text : '';
                }).filter(Boolean);
                if (parts.length) return parts.join('　');
            }
            return (err && err.message) || '请求失败';
        },

        setBusy: function (button, busy, busyText) {
            if (!button) return;
            if (busy) {
                button.dataset.originalText = button.innerHTML;
                button.disabled = true;
                button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>'
                    + (busyText || '处理中…');
            } else {
                button.disabled = false;
                if (button.dataset.originalText) button.innerHTML = button.dataset.originalText;
            }
        },
    };

    const TYPE_LABELS = {
        single: '单选题', multiple: '多选题', judge: '判断题',
        fill: '填空题', short: '简答题', essay: '论述题',
    };
    const DIFFICULTY_LABELS = { 1: '简单', 2: '中等', 3: '困难', 4: '挑战' };
    const DIFFICULTY_BADGES = {
        1: 'bg-success-subtle text-success-emphasis',
        2: 'bg-primary-subtle text-primary-emphasis',
        3: 'bg-warning-subtle text-warning-emphasis',
        4: 'bg-danger-subtle text-danger-emphasis',
    };
    const SOURCE_LABELS = { manual: '手动录入', ai: 'AI 生成', import: '批量导入' };

    global.API = API;
    global.UI = UI;
    global.LABELS = {
        TYPE: TYPE_LABELS,
        DIFFICULTY: DIFFICULTY_LABELS,
        DIFFICULTY_BADGE: DIFFICULTY_BADGES,
        SOURCE: SOURCE_LABELS,
    };
})(window);
