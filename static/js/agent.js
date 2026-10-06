/**
 * Aegis 编码 Agent 工作台（/aegis）— 三栏前端
 *
 * 依赖：
 *   auth.js      aegisFetch / aegisFetchJSON（自动带 X-Aegis-Token）
 *   common.js    escapeText / showToast / showConfirm / showPrompt
 *   settings.js  window.AegisSettings.open()（工作区未配置时引导去设置）
 *
 * 后端接口（设计 §5.4）：
 *   POST /api/agent/run      启动任务，返回 SSE
 *   POST /api/agent/approve  提交审批决策（唤醒挂起的 Runner）
 *   POST /api/agent/cancel   取消任务
 *   GET  /api/agent/changes  查询变更清单
 *   POST /api/agent/undo     回滚变更
 *   GET  /api/settings/workspace / PUT 同路径（工作区读写）
 *   POST /api/aichat/sessions（复用现有会话体系承载任务消息）
 *
 * 安全约定：
 *   · 所有动态文本一律 textContent 写入，或经 escapeText 后再拼 innerHTML；
 *   · 危险操作（dangerous=true）不提供「本次会话允许」按钮，与后端降级逻辑一致；
 *   · 绝不在此保存任何密钥。
 */
(function () {
    "use strict";

    // ── 常量 ──────────────────────────────────────────────
    var TOOL_LABELS = {
        read_file: "读取文件",
        list_dir: "列出目录",
        search_code: "检索代码",
        write_file: "写入文件",
        edit_file: "修改文件",
        run_command: "执行命令",
    };
    var ACTION_LABELS = {
        created: "新建",
        modified: "修改",
        deleted: "删除",
    };
    var RISK_LABELS = {
        readonly: "只读",
        write: "写入",
        dangerous: "危险",
        unknown: "需确认",
    };
    var DECISION_LABELS = {
        allow_once: "仅此一次",
        allow_session: "本次会话允许",
        deny: "拒绝",
    };
    var STOP_REASON = {
        completed: "正常完成",
        max_steps: "达到最大步数",
        user: "已由你停止",
        denied: "连续拒绝后停止",
        tool_errors: "工具连续报错后停止",
        error: "出错停止",
        no_model: "未配置模型",
    };
    var MAX_TERMINAL_CHARS = 20000;   // 终端输出在页面上的累计上限，防止卡死

    // ── DOM ───────────────────────────────────────────────
    var $ = function (id) { return document.getElementById(id); };

    var elWsPath = $("wsPath");
    var elWsReason = $("wsReason");
    var elTaskList = $("taskList");
    var elTimeline = $("timeline");
    var elEmpty = $("timelineEmpty");
    var elInput = $("taskInput");
    var elSend = $("btnSend");
    var elStop = $("btnStop");
    var elStatus = $("runStatus");
    var elRunTitle = $("runTitle");
    var elModelSelect = $("modelChip");
    var elThinkSeg = $("thinkSeg");
    // 技能中心已迁到独立页面 /skills，相关 DOM 引用与逻辑见 static/js/skills.js
    var elSideOverlay = $("sideOverlay");
    var elPause = $("btnPause");
    var elChangeList = $("changeList");
    var elChangeEmpty = $("changeEmpty");
    var elUndoAll = $("btnUndoAll");
    var elStats = $("runStats");
    var elConvList = $("convList");
    var elPermCurrent = $("permCurrent");
    var elComposerHint = $("composerHint");
    var elWsSearchInput = $("wsSearchInput");
    var elWsResults = $("wsResults");
    var elCmdList = $("cmdList");
    var elCmdEmpty = $("cmdEmpty");
    var elFileModal = $("fileModal");
    var elFileModalPath = $("fileModalPath");
    var elFileModalMeta = $("fileModalMeta");
    var elFileModalBody = $("fileModalBody");

    // ── 状态 ──────────────────────────────────────────────
    var state = {
        workspace: { configured: false, valid: false, path: "", reason: "" },
        sessionId: null,
        taskId: "",
        running: false,
        abortCtrl: null,
        runs: [],          // 本次会话执行过的任务
        activeRunId: "",   // 右栏当前展示的任务
        toolCards: {},     // call_id -> { root, body, term }
        streamBuf: null,   // 当前流式文本节点 { el, text }
        pendingApproval: null,
        permissionMode: "approve",  // full / approve / chat
        conversations: [],          // 最近对话（会话）列表
        thinkingLevel: "balanced",  // light / balanced / max
        paused: false,              // 暂停态
        skillsThisRun: [],          // 本次任务可用技能名
        skillsUsed: [],             // 本次任务已调用的技能名
    };

    // ── 小工具 ────────────────────────────────────────────
    function esc(text) {
        if (typeof window.escapeText === "function") return window.escapeText(text);
        var div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }

    function toast(msg, type) {
        if (typeof window.showToast === "function") window.showToast(msg, type || "info");
        else if (window.console && window.console.log) window.console.log(msg);
    }

    function fetchFn() {
        return (typeof window.aegisFetch === "function")
            ? window.aegisFetch
            : window.fetch.bind(window);
    }

    function requestJSON(url, opts) {
        var doFetch = fetchFn();
        return doFetch(url, opts || {}).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (data) {
                if (!data || Number(data.code) !== 0) {
                    throw new Error((data && data.msg) || ("HTTP " + (resp && resp.status)));
                }
                return data.data;
            });
        });
    }

    function getJSON(url) {
        return requestJSON(url, { method: "GET" });
    }

    function postJSON(url, body) {
        return requestJSON(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
    }

    function fmtMs(ms) {
        var n = Number(ms) || 0;
        if (n < 1000) return n + "ms";
        return (n / 1000).toFixed(1) + "s";
    }

    function makeEl(tag, className, text) {
        var el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined && text !== null) el.textContent = String(text);
        return el;
    }

    function scrollTimeline() {
        elTimeline.scrollTop = elTimeline.scrollHeight;
    }

    // ── 工作区 ────────────────────────────────────────────
    function loadWorkspace() {
        return getJSON("/api/settings/workspace").then(function (info) {
            state.workspace = {
                configured: !!(info && info.configured),
                valid: !!(info && info.valid),
                path: (info && info.path) || "",
                reason: (info && info.reason) || "",
            };
            renderWorkspace();
        }).catch(function (e) {
            state.workspace = { configured: false, valid: false, path: "", reason: "读取工作区失败" };
            renderWorkspace();
            toast("读取工作区失败：" + e.message, "error");
        });
    }

    function renderWorkspace() {
        var ws = state.workspace;
        elWsPath.textContent = ws.path || "未设置";
        elWsPath.title = ws.path || "";
        elWsPath.classList.toggle("is-invalid", !ws.valid);
        // 已选定工作区时保持极简：只展示路径；说明文字仅在异常时出现
        if (ws.valid) {
            elWsReason.textContent = "";
            elWsReason.hidden = true;
        } else {
            elWsReason.textContent = ws.reason || "请选择一个本地目录后再执行任务。";
            elWsReason.hidden = false;
        }
        elSend.disabled = state.running;
        elComposerHint.textContent = ws.valid
            ? "工作区：" + ws.path
            : "未设置工作区：仅可对话；选择目录后即可读写文件、执行命令";
    }

    var pickBusy = false;

    // ── 桌面环境识别 ─────────────────────────────────────
    // pywebview 只会在桌面壳里注入 window.pywebview（含 platform 标识，如 edgechromium）；
    // api 上的方法由 Python 侧异步注入（_createApi），ready 事件挂在 **window** 上：
    //   window.dispatchEvent(new CustomEvent("pywebviewready"))
    // 注意 window.pywebview 对象本身没有 addEventListener 方法，不能在它上面监听。
    function desktopBridge() {
        var pw = window.pywebview;
        if (pw && pw.api && typeof pw.api.open_folder_dialog === "function") return pw.api;
        return null;
    }

    function isDesktopShell() {
        return !!(window.pywebview && window.pywebview.platform);
    }

    function logRuntimeEnv() {
        // 环境检测日志（便于排查「桌面端被误判为浏览器」类问题）
        try {
            var pw = window.pywebview || {};
            console.info("[Aegis] runtime: " + (pw.platform
                ? "desktop-shell / pywebview-" + pw.platform + " / api-ready=" + !!(pw.api && Object.keys(pw.api).length)
                : "browser"));
            if (typeof getJSON === "function") {
                getJSON("/api/env").then(function (env) {
                    console.info("[Aegis] server env: " + JSON.stringify(env));
                }).catch(function () { /* 环境信息仅用于诊断，失败可忽略 */ });
            }
        } catch (e) { /* 诊断日志不影响主流程 */ }
    }

    function pickWorkspace() {
        // 只走桌面壳的系统原生目录选择对话框（Windows 资源管理器，可浏览各磁盘分区）。
        // 不提供手动输入路径：桥未就绪时轮询等待 + 明确报错，而不是弹出输入框。
        if (pickBusy) return;

        function openPicker(bridge) {
            pickBusy = true;
            Promise.resolve(bridge.open_folder_dialog()).then(function (path) {
                pickBusy = false;
                path = (path || "").trim();
                if (!path) {
                    toast("未选择任何目录", "info");
                    return;
                }
                putWorkspace(path); // 后端会校验存在性/可读性，非法路径直接提示
            }).catch(function () {
                pickBusy = false;
                toast("打开系统目录选择器失败，请重启 Aegis 后重试", "error");
            });
        }

        var bridge = desktopBridge();
        if (bridge) { openPicker(bridge); return; }

        if (isDesktopShell()) {
            // 桌面壳但 api 尚未注入完成：轮询等待（最长约 6 秒）
            pickBusy = true;
            toast("正在唤醒系统目录选择器…", "info");
            var tries = 0;
            var timer = setInterval(function () {
                var b = desktopBridge();
                if (b) {
                    clearInterval(timer);
                    pickBusy = false;
                    openPicker(b);
                    return;
                }
                tries += 1;
                if (tries >= 15) {
                    clearInterval(timer);
                    pickBusy = false;
                    console.warn("[Aegis] pywebview bridge not ready after 6s; pywebview=", window.pywebview);
                    toast("桌面组件未就绪，请完全退出 Aegis 后重新打开再试", "error");
                }
            }, 400);
            return;
        }
        console.warn("[Aegis] no window.pywebview detected → browser environment");
        toast("请在 Aegis 桌面应用内使用「选择目录」；浏览器环境不支持系统目录选择器", "error");
    }

    function putWorkspace(path) {
        return requestJSON("/api/settings/workspace", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: path }),
        }).then(function (result) {
            if (result && result.ok === false) {
                toast((result && result.reason) || "工作区路径无效", "error");
                return false;
            }
            toast("工作区已设置", "success");
            return loadWorkspace().then(function () { return true; });
        }).catch(function (e) {
            toast("设置工作区失败：" + e.message, "error");
            return false;
        });
    }

    // ── 工作区：复制路径 / 跨目录搜索 / 文件预览 ──────────
    function copyText(text, okMsg) {
        function done() { toast(okMsg || "已复制", "success"); }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done, function () { legacyCopy(); });
        } else {
            legacyCopy();
        }
        function legacyCopy() {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            try {
                document.execCommand("copy");
                done();
            } catch (e) {
                toast("复制失败，请手动选择路径复制", "error");
            }
            document.body.removeChild(ta);
        }
    }

    function doWorkspaceSearch() {
        var q = (elWsSearchInput.value || "").trim();
        if (!q) return;
        if (!state.workspace.valid) {
            toast("请先选择工作区目录", "error");
            return;
        }
        elWsResults.hidden = false;
        elWsResults.innerHTML = "";
        elWsResults.appendChild(makeEl("div", "ag-ws-searching", "搜索中…"));
        getJSON("/api/agent/workspace/search?q=" + encodeURIComponent(q) + "&limit=80")
            .then(function (data) {
                renderWsResults(q, (data && data.matches) || [], !!(data && data.truncated));
            }).catch(function (e) {
                elWsResults.innerHTML = "";
                elWsResults.appendChild(makeEl("div", "ag-ws-searching", "搜索失败：" + (e.message || "未知错误")));
            });
    }

    function renderWsResults(q, matches, truncated) {
        elWsResults.innerHTML = "";
        var head = makeEl("div", "ag-ws-results-head");
        head.appendChild(makeEl("span", "ag-ws-results-count",
            "「" + q + "」：" + matches.length + " 条结果" + (truncated ? "（已截断）" : "")));
        var close = makeEl("button", "ag-icon-btn", "×");
        close.type = "button";
        close.setAttribute("aria-label", "关闭搜索结果");
        close.addEventListener("click", function () { elWsResults.hidden = true; });
        head.appendChild(close);
        elWsResults.appendChild(head);

        if (!matches.length) {
            elWsResults.appendChild(makeEl("div", "ag-ws-searching", "没有匹配的文件或内容"));
            return;
        }

        // 按文件聚合：同名文件的多条内容命中折叠在一个文件条目下
        var byFile = {};
        var order = [];
        matches.forEach(function (m) {
            var key = m.path;
            if (!byFile[key]) { byFile[key] = { kind: m.kind, hits: [] }; order.push(key); }
            if (m.kind === "content") byFile[key].hits.push(m);
        });
        order.slice(0, 30).forEach(function (path) {
            var entry = byFile[path];
            var item = makeEl("div", "ag-ws-file");
            var line1 = makeEl("div", "ag-ws-file-path");
            line1.appendChild(makeEl("span", "ag-ws-file-kind", entry.kind === "name" ? "文件" : "内容"));
            line1.appendChild(makeEl("span", "ag-ws-file-name", path));
            item.appendChild(line1);
            entry.hits.slice(0, 3).forEach(function (hit) {
                var hitRow = makeEl("div", "ag-ws-hit");
                hitRow.appendChild(makeEl("span", "ag-ws-hit-line", "L" + hit.line));
                hitRow.appendChild(makeEl("span", "ag-ws-hit-text", hit.text || ""));
                hitRow.addEventListener("click", function () { openFilePreview(path, hit.line); });
                item.appendChild(hitRow);
            });
            if (!entry.hits.length) {
                item.addEventListener("click", function () { openFilePreview(path, 1); });
            }
            elWsResults.appendChild(item);
        });
        if (order.length > 30) {
            elWsResults.appendChild(makeEl("div", "ag-ws-searching", "其余 " + (order.length - 30) + " 个文件未展示，请缩小关键词"));
        }
    }

    // ── 文件 / 差异预览弹窗（搜索跳转与产物定位共用）──────
    function showFileModal(path, metaText, bodyBuilder) {
        elFileModalPath.textContent = path || "";
        elFileModalPath.title = path || "";
        elFileModalMeta.textContent = metaText || "";
        elFileModalBody.innerHTML = "";
        bodyBuilder(elFileModalBody);
        elFileModal.hidden = false;
    }

    function closeFileModal() { elFileModal.hidden = true; }

    function openFilePreview(path, highlightLine) {
        if (!state.workspace.valid) {
            toast("请先选择工作区目录", "error");
            return;
        }
        var url = "/api/agent/workspace/file?path=" + encodeURIComponent(path);
        if (highlightLine) {
            url += "&start_line=" + Math.max(1, highlightLine - 300) + "&end_line=" + (highlightLine + 300);
        }
        getJSON(url).then(function (data) {
            var lines = (data && data.lines) || [];
            var total = (data && data.total_lines) || 0;
            var start = (data && data.start_line) || 1;
            var meta = total ? ("共 " + total + " 行" + (data.truncated ? "（仅显示部分）" : "")) : "";
            showFileModal(path, meta, function (body) {
                var pre = makeEl("pre", "ag-file-view");
                lines.forEach(function (row) {
                    var lineEl = makeEl("div", "ag-file-line" +
                        (highlightLine && Number(row.no) === Number(highlightLine) ? " is-hit" : ""));
                    lineEl.appendChild(makeEl("span", "ag-file-no", String(row.no || "")));
                    lineEl.appendChild(makeEl("span", "ag-file-text", row.text === "" ? " " : row.text));
                    pre.appendChild(lineEl);
                });
                body.appendChild(pre);
                if (highlightLine) {
                    var hit = body.querySelector(".ag-file-line.is-hit");
                    if (hit) hit.scrollIntoView({ block: "center" });
                }
            });
        }).catch(function (e) {
            toast("无法预览文件：" + (e.message || "未知错误"), "error");
        });
    }

    function openDiffPreview(change) {
        var path = (change && change.path) || "";
        var diff = change && change.diff;
        var stats = change && change.stats;
        var meta = "";
        if (stats) {
            meta = "+" + (Number(stats.added) || 0) + " / -" + (Number(stats.removed) || 0);
        } else if (change && change.bytes) {
            meta = Number(change.bytes) + " B";
        }
        showFileModal(path, (ACTION_LABELS[change.action] || change.action || "") + " · " + meta, function (body) {
            if (diff) {
                var box = makeEl("div", "tl-diff-body ag-fmodal-diff");
                renderDiff(box, diff);
                body.appendChild(box);
                return;
            }
            // 没有现成 diff（如删除的文件或历史快照）→ 回落到文件内容预览
            var tip = makeEl("div", "ag-ws-searching", "没有差异详情，尝试直接预览文件内容…");
            body.appendChild(tip);
            openFilePreview(path, 1);
            closeFileModal();
        });
    }

    // ── 模型切换（顶栏下拉）──────────────────────────────
    // GET /api/settings/providers → {providers:[{key,label,model,enabled,has_key}], active}
    function loadModels() {
        return getJSON("/api/settings/providers").then(function (info) {
            renderModels(info || {});
        }).catch(function () { /* 读取失败时保留现有下拉 */ });
    }

    function renderModels(info) {
        if (!elModelSelect) return;
        // 顶部不再提供下拉切换；仅以只读文本展示当前生效模型（与「设置」保持一致）
        var label = (info && (info.active_label || info.active)) || "";
        if (label) {
            elModelSelect.textContent = "模型：" + label;
            elModelSelect.classList.remove("is-unset");
        } else {
            elModelSelect.textContent = "模型：未配置";
            elModelSelect.classList.add("is-unset");
        }
    }

    function switchModel(key) {
        if (!key) { openSettings(); return; }
        requestJSON("/api/settings/providers", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ active: key }),
        }).then(function (info) {
            toast("已切换模型", "success");
            if (info && info.active_label) loadModels();
        }).catch(function (e) {
            toast("切换模型失败：" + e.message, "error");
            loadModels();
        });
    }

    // ── 思考量级（三档，明确显示当前档位）────────────────
    function loadThinking() {
        return getJSON("/api/settings/thinking").then(function (info) {
            var level = (info && info.level) || "balanced";
            state.thinkingLevel = level;
            renderThinking(level);
        }).catch(function () { renderThinking(state.thinkingLevel); });
    }

    function renderThinking(level) {
        if (!elThinkSeg) return;
        var btns = elThinkSeg.querySelectorAll(".ag-think-btn");
        for (var i = 0; i < btns.length; i++) {
            btns[i].classList.toggle("is-active", btns[i].getAttribute("data-level") === level);
        }
    }

    function setThinking(level) {
        if (!level) return;
        state.thinkingLevel = level;
        renderThinking(level);
        requestJSON("/api/settings/thinking", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ level: level }),
        }).catch(function (e) {
            toast("保存思考量级失败：" + e.message, "error");
        });
    }

    // 技能中心（列表 / 启用禁用 / 注册 / 删除）已迁移为独立页面 /skills，
    // 实现见 static/js/skills.js + templates/agent/skills.html。
    // 工作台这边只保留「技能调用状态」的展示能力（见下方 appendSkillChip）。

    // 时间线内展示技能调用状态（挂到当前 assistant 节点）
    function appendSkillChip(name) {
        if (!name) return;
        var node = state.streamBuf ? state.streamBuf.node : null;
        if (!node) {
            var nodes = elTimeline.querySelectorAll(".tl-assistant");
            node = nodes.length ? nodes[nodes.length - 1] : null;
        }
        if (!node) return;
        node.appendChild(makeEl("span", "ag-skill-chip", "⚡ 技能：" + name));
        scrollTimeline();
    }

    // ── 暂停 / 继续 ──────────────────────────────────────
    function pauseTask() {
        if (!state.running || state.paused) return;
        state.paused = true;
        setStatus("已暂停", "paused");
        if (elPause) elPause.textContent = "继续";
        if (state.taskId) {
            postJSON("/api/agent/pause", { task_id: state.taskId }).catch(function () { /* 已结束则忽略 */ });
        }
    }

    function resumeTask() {
        if (!state.running || !state.paused) return;
        if (state.taskId) {
            postJSON("/api/agent/resume", { task_id: state.taskId }).catch(function () { /* 忽略 */ });
        }
        // resumed 事件到达后由 handleEvent 复位状态
    }

    // ── 侧边栏折叠 / 移动端抽屉 ──────────────────────────
    function toggleSidebar() {
        var app = document.getElementById("aegisApp");
        if (app) app.classList.toggle("side-collapsed");
    }
    function closeSidebar() {
        var app = document.getElementById("aegisApp");
        if (app) app.classList.add("side-collapsed");
    }

    function openSettings() {
        if (window.AegisSettings && typeof window.AegisSettings.open === "function") {
            window.AegisSettings.open();
        } else {
            toast("设置组件未加载，请刷新页面后重试", "error");
        }
    }

    // ── 权限设置（三级：完全访问 / 需用户批准 / 仅保留对话）──
    var PERM_LABELS = { full: "完全访问", approve: "需用户批准", chat: "仅保留对话" };

    function loadPermission() {
        return getJSON("/api/settings/permission").then(function (info) {
            setPermissionMode((info && info.mode) || "approve", true);
        }).catch(function () { /* 读取失败时保持默认 approve */ });
    }

    function setPermissionMode(mode, silent) {
        if (!PERM_LABELS[mode]) mode = "approve";
        state.permissionMode = mode;
        if (elPermCurrent) elPermCurrent.textContent = PERM_LABELS[mode];
        var radios = document.querySelectorAll('input[name="permMode"]');
        for (var i = 0; i < radios.length; i++) {
            radios[i].checked = (radios[i].value === mode);
        }
        if (!silent) toast("权限已切换为「" + PERM_LABELS[mode] + "」", "success");
    }

    function savePermission(mode) {
        if (!PERM_LABELS[mode]) return;
        var prev = state.permissionMode;
        setPermissionMode(mode, true);
        requestJSON("/api/settings/permission", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: mode }),
        }).then(function () {
            toast("权限已切换为「" + PERM_LABELS[mode] + "」并已保存", "success");
        }).catch(function (e) {
            setPermissionMode(prev, true);
            toast("权限保存失败：" + e.message, "error");
        });
    }

    function bindPermission() {
        var toggle = $("btnPermToggle");
        var body = $("permBody");
        if (toggle && body) {
            toggle.addEventListener("click", function () {
                var open = body.classList.toggle("is-open");
                toggle.setAttribute("aria-expanded", open ? "true" : "false");
            });
        }
        var radios = document.querySelectorAll('input[name="permMode"]');
        for (var i = 0; i < radios.length; i++) {
            radios[i].addEventListener("change", function () {
                if (this.checked) savePermission(this.value);
            });
        }
        loadPermission();
    }

    // ── 对话列表（会话）───────────────────────────────────
    function loadConversations() {
        return getJSON("/api/aichat/sessions").then(function (list) {
            state.conversations = Array.isArray(list) ? list : (list && list.sessions) || [];
            renderConversations();
        }).catch(function () { /* 列表读取失败不阻塞主流程 */ });
    }

    function renderConversations() {
        if (!elConvList) return;
        elConvList.innerHTML = "";
        var items = state.conversations || [];
        if (!items.length) {
            elConvList.appendChild(makeEl("div", "ag-conv-empty", "还没有对话，点上方新建一个"));
            return;
        }
        items.slice(0, 20).forEach(function (s) {
            var id = (s.session_id != null) ? s.session_id : s.id;
            var item = makeEl("div", "ag-conv-item");
            item.setAttribute("role", "button");
            item.setAttribute("tabindex", "0");
            if (String(id) === String(state.sessionId)) item.classList.add("is-active");
            item.appendChild(makeEl("span", "ag-conv-title",
                s.session_title || s.title || ("对话 " + id)));
            var del = makeEl("span", "ag-conv-del", "✕");
            del.setAttribute("role", "button");
            del.title = "删除对话";
            del.addEventListener("click", function (ev) {
                ev.stopPropagation();
                deleteConversation(id, s.session_title || s.title || ("对话 " + id));
            });
            item.appendChild(del);
            item.addEventListener("click", function () { switchConversation(id); });
            elConvList.appendChild(item);
        });
    }

    function deleteConversation(id, title) {
        if (state.running) { toast("当前任务仍在执行中，稍后再删除", "info"); return; }
        function doDelete() {
            requestJSON("/api/aichat/sessions/" + id, { method: "DELETE" }).then(function () {
                toast("对话已删除", "success");
                if (String(state.sessionId) === String(id)) {
                    // 删除的是当前对话：清空视图，等下次发送时自动建新会话
                    state.sessionId = null;
                    clearTimeline();
                    elRunTitle.textContent = "Aegis 工作台";
                    setStatus("空闲", "idle");
                }
                return loadConversations();
            }).catch(function (e) {
                toast("删除对话失败：" + e.message, "error");
            });
        }
        if (typeof window.showConfirm === "function") {
            window.showConfirm(
                "将删除对话「" + title + "」及其全部消息，删除后无法恢复。",
                "删除对话").then(function (ok) {
                if (ok) doDelete();
            });
        } else {
            doDelete();
        }
    }

    function clearTimeline() {
        elTimeline.innerHTML = "";
        elEmpty = null;
        state.streamBuf = null;
        state.toolCards = {};
        state.runs = [];
        renderTaskList();
        elChangeList.innerHTML = "";
        elChangeEmpty.style.display = "";
        elStats.innerHTML = "";
        elUndoAll.disabled = true;
        state.taskId = "";
        state.activeRunId = "";
    }

    function renderHistory(sessionId) {
        return getJSON("/api/aichat/sessions/" + sessionId + "/messages").then(function (list) {
            var msgs = Array.isArray(list) ? list : (list && list.messages) || [];
            msgs.forEach(function (m) {
                var role = m.message_role || m.role;
                var text = m.message_content || m.content || "";
                if (!text) return;
                if (role === "user") {
                    appendUserBubble(text);
                } else if (role === "assistant") {
                    var node = pushNode("tl-assistant");
                    node.appendChild(makeEl("div", "tl-bubble", text));
                }
            });
        }).catch(function () { /* 历史读取失败时保留空时间线 */ });
    }

    function switchConversation(id) {
        if (state.running) { toast("当前任务仍在执行中", "info"); return; }
        state.sessionId = id;
        clearTimeline();
        var hit = null;
        (state.conversations || []).forEach(function (s) {
            var sid = (s.session_id != null) ? s.session_id : s.id;
            if (String(sid) === String(id)) hit = s;
        });
        elRunTitle.textContent = (hit && (hit.session_title || hit.title)) || "Aegis 工作台";
        setStatus("空闲", "idle");
        renderConversations();
        renderHistory(id);
        elInput.focus();
    }

    function newConversation() {
        if (state.running) { toast("当前任务仍在执行中", "info"); return Promise.resolve(null); }
        return postJSON("/api/aichat/sessions", { title: "新对话" }).then(function (s) {
            state.sessionId = (s && s.session_id) || null;
            clearTimeline();
            elRunTitle.textContent = "新对话";
            setStatus("空闲", "idle");
            toast("已开启新对话", "success");
            return loadConversations();
        }).catch(function (e) {
            toast("新建对话失败：" + e.message, "error");
            return null;
        });
    }

    // ── 会话 ──────────────────────────────────────────────
    function ensureSession() {
        if (state.sessionId) return Promise.resolve(state.sessionId);
        return postJSON("/api/aichat/sessions", { title: "Aegis 任务" }).then(function (s) {
            state.sessionId = s.session_id;
            loadConversations();
            return state.sessionId;
        });
    }

    // ── 任务列表（左栏；按任务聚合，可折叠）───────────────
    function taskStateOf(run) {
        if (run.running) return { label: "执行中", cls: "state-running" };
        var stopped = run.stopped_by || "completed";
        if (stopped === "completed") return { label: "正常完成", cls: "state-ok" };
        if (stopped === "user") return { label: "已由你停止", cls: "state-idle" };
        return { label: STOP_REASON[stopped] || "已结束", cls: "state-bad" };
    }

    function taskTimeOf(run) {
        if (run.elapsedMs != null) return fmtMs(run.elapsedMs);
        if (run.running && run.startedAt) {
            var secs = Math.floor((Date.now() - run.startedAt.getTime()) / 1000);
            return secs < 60 ? (secs + "s") : Math.floor(secs / 60) + "m" + (secs % 60) + "s";
        }
        return "";
    }

    function renderTaskList() {
        elTaskList.innerHTML = "";
        if (!state.runs.length) {
            elTaskList.appendChild(makeEl("div", "ag-task-empty", "还没有执行过任务"));
            return;
        }
        state.runs.forEach(function (run) {
            var st = taskStateOf(run);
            var item = makeEl("div", "ag-task-item" + (run.task_id === state.activeRunId ? " active" : "") +
                (run.expanded ? " expanded" : ""));

            // 收起态一行摘要：标题 + 状态 + 时间（保持单行信息密度，不展开细节）
            var row = makeEl("div", "ag-task-row");
            var caret = makeEl("span", "ag-task-caret", "›");
            row.appendChild(caret);
            var title = makeEl("span", "ag-task-title", run.title);
            title.title = run.title;
            row.appendChild(title);
            row.appendChild(makeEl("span", "ag-task-pill " + st.cls, st.label));
            var timeText = taskTimeOf(run);
            if (timeText) row.appendChild(makeEl("span", "ag-task-time", timeText));
            row.addEventListener("click", function () {
                run.expanded = !run.expanded;
                renderTaskList();
            });
            item.appendChild(row);

            // 展开态：摘要 + 关键指标 + 产物入口
            var detail = makeEl("div", "ag-task-detail");
            var summary = (run.lastText || "").trim().replace(/\s+/g, " ");
            if (summary) {
                detail.appendChild(makeEl("div", "ag-task-summary",
                    summary.length > 90 ? summary.slice(0, 90) + "…" : summary));
            }
            var meta = makeEl("div", "ag-task-meta");
            meta.appendChild(makeEl("span", "ag-task-count",
                "步骤 " + (run.steps || 0) + " · 工具 " + (run.toolCalls || 0) +
                " · 变更 " + (run.changes || []).length));
            if (run.task_id === state.activeRunId) {
                meta.appendChild(makeEl("span", "ag-task-current", "当前展示"));
            }
            detail.appendChild(meta);
            var openBtn = makeEl("button", "ag-btn ag-btn-ghost ag-btn-sm", "查看产物");
            openBtn.type = "button";
            openBtn.addEventListener("click", function () { selectRun(run.task_id); });
            detail.appendChild(openBtn);
            item.appendChild(detail);

            elTaskList.appendChild(item);
        });
    }

    function findRun(taskId) {
        for (var i = 0; i < state.runs.length; i++) {
            if (state.runs[i].task_id === taskId) return state.runs[i];
        }
        return null;
    }

    function selectRun(taskId) {
        var run = findRun(taskId);
        if (!run) return;
        state.activeRunId = taskId;
        renderTaskList();
        renderChanges(run);
        refreshChanges(taskId);
    }

    // ── 时间线：通用节点 ──────────────────────────────────
    function hideEmpty() {
        if (elEmpty && elEmpty.parentNode) elEmpty.parentNode.removeChild(elEmpty);
        elEmpty = null;
    }

    function pushNode(className) {
        hideEmpty();
        var node = makeEl("div", "tl-item " + className);
        elTimeline.appendChild(node);
        scrollTimeline();
        return node;
    }

    function appendUserBubble(text) {
        var node = pushNode("tl-user");
        var bubble = makeEl("div", "tl-bubble", text);
        node.appendChild(bubble);
        return node;
    }

    function appendStep(step, title) {
        var node = pushNode("tl-step");
        node.appendChild(makeEl("span", "tl-step-badge", "第 " + step + " 步"));
        node.appendChild(makeEl("span", "tl-step-text", title || "思考中…"));
        return node;
    }

    function appendNotice(text) {
        var node = pushNode("tl-notice");
        node.appendChild(makeEl("span", "tl-notice-text", text || ""));
        return node;
    }

    /** 流式文本：连续 chunk 合并到同一个气泡 */
    function appendChunk(text) {
        if (!text) return;
        if (!state.streamBuf) {
            var node = pushNode("tl-assistant");
            var bubble = makeEl("div", "tl-bubble");
            node.appendChild(bubble);
            state.streamBuf = { node: node, bubble: bubble, text: "" };
        }
        state.streamBuf.text += text;
        state.streamBuf.bubble.textContent = state.streamBuf.text;
        scrollTimeline();
    }

    function flushStream() {
        state.streamBuf = null;
    }

    // ── 时间线：工具卡片 ──────────────────────────────────
    function appendToolCall(evt) {
        flushStream();
        var callId = evt.call_id || ("call-" + Date.now());
        var node = pushNode("tl-tool");
        var head = makeEl("div", "tl-tool-head");
        head.appendChild(makeEl("span", "tl-tool-name", TOOL_LABELS[evt.tool] || evt.tool || "工具"));
        var risk = evt.risk || "unknown";
        head.appendChild(makeEl("span", "tl-tool-risk risk-" + risk, RISK_LABELS[risk] || risk));
        head.appendChild(makeEl("span", "tl-tool-summary", evt.summary || ""));
        var toggle = makeEl("button", "tl-tool-toggle", "详情");
        toggle.type = "button";
        head.appendChild(toggle);
        node.appendChild(head);

        var body = makeEl("div", "tl-tool-body");
        body.style.display = "none";
        node.appendChild(body);

        var args = evt.args || {};
        var argsText = "";
        try { argsText = JSON.stringify(args, null, 2); } catch (e) { argsText = String(args); }
        if (argsText && argsText !== "{}") {
            body.appendChild(makeEl("div", "tl-tool-label", "参数"));
            body.appendChild(makeEl("pre", "tl-code", argsText));
        }

        var term = makeEl("pre", "tl-terminal");
        term.style.display = "none";
        var termText = { value: "" };
        node.appendChild(term);

        var status = makeEl("div", "tl-tool-status", "执行中…");
        node.appendChild(status);

        toggle.addEventListener("click", function () {
            var shown = body.style.display !== "none";
            body.style.display = shown ? "none" : "";
            toggle.textContent = shown ? "详情" : "收起";
        });

        state.toolCards[callId] = {
            root: node, body: body, status: status,
            term: term, termText: termText,
        };
        scrollTimeline();
        return node;
    }

    function finishToolCall(evt) {
        var card = state.toolCards[evt.call_id];
        if (!card) return;
        var ok = !!evt.ok;
        card.root.classList.add(ok ? "ok" : "failed");
        card.status.textContent = ok
            ? ("完成 · " + fmtMs(evt.elapsed_ms))
            : ("失败 · " + ((evt.error && evt.error.message) || "未知错误"));
        card.status.classList.add(ok ? "is-ok" : "is-failed");

        var data = evt.data;
        var text = "";
        if (data !== undefined && data !== null) {
            if (typeof data === "string") text = data;
            else {
                try { text = JSON.stringify(data, null, 2); } catch (e) { text = String(data); }
            }
        }
        if (text) {
            card.body.appendChild(makeEl("div", "tl-tool-label", "结果"));
            card.body.appendChild(makeEl("pre", "tl-code", text));
        }
        if (!ok && evt.error && evt.error.detail !== undefined && evt.error.detail !== null) {
            var detail = typeof evt.error.detail === "string"
                ? evt.error.detail
                : JSON.stringify(evt.error.detail, null, 2);
            card.body.appendChild(makeEl("div", "tl-tool-label", "详情"));
            card.body.appendChild(makeEl("pre", "tl-code", detail));
        }
        scrollTimeline();
    }

    function appendTerminal(evt) {
        var card = state.toolCards[evt.call_id];
        if (!card) return;
        if (card.termText.value.length < MAX_TERMINAL_CHARS) {
            card.termText.value += (evt.text || "");
        }
        card.term.textContent = card.termText.value;
        card.term.style.display = "";
        scrollTimeline();
    }

    /** unified diff 渲染：逐行判定 +/-，全部走 textContent，天然免疫 XSS */
    function renderDiff(container, diffText) {
        var lines = String(diffText || "").split("\n");
        lines.forEach(function (line) {
            var cls = "dl-ctx";
            if (line.indexOf("+") === 0 && line.indexOf("+++") !== 0) cls = "dl-add";
            else if (line.indexOf("-") === 0 && line.indexOf("---") !== 0) cls = "dl-del";
            else if (line.indexOf("@@") === 0) cls = "dl-hunk";
            else if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) cls = "dl-file";
            container.appendChild(makeEl("div", "dl " + cls, line));
        });
    }

    function appendDiff(evt) {
        var node = pushNode("tl-diff");
        var head = makeEl("div", "tl-diff-head");
        head.appendChild(makeEl("span", "tl-diff-path", evt.path || ""));
        var stats = evt.stats || {};
        head.appendChild(makeEl("span", "tl-diff-add", "+" + (Number(stats.added) || 0)));
        head.appendChild(makeEl("span", "tl-diff-del", "-" + (Number(stats.removed) || 0)));
        node.appendChild(head);

        var body = makeEl("div", "tl-diff-body");
        renderDiff(body, evt.diff);
        node.appendChild(body);

        var card = state.toolCards[evt.call_id];
        if (card) {
            // 同时把差异收进对应工具卡片，便于折叠后回看
            var copy = makeEl("div", "tl-diff-body");
            renderDiff(copy, evt.diff);
            card.body.appendChild(makeEl("div", "tl-tool-label", "差异"));
            card.body.appendChild(copy);
        }
        scrollTimeline();
        return node;
    }

    // ── 审批卡片 ──────────────────────────────────────────
    var KIND_LABELS = {
        file_write: "写入文件",
        file_edit: "修改文件",
        command: "执行命令",
    };

    function appendApproval(evt) {
        flushStream();
        var node = pushNode("tl-approval" + (evt.dangerous ? " is-dangerous" : ""));
        node.setAttribute("data-request-id", evt.request_id || "");

        var head = makeEl("div", "tl-ap-head");
        head.appendChild(makeEl("span", "tl-ap-kind", KIND_LABELS[evt.kind] || evt.kind || "操作"));
        head.appendChild(makeEl("span", "tl-ap-title", evt.title || "需要你确认"));
        node.appendChild(head);

        if (evt.dangerous) {
            node.appendChild(makeEl("div", "tl-ap-warn",
                "⚠️ 这是一条高风险命令，只允许「仅此一次」，不可在本次会话内豁免。"));
        }
        if (evt.detail) {
            node.appendChild(makeEl("pre", "tl-code tl-ap-detail", evt.detail));
        }
        if (evt.diff) {
            var diffBox = makeEl("div", "tl-diff-body");
            renderDiff(diffBox, evt.diff);
            node.appendChild(diffBox);
        }

        var actions = makeEl("div", "tl-ap-actions");
        var options = Array.isArray(evt.options) && evt.options.length
            ? evt.options
            : ["allow_once", "deny"];
        // 危险操作：即便后端把 allow_session 放进 options，前端也强制剔除
        if (evt.dangerous) {
            options = options.filter(function (o) { return o !== "allow_session"; });
        }
        options.forEach(function (decision) {
            var btn = makeEl("button", "ag-btn ag-btn-sm " + btnClassOf(decision),
                DECISION_LABELS[decision] || decision);
            btn.type = "button";
            btn.addEventListener("click", function () {
                resolveApproval(evt.request_id, decision, node, actions);
            });
            actions.appendChild(btn);
        });
        node.appendChild(actions);
        state.pendingApproval = node;
        return node;
    }

    function btnClassOf(decision) {
        if (decision === "deny") return "ag-btn-danger";
        if (decision === "allow_session") return "ag-btn-ghost";
        return "ag-btn-primary";
    }

    function resolveApproval(requestId, decision, node, actions) {
        if (!requestId) return;
        Array.prototype.forEach.call(actions.querySelectorAll("button"), function (b) {
            b.disabled = true;
        });
        postJSON("/api/agent/approve", {
            request_id: requestId,
            decision: decision,
            task_id: state.taskId,
        }).catch(function (e) {
            toast("提交审批失败：" + e.message, "error");
            Array.prototype.forEach.call(actions.querySelectorAll("button"), function (b) {
                b.disabled = false;
            });
            return;
        }).then(function () {
            if (node) node.classList.add("resolved");
            var tag = makeEl("span", "tl-ap-result", "已提交：" + (DECISION_LABELS[decision] || decision));
            if (node) node.appendChild(tag);
            if (state.pendingApproval === node) state.pendingApproval = null;
        });
    }

    function markApprovalResolved(evt) {
        var node = state.pendingApproval;
        if (!node || node.getAttribute("data-request-id") !== (evt.request_id || "")) {
            return;
        }
        node.classList.add("resolved");
        var actions = node.querySelector(".tl-ap-actions");
        if (actions) {
            Array.prototype.forEach.call(actions.querySelectorAll("button"), function (b) {
                b.disabled = true;
            });
        }
        var tag = makeEl("span", "tl-ap-result", "决策：" + (DECISION_LABELS[evt.decision] || evt.decision));
        node.appendChild(tag);
        state.pendingApproval = null;
    }

    // ── 错误卡片 ──────────────────────────────────────────
    function appendError(evt) {
        flushStream();
        var node = pushNode("tl-error");
        node.appendChild(makeEl("div", "tl-error-text", evt.content || evt.reason || "任务执行失败"));
        if (evt.action) {
            node.appendChild(makeEl("div", "tl-error-action", evt.action));
        }
        if (evt.need_settings) {
            var btn = makeEl("button", "ag-btn ag-btn-primary ag-btn-sm", "打开设置");
            btn.type = "button";
            btn.addEventListener("click", openSettings);
            node.appendChild(btn);
        }
        return node;
    }

    // ── 右栏：产物面板（文件变更 + 命令记录）──────────────
    function renderChanges(run) {
        renderCmdList(run);
        elChangeList.innerHTML = "";
        var changes = (run && run.changes) || [];
        if (!changes.length) {
            elChangeEmpty.style.display = "";
            elUndoAll.disabled = true;
            return;
        }
        elChangeEmpty.style.display = "none";
        elUndoAll.disabled = false;

        changes.forEach(function (c) {
            var item = makeEl("div", "ag-change-item");
            var head = makeEl("div", "ag-change-head");
            head.appendChild(makeEl("span", "ag-change-badge act-" + (c.action || "modified"),
                ACTION_LABELS[c.action] || c.action || "修改"));
            var path = makeEl("span", "ag-change-path", c.path || "");
            path.title = "点击查看差异或文件内容";
            head.appendChild(path);
            item.appendChild(head);

            var meta = makeEl("div", "ag-change-meta");
            var stats = c.stats || {};
            meta.appendChild(makeEl("span", "ag-diff-add", "+" + (Number(stats.added) || 0)));
            meta.appendChild(makeEl("span", "ag-diff-del", "-" + (Number(stats.removed) || 0)));
            if (c.bytes) {
                meta.appendChild(makeEl("span", "ag-change-bytes", Number(c.bytes) + " B"));
            }
            var undo = makeEl("button", "ag-btn ag-btn-ghost ag-btn-sm", "撤销");
            undo.type = "button";
            undo.addEventListener("click", function (ev) {
                ev.stopPropagation();
                undoOne(c.change_id);
            });
            meta.appendChild(undo);
            item.appendChild(meta);

            // 点击定位：优先看差异，没有差异则直接预览文件内容
            item.addEventListener("click", function () { openDiffPreview(c); });
            elChangeList.appendChild(item);
        });
    }

    function renderCmdList(run) {
        if (!elCmdList || !elCmdEmpty) return;
        elCmdList.innerHTML = "";
        var cmds = (run && run.commands) || [];
        elCmdEmpty.hidden = cmds.length > 0;
        if (!cmds.length) return;
        cmds.forEach(function (cmd) {
            var item = makeEl("div", "ag-cmd-item" + (cmd.ok === false ? " is-fail" : ""));
            var row = makeEl("div", "ag-cmd-row");
            row.appendChild(makeEl("span", "ag-cmd-status " +
                (cmd.ok === null ? "is-run" : (cmd.ok ? "is-ok" : "is-fail")),
                cmd.ok === null ? "…" : (cmd.ok ? "✓" : "✕")));
            var text = makeEl("span", "ag-cmd-text", cmd.command || "(空命令)");
            text.title = (cmd.command || "") + "（点击查看输出）";
            row.appendChild(text);
            item.appendChild(row);
            item.addEventListener("click", function () { showCommandModal(cmd); });
            elCmdList.appendChild(item);
        });
    }

    function showCommandModal(cmd) {
        showFileModal(cmd.command || "命令", "命令执行记录", function (body) {
            var pre = makeEl("pre", "ag-file-view");
            var out = (cmd.output || "").trim() || "（没有捕获到输出）";
            out.split("\n").slice(0, 500).forEach(function (line) {
                var lineEl = makeEl("div", "ag-file-line");
                lineEl.appendChild(makeEl("span", "ag-file-text", line === "" ? " " : line));
                pre.appendChild(lineEl);
            });
            body.appendChild(pre);
        });
    }

    function undoOne(changeId) {
        var taskId = state.activeRunId || state.taskId;
        if (!taskId) return;
        if (typeof window.showConfirm === "function") {
            window.showConfirm("将把该文件恢复到本次修改之前的状态。", "撤销变更").then(function (ok) {
                if (ok) doUndo(taskId, changeId);
            });
        } else {
            doUndo(taskId, changeId);
        }
    }

    function doUndo(taskId, changeId) {
        var payload = { task_id: taskId };
        if (changeId !== undefined && changeId !== null) payload.change_id = changeId;
        postJSON("/api/agent/undo", payload).then(function (result) {
            var restored = (result && result.restored) || 0;
            var removed = (result && result.removed) || 0;
            var failed = (result && result.failed) || [];
            if (failed.length) {
                toast("已回滚 " + (restored + removed) + " 项，" + failed.length + " 项失败，请查看服务日志", "error");
            } else {
                toast("已回滚 " + (restored + removed) + " 项", "success");
            }
            refreshChanges(taskId);
        }).catch(function (e) {
            toast("撤销失败：" + e.message, "error");
        });
    }

    function refreshChanges(taskId) {
        if (!taskId) return;
        getJSON("/api/agent/changes?task_id=" + encodeURIComponent(taskId)).then(function (data) {
            var run = findRun(taskId);
            if (!run) return;
            run.changes = (data && data.changes) || [];
            if (state.activeRunId === taskId) renderChanges(run);
            renderTaskList();
        }).catch(function () {
            /* 任务已结束且快照过期时忽略 */
        });
    }

    function renderStats(evt) {
        elStats.innerHTML = "";
        var stats = (evt && evt.stats) || {};
        var rows = [
            ["步数", Number(stats.steps) || 0],
            ["工具调用", Number(stats.tool_calls) || 0],
            ["文件变更", Number(stats.changes) || 0],
            ["耗时", fmtMs(stats.elapsed_ms)],
            ["结束原因", STOP_REASON[stats.stopped_by] || stats.stopped_by || "—"],
        ];
        rows.forEach(function (r) {
            var row = makeEl("div", "ag-stat-row");
            row.appendChild(makeEl("span", "ag-stat-key", r[0]));
            row.appendChild(makeEl("span", "ag-stat-val", String(r[1])));
            elStats.appendChild(row);
        });
    }

    // ── 任务执行 ──────────────────────────────────────────
    function setStatus(text, stateName) {
        elStatus.textContent = text;
        elStatus.setAttribute("data-state", stateName || "idle");
    }

    function setRunning(running) {
        state.running = running;
        state.paused = false;
        elStop.disabled = !running;
        if (elPause) { elPause.disabled = !running; elPause.textContent = "暂停"; }
        elSend.disabled = running;
        elSend.textContent = running ? "执行中…" : "发送";
    }

    function sendTask() {
        var text = (elInput.value || "").trim();
        if (!text) return;
        if (state.running) return;
        // 无工作区时不再拦截：服务端会自动降级为纯对话模式（不触碰文件/命令）

        elInput.value = "";
        appendUserBubble(text);
        setRunning(true);
        setStatus("执行中", "running");
        elRunTitle.textContent = text.length > 22 ? text.slice(0, 22) + "…" : text;

        ensureSession().then(function (sid) {
            state.abortCtrl = new AbortController();
            return fetchFn()("/api/agent/run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    session_id: sid,
                    message: text,
                    // 工作区无效时不携带，服务端据此降级为纯对话
                    workspace: state.workspace.valid ? state.workspace.path : "",
                    permission_mode: state.permissionMode,
                    thinking_level: state.thinkingLevel,
                }),
                signal: state.abortCtrl.signal,
            }).then(function (resp) {
                if (!resp.ok) {
                    return resp.json().catch(function () { return {}; }).then(function (err) {
                        throw new Error((err && err.msg) || ("HTTP " + resp.status));
                    });
                }
                return readSSE(resp, handleEvent);
            });
        }).catch(function (e) {
            if (e && e.name === "AbortError") return;
            appendError({ content: "启动任务失败：" + (e.message || "未知错误"), action: "请重试或到设置中检查模型配置" });
            finishRun();
        });
    }

    function stopTask() {
        if (!state.running) return;
        if (state.taskId) {
            postJSON("/api/agent/cancel", { task_id: state.taskId }).catch(function () {
                /* 任务可能已结束，忽略 */
            });
        }
        if (state.abortCtrl) {
            try { state.abortCtrl.abort(); } catch (e) { /* ignore */ }
            state.abortCtrl = null;
        }
        setStatus("已停止", "idle");
        setRunning(false);
    }

    function finishRun() {
        flushStream();
        setRunning(false);
        setStatus("空闲", "idle");
        if (state.taskId) refreshChanges(state.taskId);
        // 对话标题会随首条消息更新，任务结束后刷新列表
        loadConversations();
    }

    // ── SSE 事件分发 ──────────────────────────────────────
    function handleEvent(evt) {
        if (!evt || !evt.type) return;
        var type = evt.type;

        if (type === "run_start") {
            state.taskId = evt.task_id || state.taskId;
            state.skillsThisRun = Array.isArray(evt.skills) ? evt.skills : [];
            state.skillsUsed = [];
            if (state.skillsThisRun.length) {
                appendNotice("本次可用技能：" + state.skillsThisRun.join("、"));
            }
            state.runs.push({
                task_id: state.taskId,
                title: elRunTitle.textContent || "新任务",
                running: true,
                stopped_by: "",
                changes: [],
                steps: 0,
                toolCalls: 0,
                elapsedMs: null,
                startedAt: new Date(),
                lastText: "",
                commands: [],
                cmdByCall: {},
                expanded: true,   // 执行中的任务默认展开
            });
            state.activeRunId = state.taskId;
            renderTaskList();
            if (evt.model && elModelSelect) {
                elModelSelect.textContent = "模型：" + evt.model;
                elModelSelect.classList.remove("is-unset");
            }
            return;
        }

        if (type === "agent_step") {
            appendStep(evt.step, evt.title);
            var stepRun = findRun(state.taskId);
            if (stepRun && evt.step) stepRun.steps = evt.step;
            return;
        }

        if (type === "chunk") {
            appendChunk(evt.content);
            var chunkRun = findRun(state.taskId);
            if (chunkRun && chunkRun.lastText.length < 2000) {
                chunkRun.lastText += evt.content || "";
            }
            return;
        }

        if (type === "tool_call") {
            appendToolCall(evt);
            var callRun = findRun(state.taskId);
            if (callRun) {
                callRun.toolCalls += 1;
                if (evt.tool === "run_command") {
                    // 产物面板的命令记录：先记命令，执行结果回填 ok
                    var entry = {
                        command: evt.summary || (evt.args && evt.args.command) || "",
                        ok: null,
                        output: "",
                    };
                    callRun.commands.push(entry);
                    callRun.cmdByCall[evt.call_id || ""] = entry;
                    if (state.activeRunId === callRun.task_id) renderCmdList(callRun);
                }
            }
            return;
        }

        if (type === "tool_result") {
            finishToolCall(evt);
            var resRun = findRun(state.taskId);
            if (resRun) {
                var cmdEntry = resRun.cmdByCall[evt.call_id || ""];
                if (cmdEntry) {
                    cmdEntry.ok = !!evt.ok;
                    if (!evt.ok && evt.error && evt.error.message) {
                        cmdEntry.output = (cmdEntry.output || "") + "\n[错误] " + evt.error.message;
                    }
                }
            }
            return;
        }

        if (type === "diff") {
            appendDiff(evt);
            return;
        }

        if (type === "terminal") {
            appendTerminal(evt);
            // 命令输出归档到最近一条尚未结束的命令（terminal 事件紧跟其命令）
            var termRun = findRun(state.taskId);
            if (termRun && termRun.commands.length) {
                var lastCmd = termRun.commands[termRun.commands.length - 1];
                if (lastCmd && lastCmd.ok === null && lastCmd.output.length < 2000) {
                    lastCmd.output += evt.text || "";
                }
            }
            return;
        }

        if (type === "file_change") {
            var run = findRun(evt.task_id || state.taskId);
            if (run) {
                // 同一文件重复变更时替换旧记录，保证清单与磁盘状态一致
                var replaced = false;
                for (var i = 0; i < run.changes.length; i++) {
                    if (run.changes[i].path === evt.path) {
                        run.changes[i] = evt;
                        replaced = true;
                        break;
                    }
                }
                if (!replaced) run.changes.push(evt);
                if (state.activeRunId === run.task_id) renderChanges(run);
            }
            return;
        }

        if (type === "approval_request") {
            appendApproval(evt);
            setStatus("等待你确认", "waiting");
            return;
        }

        if (type === "approval_resolved") {
            markApprovalResolved(evt);
            setStatus("执行中", "running");
            return;
        }

        if (type === "notice") {
            flushStream();
            appendNotice(evt.content);
            return;
        }

        if (type === "paused") {
            state.paused = true;
            setStatus("已暂停", "paused");
            if (elPause) elPause.textContent = "继续";
            return;
        }

        if (type === "resumed") {
            state.paused = false;
            setStatus("执行中", "running");
            if (elPause) elPause.textContent = "暂停";
            return;
        }

        if (type === "skill_used") {
            var sk = evt.skill || "";
            if (sk && state.skillsUsed.indexOf(sk) === -1) state.skillsUsed.push(sk);
            appendSkillChip(sk);
            return;
        }

        if (type === "error") {
            appendError(evt);
            return;
        }

        if (type === "run_end") {
            flushStream();
            var rec = findRun(evt.task_id || state.taskId);
            if (rec) {
                rec.running = false;
                rec.stopped_by = (evt.stats && evt.stats.stopped_by) || "";
                rec.elapsedMs = (evt.stats && evt.stats.elapsed_ms) || null;
                if (evt.stats && evt.stats.steps) rec.steps = evt.stats.steps;
                if (evt.stats && evt.stats.tool_calls) rec.toolCalls = evt.stats.tool_calls;
            }
            renderTaskList();
            renderStats(evt);
            return;
        }

        if (type === "done") {
            finishRun();
            return;
        }

        // 未知事件类型：直接忽略，不抛错、不改 DOM
    }

    // 逐行解析 text/event-stream
    function readSSE(resp, onEvent) {
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
            return reader.read().then(function (result) {
                if (result.done) return;
                buffer += decoder.decode(result.value, { stream: true });
                var idx;
                while ((idx = buffer.indexOf("\n\n")) !== -1) {
                    var rawEvent = buffer.substring(0, idx);
                    buffer = buffer.substring(idx + 2);
                    rawEvent.split("\n").forEach(function (line) {
                        if (line.indexOf("data:") !== 0) return;
                        var dataStr = line.substring(5).trim();
                        if (!dataStr) return;
                        try { onEvent(JSON.parse(dataStr)); } catch (e) { /* 跳过坏帧 */ }
                    });
                }
                return pump();
            });
        }
        return pump();
    }

    // ── 事件绑定 ──────────────────────────────────────────
    elSend.addEventListener("click", sendTask);
    elStop.addEventListener("click", stopTask);
    $("btnPickWorkspace").addEventListener("click", pickWorkspace);
    $("btnCopyWs").addEventListener("click", function () {
        if (!state.workspace.path) { toast("还没有设置工作区", "info"); return; }
        copyText(state.workspace.path, "工作区路径已复制");
    });
    $("btnOpenSettings").addEventListener("click", openSettings);
    $("btnSettings").addEventListener("click", openSettings);
    if (elWsSearchInput) {
        elWsSearchInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); doWorkspaceSearch(); }
        });
    }
    var elWsSearchBtn = $("btnWsSearch");
    if (elWsSearchBtn) elWsSearchBtn.addEventListener("click", doWorkspaceSearch);

    // 文件预览弹窗：关闭 / 复制路径 / Esc
    var elFileModalClose = $("fileModalClose");
    if (elFileModalClose) elFileModalClose.addEventListener("click", closeFileModal);
    var elFileModalMask = $("fileModalMask");
    if (elFileModalMask) elFileModalMask.addEventListener("click", closeFileModal);
    var elFileModalCopy = $("fileModalCopy");
    if (elFileModalCopy) elFileModalCopy.addEventListener("click", function () {
        var p = elFileModalPath.textContent || "";
        if (p) copyText(p, "路径已复制");
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && elFileModal && !elFileModal.hidden) closeFileModal();
    });

    $("btnNewTask").addEventListener("click", function () {
        // 新建对话：立即创建新会话并清空工作区视图（不再复用当前对话）
        newConversation();
    });

    elInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendTask();
        }
    });

    elUndoAll.addEventListener("click", function () {
        var taskId = state.activeRunId || state.taskId;
        if (!taskId) return;
        if (typeof window.showConfirm === "function") {
            window.showConfirm("将把本次任务改动过的文件全部恢复到修改之前。", "全部撤销").then(function (ok) {
                if (ok) doUndo(taskId, null);
            });
        } else {
            doUndo(taskId, null);
        }
    });

    // 权限设置（三级）—— 替代原「自动批准写入」开关
    bindPermission();

    // ── 右栏折叠 / 展开 ──────────────────────────────────
    var RIGHT_COLLAPSE_KEY = "aegis.rightCollapsed";
    function applyRightCollapsed(collapsed) {
        var app = document.getElementById("aegisApp");
        var btn = $("btnToggleRight");
        if (app) app.classList.toggle("right-collapsed", collapsed);
        if (btn) btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
        try { window.localStorage.setItem(RIGHT_COLLAPSE_KEY, collapsed ? "1" : "0"); } catch (e) { /* 忽略 */ }
    }

    function bindRightToggle() {
        var btn = $("btnToggleRight");
        if (!btn) return;
        var collapsed = false;
        try { collapsed = window.localStorage.getItem(RIGHT_COLLAPSE_KEY) === "1"; } catch (e) { /* 忽略 */ }
        applyRightCollapsed(collapsed);
        btn.addEventListener("click", function () {
            var app = document.getElementById("aegisApp");
            applyRightCollapsed(!(app && app.classList.contains("right-collapsed")));
        });
    }

    // ── 初始化 ────────────────────────────────────────────
    renderTaskList();
    setRunning(false);
    // 移动端默认折叠侧边栏（抽屉收起），由顶栏按钮展开
    if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
        var app0 = document.getElementById("aegisApp");
        if (app0) app0.classList.add("side-collapsed");
    }
    loadWorkspace();
    loadModels();
    loadThinking();
    loadConversations();
    bindRightToggle();
    logRuntimeEnv();
    // 桌面壳的桥是异步注入的：就绪后补记一条环境日志（便于诊断桥延迟问题）
    window.addEventListener("pywebviewready", function () {
        try {
            console.info("[Aegis] pywebviewready fired, api keys:", Object.keys((window.pywebview || {}).api || {}));
        } catch (e) { /* 诊断日志 */ }
    });
    // 设置保存 / 模型切换后刷新模型下拉
    window.addEventListener("aegis:settings-changed", function () {
        loadModels();
        loadWorkspace();
    });

    // ── 思考量级（三档）─────────────────────────────────
    if (elThinkSeg) {
        var thinkBtns = elThinkSeg.querySelectorAll(".ag-think-btn");
        for (var ti = 0; ti < thinkBtns.length; ti++) {
            (function (btn) {
                btn.addEventListener("click", function () {
                    setThinking(btn.getAttribute("data-level"));
                });
            })(thinkBtns[ti]);
        }
    }

    // ── 技能中心 ────────────────────────────────────────
    // 技能中心：跳转独立页面 /skills（不再用浮层弹窗 —— 浮层会遮挡主页面、
    // 背景半透明导致底层透视、文字重叠；独立页面自带不透明背景与隔离容器）
    var elBtnSkills = $("btnSkills");
    if (elBtnSkills) {
        elBtnSkills.addEventListener("click", function () {
            window.location.href = "/skills";
        });
    }

    // ── 暂停 / 继续 ──────────────────────────────────────
    if (elPause) elPause.addEventListener("click", function () {
        if (state.paused) resumeTask(); else pauseTask();
    });

    // ── 侧边栏折叠 / 移动端抽屉 ──────────────────────────
    var elSideToggle = $("btnSideToggle");
    if (elSideToggle) elSideToggle.addEventListener("click", toggleSidebar);
    if (elSideOverlay) elSideOverlay.addEventListener("click", closeSidebar);

    elInput.focus();
})();
