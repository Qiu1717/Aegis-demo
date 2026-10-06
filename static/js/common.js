/**
 * Aegis (aegis) - 通用工具函数
 * 封装 Fetch 请求、Toast 提示、弹窗组件
 */

// ── API 请求封装 ──────────────────────────────────────────
const API = {
    async request(url, options = {}) {
        const defaultOpts = {
            headers: { "Content-Type": "application/json" },
        };
        const opts = { ...defaultOpts, ...options };
        if (opts.body && typeof opts.body === "object") {
            opts.body = JSON.stringify(opts.body);
        }
        const resp = await fetch(url, opts);
        const data = await resp.json();
        if (data.code !== 0) {
            throw new Error(data.msg || "请求失败");
        }
        return data.data;
    },

    get(url) {
        return this.request(url, { method: "GET" });
    },

    post(url, body) {
        return this.request(url, { method: "POST", body });
    },

    put(url, body) {
        return this.request(url, { method: "PUT", body });
    },

    del(url) {
        return this.request(url, { method: "DELETE" });
    },
};

// ── HTML 转义 ────────────────────────────────────────────
// 弹窗内容包含会话标题、规则内容等用户输入，必须转义后再拼进 innerHTML，
// 否则形如 <img src=x onerror=...> 的标题会在弹窗时被执行（XSS）。
// 这里刻意不与各页面的 escapeHtml 同名，避免全局函数互相覆盖。
function escapeText(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
}

function escapeAttrText(text) {
    return escapeText(text).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ── Toast 提示 ───────────────────────────────────────────
function showToast(msg, type = "info", duration = 2500) {
    // 移除已有 toast
    const existing = document.querySelector(".toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.textContent = msg;
    document.body.appendChild(toast);

    requestAnimationFrame(() => toast.classList.add("show"));
    setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// ── 确认弹窗（返回 Promise<boolean>）──────────────────────
/**
 * 居中固定定位的确认卡片。
 *
 * 为什么要分两套：工作台页（/workspace）只引了 workspace.css + settings.css，
 * 没引 common.css，于是下面默认那套 .modal-overlay/.modal-box 没有样式 ——
 * 弹层被当成普通文档流元素渲染，挤在页面下方要手动下滑才看得见（缺陷2）。
 * 这里检测工作台样式是否已加载，是则用 .aw-confirm-* （定义在 workspace.css，
 * 变量与侧边栏统一）；否则退回原有 .modal-* 方案，其它页面行为完全不变。
 */
function showConfirm(message, title = "确认操作") {
    return new Promise((resolve) => {
        // 工作台样式已加载 → 用工作台模态；否则用通用模态
        const useWorkspaceStyle =
            typeof getComputedStyle === "function" &&
            document.documentElement &&
            !!getComputedStyle(document.body).getPropertyValue("--aw-primary");

        const overlay = document.createElement("div");
        if (useWorkspaceStyle) {
            overlay.className = "aw-confirm-mask";
            // 同样只用 textContent / createElement，不拼 innerHTML（保持既有 XSS 约定）
            const card = document.createElement("div");
            card.className = "aw-confirm-card";
            card.setAttribute("role", "dialog");
            card.setAttribute("aria-modal", "true");

            const t = document.createElement("div");
            t.className = "aw-confirm-title";
            t.textContent = title;
            card.appendChild(t);

            const m = document.createElement("p");
            m.className = "aw-confirm-msg";
            m.textContent = message;
            card.appendChild(m);

            const actions = document.createElement("div");
            actions.className = "aw-confirm-actions";

            const cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "aw-confirm-btn";
            cancelBtn.dataset.act = "cancel";
            cancelBtn.textContent = "取消";

            const okBtn = document.createElement("button");
            okBtn.type = "button";
            okBtn.className = "aw-confirm-btn is-danger";
            okBtn.dataset.act = "ok";
            okBtn.textContent = "确认";

            actions.appendChild(cancelBtn);
            actions.appendChild(okBtn);
            card.appendChild(actions);
            overlay.appendChild(card);
        } else {
            overlay.className = "modal-overlay active";
            overlay.innerHTML = `
                <div class="modal-box">
                    <div class="modal-title">${escapeText(title)}</div>
                    <p style="font-size:14px;color:var(--text-secondary);line-height:1.6;">${escapeText(message)}</p>
                    <div class="modal-actions">
                        <button class="btn btn-secondary" data-act="cancel">取消</button>
                        <button class="btn btn-danger" data-act="ok">确认</button>
                    </div>
                </div>
            `;
        }
        document.body.appendChild(overlay);

        const finish = (val) => {
            overlay.remove();
            document.removeEventListener("keydown", onKey);
            resolve(val);
        };
        // 支持键盘操作：Enter 确认、Esc 取消
        const onKey = (e) => {
            if (e.key === "Enter") finish(true);
            else if (e.key === "Escape") finish(false);
        };
        document.addEventListener("keydown", onKey);

        overlay.querySelector('[data-act="cancel"]').onclick = () => finish(false);
        overlay.querySelector('[data-act="ok"]').onclick = () => finish(true);
        overlay.onclick = (e) => {
            if (e.target === overlay) finish(false);
        };
        // 焦点交给确认框：键盘用户可直接回车，且不会误触到背后的输入框
        try {
            const first = overlay.querySelector(".aw-confirm-btn.is-danger")
                || overlay.querySelector(".aw-confirm-btn");
            if (first) first.focus();
        } catch (e) { /* 忽略：不影响确认流程 */ }
    });
}

// ── 输入弹窗（返回 Promise<string|null>）──────────────────
function showPrompt(title, defaultValue = "", placeholder = "请输入内容") {
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "modal-overlay active";
        overlay.innerHTML = `
            <div class="modal-box">
                <div class="modal-title">${escapeText(title)}</div>
                <input class="modal-input" type="text"
                       value="${escapeAttrText(defaultValue)}"
                       placeholder="${escapeAttrText(placeholder)}" />
                <div class="modal-actions">
                    <button class="btn btn-secondary" data-act="cancel">取消</button>
                    <button class="btn btn-primary" data-act="ok">确定</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const input = overlay.querySelector(".modal-input");
        input.focus();
        input.select();

        // 语义约定：取消返回 null，确定返回输入框内容（空字符串表示清空了内容），
        // 这样调用方才能区分「用户取消」与「用户提交空值」。
        const close = (val) => {
            overlay.remove();
            resolve(val);
        };

        overlay.querySelector('[data-act="cancel"]').onclick = () => close(null);
        overlay.querySelector('[data-act="ok"]').onclick = () => {
            close(input.value.trim());
        };
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                close(input.value.trim());
            } else if (e.key === "Escape") {
                close(null);
            }
        });
        overlay.onclick = (e) => {
            if (e.target === overlay) close(null);
        };
    });
}

// ── 时间格式化 ───────────────────────────────────────────
function formatTime(timeStr) {
    if (!timeStr) return "";
    const d = new Date(timeStr.replace(/-/g, "/"));
    const now = new Date();
    const diff = (now - d) / 1000;

    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
    if (diff < 604800) return Math.floor(diff / 86400) + "天前";

    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${mm}-${dd} ${hh}:${mi}`;
}

function formatFullTime(timeStr) {
    if (!timeStr) return "";
    const d = new Date(timeStr.replace(/-/g, "/"));
    const yy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${yy}-${mm}-${dd} ${hh}:${mi}`;
}
