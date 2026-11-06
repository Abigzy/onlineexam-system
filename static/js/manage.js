/* ============================================================
 * 管理端「用户治理」交互
 *
 * 只做两件危险动作 + 一个密码生成器，业务逻辑一律在后端：
 *   * 停用 / 启用  → POST /api/v1/users/{id}/toggle-active/
 *   * 重置密码     → POST /api/v1/users/{id}/reset-password/
 * 复用 api.js 的 API / UI（会话 + CSRF，失败自动 toast）。
 *
 * 安全纪律：
 *   * 新密码明文只写进 DOM 一次，关闭模态框立即清空（AC-21）；
 *   * 危险动作一律 POST + 二次确认（AC-24 / AC-37）。
 * ============================================================ */

(function (global) {
    'use strict';

    var LOWER = 'abcdefghijkmnopqrstuvwxyz';   // 去掉 l
    var UPPER = 'ABCDEFGHJKLMNPQRSTUVWXYZ';    // 去掉 I、O
    var DIGITS = '23456789';                   // 去掉 0、1
    var ALL = LOWER + UPPER + DIGITS;

    function randomInt(max) {
        if (global.crypto && global.crypto.getRandomValues) {
            var buffer = new Uint32Array(1);
            global.crypto.getRandomValues(buffer);
            return buffer[0] % max;
        }
        return Math.floor(Math.random() * max);
    }

    function pick(source) {
        return source.charAt(randomInt(source.length));
    }

    /** 生成 12 位强密码：保证至少各含一个小写、大写、数字。 */
    function generatePassword(length) {
        var size = Math.max(length || 12, 9);
        var chars = [pick(LOWER), pick(UPPER), pick(DIGITS)];
        while (chars.length < size) {
            chars.push(pick(ALL));
        }
        for (var i = chars.length - 1; i > 0; i -= 1) {
            var j = randomInt(i + 1);
            var tmp = chars[i];
            chars[i] = chars[j];
            chars[j] = tmp;
        }
        return chars.join('');
    }

    /* ============================================================
     * 一、新建用户页：随机生成初始密码
     * ============================================================ */
    function bindPasswordGenerator() {
        var button = document.getElementById('btn-gen-password');
        if (!button) return;
        button.addEventListener('click', function () {
            var password = generatePassword(12);
            var first = document.getElementById('id_password');
            var second = document.getElementById('id_password2');
            if (first) {
                first.type = 'text';
                first.value = password;
            }
            if (second) {
                second.type = 'text';
                second.value = password;
            }
            if (global.UI) global.UI.toast('已生成随机密码，请复制后转告用户', 'info');
        });
    }

    /* ============================================================
     * 二、停用 / 启用
     * ============================================================ */
    function bindToggleActive() {
        var modalEl = document.getElementById('toggleModal');
        var confirmBtn = document.getElementById('toggleConfirm');
        if (!modalEl || !confirmBtn) return;

        var pending = null;

        Array.prototype.forEach.call(
            document.querySelectorAll('[data-action="toggle-active"]'),
            function (btn) {
                btn.addEventListener('click', function () {
                    pending = btn;
                    var deactivating = btn.dataset.active === '1';
                    document.getElementById('toggleModalTitle').textContent =
                        deactivating ? '确认停用' : '确认启用';
                    document.getElementById('toggleModalText').textContent = deactivating
                        ? '该账号将立即无法登录，其已有数据（题库、试卷、成绩）保留不变。'
                        : '启用后该账号可以立即登录，历史数据不受影响。';
                    confirmBtn.className = 'btn btn-sm ' + (deactivating ? 'btn-danger' : 'btn-success');
                    confirmBtn.textContent = deactivating ? '确认停用' : '确认启用';
                    global.bootstrap.Modal.getOrCreateInstance(modalEl).show();
                });
            }
        );

        confirmBtn.addEventListener('click', async function () {
            if (!pending) return;
            var userId = pending.dataset.userId;
            global.UI.setBusy(confirmBtn, true, '处理中…');
            try {
                var data = await global.API.post('/users/' + userId + '/toggle-active/', {});
                global.bootstrap.Modal.getOrCreateInstance(modalEl).hide();
                global.UI.success(data && data.is_active ? '账号已启用' : '账号已停用');
                global.setTimeout(function () { global.location.reload(); }, 600);
            } catch (err) {
                global.UI.error(global.UI.errorText(err));
            } finally {
                global.UI.setBusy(confirmBtn, false);
            }
        });
    }

    /* ============================================================
     * 三、重置密码（明文只展示一次）
     * ============================================================ */
    function bindResetPassword() {
        var modalEl = document.getElementById('resetModal');
        var confirmBtn = document.getElementById('resetConfirm');
        if (!modalEl || !confirmBtn) return;

        var target = null;
        var resultBox = document.getElementById('pwd-result');
        var resultText = document.getElementById('pwd-result-text');
        var manualWrap = document.getElementById('pwd-manual-wrap');
        var manualInput = document.getElementById('pwd-manual-input');
        var copyBtn = document.getElementById('pwd-copy');

        function resetModalState() {
            resultBox.classList.add('d-none');
            resultText.textContent = '';
            manualWrap.classList.add('d-none');
            manualInput.value = '';
            confirmBtn.disabled = false;
            confirmBtn.classList.remove('d-none');
        }

        Array.prototype.forEach.call(
            document.querySelectorAll('[data-action="reset-password"]'),
            function (btn) {
                btn.addEventListener('click', function () {
                    target = btn;
                    resetModalState();
                    document.getElementById('resetModalTarget').textContent =
                        '目标账号：' + btn.dataset.username;
                    global.bootstrap.Modal.getOrCreateInstance(modalEl).show();
                });
            }
        );

        Array.prototype.forEach.call(
            document.querySelectorAll('input[name="pwd-mode"]'),
            function (radio) {
                radio.addEventListener('change', function () {
                    manualWrap.classList.toggle('d-none', radio.value !== 'manual' || !radio.checked);
                });
            }
        );

        confirmBtn.addEventListener('click', async function () {
            if (!target) return;
            var mode = document.querySelector('input[name="pwd-mode"]:checked');
            var body = {};
            if (mode && mode.value === 'manual') {
                var value = (manualInput.value || '').trim();
                if (value.length < 6) {
                    global.UI.error('手动输入的密码不能少于 6 位');
                    return;
                }
                body.new_password = value;
            }

            global.UI.setBusy(confirmBtn, true, '重置中…');
            try {
                var data = await global.API.post(
                    '/users/' + target.dataset.userId + '/reset-password/', body
                );
                resultText.textContent = data.password;
                resultBox.classList.remove('d-none');
                confirmBtn.classList.add('d-none');
                global.UI.success('密码已重置，请立即复制新密码');
            } catch (err) {
                global.UI.error(global.UI.errorText(err));
            } finally {
                global.UI.setBusy(confirmBtn, false);
            }
        });

        if (copyBtn) {
            copyBtn.addEventListener('click', function () {
                var text = resultText.textContent || '';
                if (!text) return;
                if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
                    global.navigator.clipboard.writeText(text).then(function () {
                        global.UI.success('已复制到剪贴板');
                    }, function () {
                        global.UI.error('复制失败，请手动选择文本复制');
                    });
                } else {
                    global.UI.error('当前浏览器不支持一键复制，请手动选择文本复制');
                }
            });
        }

        // AC-21：关闭模态框即清空明文，刷新后也不会再出现
        modalEl.addEventListener('hidden.bs.modal', function () {
            resultText.textContent = '';
            manualInput.value = '';
            target = null;
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        bindPasswordGenerator();
        bindToggleActive();
        bindResetPassword();
    });
})(window);
