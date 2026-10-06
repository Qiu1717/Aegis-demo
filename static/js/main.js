/**
 * 首页 — 进入即可直接对话（接入真实后端）
 * 数据来源：
 *   GET    /api/aichat/sessions              会话列表（置顶优先 + 最后消息时间倒序）
 *   POST   /api/aichat/sessions              新建会话
 *   DELETE /api/aichat/sessions/<id>         删除会话（级联删消息）→ 用作「清空当前会话」
 *   GET    /api/aichat/sessions/<id>/messages 会话历史消息
 *   POST   /api/aichat/chat                  流式对话（SSE：chunk/notice/provider/blocked/error/done）
 * 完整版聊天页 /aichat 保留为二级入口，本页逻辑独立、不依赖它。
 */
(function () {
    "use strict";

    // ── 快捷内容（仅用于填充输入框，与后端无关）─────────
    var EXAMPLES = [
        { icon: "c1", title: "写一份项目周报", desc: "快速生成结构清晰的周报", prompt: "帮我写一份本周项目周报，包含已完成工作、下周计划和风险提示。" },
        { icon: "c2", title: "解释一段代码", desc: "用通俗语言讲清楚原理", prompt: "用通俗易懂的方式解释一下 Python 装饰器是什么。" },
        { icon: "c3", title: "中英互译", desc: "地道的翻译与表达建议", prompt: "把「工欲善其事，必先利其器」翻译成地道的英文。" },
        { icon: "c4", title: "制定学习计划", desc: "帮你拆解目标和节奏", prompt: "我想在一个月内入门 Flask Web 开发，帮我制定一份学习计划。" },
    ];
    var CHIPS = ["帮我写周报", "翻译成英文", "解释这段代码", "来个头脑风暴", "润色这段文案"];

    // 本页（对话模式）能识别的 SSE 事件类型；其余类型一律忽略（AG-09①）
    var KNOWN_TYPES = ["chunk", "notice", "provider", "web", "blocked", "error", "done"];

    // 长期记忆类别 -> 中文展示标签（与服务端 memory.CATEGORY_LABELS 对应）
    var CATEGORY_LABELS = {
        name: "姓名", location: "所在地", preference: "偏好",
        occupation: "职业", project: "项目", general: "其他",
    };

    var EX_ICONS = {
        c1: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h4"/>',
        c2: '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
        c3: '<circle cx="7" cy="12" r="5"/><circle cx="17" cy="12" r="5"/>',
        c4: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    };
    var COPY_SVG =
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    var SEND_SVG =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    var STOP_SVG =
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

    // ── 状态 ──────────────────────────────────────────
    var sessions = [];        // 服务端会话列表
    var activeId = null;      // 当前会话 id（null = 未开始的新对话）
    var currentMessages = []; // 当前会话已渲染的消息（避免重复拉取）
    var isSending = false;
    var abortCtrl = null;
    var webEnabled = true;     // 「联网搜索」开关（默认开；关闭时后端不发起任何网络请求）

    // ── DOM ───────────────────────────────────────────
    var $ = function (id) { return document.getElementById(id); };
    var app = $("app");
    var sessionListEl = $("sessionList");
    var messagesEl = $("messages");
    var heroEl = $("hero");
    var inputEl = $("chatInput");
    var sendBtn = $("sendBtn");
    var clearBtn = $("clearChatBtn");
    var topbarTitle = $("topbarTitle");
    var modelTag = $("modelTag");
    var chatScroll = $("chatScroll");

    var webToggle = $("webToggle");
    var poweredByLabel = modelTag ? modelTag.textContent : "Aegis";

    // ── 工具 ──────────────────────────────────────────
    function hhmm(dateStr) {
        var d = dateStr ? new Date(String(dateStr).replace(/-/g, "/")) : new Date();
        if (isNaN(d.getTime())) return "";
        return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    }

    // 按时段动态问候（身份统一为 Aegis）
    function greetingByHour(h) {
        if (h < 5) return "夜深了，我是 Aegis，需要我做点什么吗";
        if (h < 9) return "早上好，我是 Aegis，新的一天开始了";
        if (h < 12) return "早上好，我是 Aegis，需要做些什么";
        if (h < 14) return "中午好，我是 Aegis，先好好吃顿饭吧";
        if (h < 18) return "下午好，我是 Aegis，有什么想聊的";
        if (h < 23) return "晚上好，我是 Aegis，今天辛苦了";
        return "早些休息吧，我是 Aegis，需要我帮什么吗";
    }

    function refreshGreeting() {
        var el = document.querySelector(".hero-title");
        if (el) el.textContent = greetingByHour(new Date().getHours());
    }

    // 问候区淡出/淡入（尊重减少动效偏好时直接切换）
    function reduceMotion() {
        return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }
    function hideHero() {
        if (heroEl.style.display === "none") return;
        if (reduceMotion()) { heroEl.style.display = "none"; return; }
        heroEl.classList.add("hero-hidden");
        setTimeout(function () { heroEl.style.display = "none"; }, 340);
    }
    function showHero() {
        heroEl.style.display = "";
        if (reduceMotion()) { heroEl.classList.remove("hero-hidden"); return; }
        void heroEl.offsetWidth; // 强制回流，让淡入过渡生效
        heroEl.classList.remove("hero-hidden");
    }

    function groupOf(timeStr) {
        if (!timeStr) return "更早";
        var d = new Date(String(timeStr).replace(/-/g, "/"));
        if (isNaN(d.getTime())) return "更早";
        var now = new Date();
        var day = function (x) { return new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime(); };
        var diff = (day(now) - day(d)) / 86400000;
        if (diff < 1) return "今天";
        if (diff < 2) return "昨天";
        return "更早";
    }

    function esc(text) {
        var div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }

    function scrollToBottom(force) {
        var nearBottom = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 120;
        if (force || nearBottom) {
            chatScroll.scrollTo({ top: chatScroll.scrollHeight });
        }
    }

    // ── 主题切换 ──────────────────────────────────────
    function applyTheme(theme) {
        if (theme === "dark") document.documentElement.setAttribute("data-theme", "dark");
        else document.documentElement.removeAttribute("data-theme");
        try { localStorage.setItem("home-theme", theme); } catch (e) { /* ignore */ }
    }
    function initTheme() {
        var saved = null;
        try { saved = localStorage.getItem("home-theme"); } catch (e) { /* ignore */ }
        if (!saved) {
            saved = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
        }
        applyTheme(saved);
    }
    $("themeToggle").addEventListener("click", function () {
        applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
    });

    // ── 侧边栏折叠（桌面）/ 抽屉（移动） ──────────────
    function isMobile() { return window.matchMedia("(max-width: 767px)").matches; }
    function closeDrawer() { app.classList.remove("sidebar-open"); }

    $("sidebarToggle").addEventListener("click", function () {
        if (isMobile()) app.classList.add("sidebar-open");
        else app.classList.toggle("sidebar-collapsed");
    });
    $("sidebarClose").addEventListener("click", closeDrawer);
    $("backdrop").addEventListener("click", closeDrawer);
    window.addEventListener("resize", function () {
        if (!isMobile()) app.classList.remove("sidebar-open");
    });

    // ── 会话列表（真实数据）───────────────────────────
    function loadSessions() {
        return API.get("/api/aichat/sessions").then(function (data) {
            sessions = data || [];
            renderSessions($("sessionSearch").value);
        }).catch(function (e) {
            sessions = [];
            renderSessions("");
            showToast("会话列表加载失败: " + e.message, "error");
        });
    }

    function renderSessions(keyword) {
        var kw = (keyword || "").trim().toLowerCase();
        sessionListEl.innerHTML = "";

        var groups = [];
        sessions.forEach(function (s) {
            var title = s.session_title || "新对话";
            if (kw && title.toLowerCase().indexOf(kw) === -1) return;
            var g = groupOf(s.last_message_time || s.created_time);
            var bucket = groups.find(function (x) { return x.name === g; });
            if (!bucket) { bucket = { name: g, items: [] }; groups.push(bucket); }
            bucket.items.push(s);
        });

        if (!groups.length) {
            var empty = document.createElement("div");
            empty.className = "session-empty";
            empty.textContent = kw ? "没有匹配的会话" : "暂无历史会话，发条消息开始吧";
            sessionListEl.appendChild(empty);
            return;
        }

        groups.forEach(function (g) {
            var t = document.createElement("div");
            t.className = "session-group-title";
            t.textContent = g.name;
            sessionListEl.appendChild(t);

            g.items.forEach(function (s) {
                var item = document.createElement("div");
                item.className = "session-item" + (s.session_id === activeId ? " active" : "");

                var dot = document.createElement("span");
                dot.className = "session-dot";
                var title = document.createElement("span");
                title.className = "session-title";
                title.textContent = s.session_title || "新对话";
                var del = document.createElement("button");
                del.className = "session-del";
                del.type = "button";
                del.title = "删除会话";
                del.innerHTML =
                    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    '<path d="M18 6L6 18M6 6l12 12"/></svg>';
                del.addEventListener("click", function (e) {
                    e.stopPropagation();
                    removeSession(s.session_id, s.session_title);
                });

                item.appendChild(dot);
                item.appendChild(title);
                item.appendChild(del);
                item.title = s.session_title || "新对话";
                item.addEventListener("click", function () {
                    selectSession(s.session_id);
                    if (isMobile()) closeDrawer();
                });
                sessionListEl.appendChild(item);
            });
        });
    }

    $("sessionSearch").addEventListener("input", function () {
        renderSessions(this.value);
    });

    // ── 会话操作 ──────────────────────────────────────
    function stopStreaming() {
        if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; }
        isSending = false;
        setSendState();
    }

    function selectSession(id) {
        stopStreaming();
        API.get("/api/aichat/sessions/" + id + "/messages").then(function (msgs) {
            activeId = id;
            currentMessages = msgs || [];
            renderSessions($("sessionSearch").value);
            renderMessages(currentMessages);
            var s = sessions.find(function (x) { return x.session_id === id; });
            topbarTitle.textContent = s ? (s.session_title || "新对话") : "新对话";
            hideHero();
            clearBtn.style.display = "";
            scrollToBottom(true);
        }).catch(function (e) {
            showToast("加载会话失败: " + e.message, "error");
        });
    }

    function newChat() {
        stopStreaming();
        activeId = null;
        currentMessages = [];
        renderSessions($("sessionSearch").value);
        messagesEl.innerHTML = "";
        topbarTitle.textContent = "新对话";
        showHero();
        clearBtn.style.display = "none";
        refreshGreeting();
        inputEl.focus();
        if (isMobile()) closeDrawer();
    }

    function removeSession(id, title) {
        showConfirm("将删除会话「" + (title || "新对话") + "」及其全部消息，且不可恢复。", "删除会话")
            .then(function (ok) {
                if (!ok) return;
                API.del("/api/aichat/sessions/" + id).then(function () {
                    showToast("会话已删除", "success");
                    if (id === activeId) newChat();
                    loadSessions();
                }).catch(function (e) {
                    showToast("删除失败: " + e.message, "error");
                });
            });
    }

    // ── 长期记忆面板（跨会话 Context Memory）──────────────
    function openMemoryPanel() {
        API.get("/api/aichat/memory").then(function (list) {
            list = list || [];
            var overlay = document.createElement("div");
            overlay.className = "modal-overlay active";
            var html = '<div class="modal-box memory-box">'
                + '<div class="modal-title">长期记忆</div>'
                + '<p class="memory-hint">跨会话保留的关于你的信息，Aegis 会在对话中自然运用。</p>'
                + '<div class="memory-list">';
            if (!list.length) {
                html += '<div class="memory-empty">暂无记忆。聊到「我叫 XX」「我住在 XX」「我喜欢 XX」时会自动记下，也可手动添加。</div>';
            } else {
                list.forEach(function (m) {
                    var label = CATEGORY_LABELS[m.category] || "其他";
                    html += '<div class="memory-item" data-key="' + escapeAttrText(m.memory_key) + '">'
                        + '<div class="memory-text"><span class="memory-cat">' + escapeText(label) + '</span>'
                        + '<span class="memory-val">' + escapeText(m.memory_value) + '</span></div>'
                        + '<button class="memory-del" type="button" title="删除" aria-label="删除">×</button></div>';
                });
            }
            html += '</div><div class="modal-actions">'
                + '<button class="btn btn-secondary" data-act="add">添加</button>'
                + '<button class="btn btn-primary" data-act="close">关闭</button>'
                + '</div></div>';
            overlay.innerHTML = html;
            document.body.appendChild(overlay);

            function close() { overlay.remove(); }

            overlay.querySelector('[data-act="close"]').onclick = close;
            overlay.onclick = function (e) { if (e.target === overlay) close(); };

            overlay.querySelector('[data-act="add"]').onclick = function () {
                showPrompt("记忆名称（如：姓名 / 所在地 / 偏好）", "", "例如：姓名").then(function (k) {
                    if (!k) return;
                    showPrompt("记忆内容", "", "例如：小明").then(function (v) {
                        if (!v) return;
                        API.post("/api/aichat/memory", { key: k, value: v }).then(function () {
                            showToast("已添加记忆", "success");
                            close(); openMemoryPanel();
                        }).catch(function (e) { showToast("添加失败: " + e.message, "error"); });
                    });
                });
            };

            Array.prototype.forEach.call(overlay.querySelectorAll(".memory-del"), function (btn) {
                btn.onclick = function () {
                    var item = btn.closest(".memory-item");
                    var key = item.getAttribute("data-key");
                    showConfirm("删除记忆「" + key + "」？", "删除记忆").then(function (ok) {
                        if (!ok) return;
                        API.del("/api/aichat/memory/" + encodeURIComponent(key)).then(function () {
                            showToast("已删除", "success");
                            close(); openMemoryPanel();
                        }).catch(function (e) { showToast("删除失败: " + e.message, "error"); });
                    });
                };
            });
        }).catch(function (e) {
            showToast("记忆加载失败: " + e.message, "error");
        });
    }
    $("memoryBtn").addEventListener("click", function () {
        openMemoryPanel();
        if (isMobile()) closeDrawer();
    });

    // 「清空当前会话」= 删除当前会话并回到新对话
    clearBtn.addEventListener("click", function () {
        if (!activeId) return;
        var s = sessions.find(function (x) { return x.session_id === activeId; });
        removeSession(activeId, s ? s.session_title : "");
    });
    $("newChatBtn").addEventListener("click", newChat);

    // ── 消息渲染 ──────────────────────────────────────
    function renderMessages(list) {
        messagesEl.innerHTML = "";
        (list || []).forEach(function (m) {
            var isUser = m.message_role === "user";
            messagesEl.appendChild(createMessage({
                role: isUser ? "user" : "assistant",
                content: m.message_content || "",
                time: hhmm(m.created_time),
            }));
        });
    }

    function createMessage(m) {
        var isUser = m.role === "user";
        var div = document.createElement("div");
        div.className = "msg " + (isUser ? "user" : "ai");

        var avatar = document.createElement("div");
        avatar.className = "msg-avatar";
        avatar.textContent = isUser ? "ME" : "AI";

        var main = document.createElement("div");
        main.className = "msg-main";

        var bubble = document.createElement("div");
        bubble.className = "msg-bubble";
        bubble.textContent = m.content || "";
        main.appendChild(bubble);

        var meta = document.createElement("div");
        meta.className = "msg-meta";
        var time = document.createElement("span");
        time.textContent = m.time || "";
        meta.appendChild(time);

        if (!isUser) {
            meta.appendChild(buildCopyBtn(m.content || ""));
        }
        main.appendChild(meta);

        div.appendChild(avatar);
        div.appendChild(main);
        return div;
    }

    function buildCopyBtn(text) {
        var copy = document.createElement("button");
        copy.className = "msg-copy";
        copy.type = "button";
        copy.innerHTML = COPY_SVG + "复制";
        copy.addEventListener("click", function () { copyText(text); });
        return copy;
    }

    function copyText(text) {
        var done = function () { showToast("已复制到剪贴板", "success"); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text, done); });
        } else {
            fallbackCopy(text, done);
        }
    }
    function fallbackCopy(text, done) {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;opacity:0;";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) { showToast("复制失败，请手动选择复制", "error"); }
        ta.remove();
    }

    // ── 示例卡片 / 快捷提示词 ─────────────────────────
    EXAMPLES.forEach(function (ex) {
        var card = document.createElement("div");
        card.className = "example-card";
        card.innerHTML =
            '<div class="ex-icon ' + ex.icon + '">' +
                '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                (EX_ICONS[ex.icon] || EX_ICONS.c1) + "</svg></div>" +
            '<div class="ex-title"></div>' +
            '<div class="ex-desc"></div>';
        card.querySelector(".ex-title").textContent = ex.title;
        card.querySelector(".ex-desc").textContent = ex.desc;
        card.addEventListener("click", function () {
            inputEl.value = ex.prompt;
            autoResize();
            setSendState();
            sendMessage();
        });
        $("exampleGrid").appendChild(card);
    });

    CHIPS.forEach(function (text) {
        var chip = document.createElement("button");
        chip.className = "chip";
        chip.type = "button";
        chip.textContent = text;
        chip.addEventListener("click", function () {
            inputEl.value = text;
            inputEl.focus();
            autoResize();
            setSendState();
        });
        $("chips").appendChild(chip);
    });

    // ── 发送（真实流式对话）───────────────────────────
    function setSendState() {
        var hasText = !!inputEl.value.trim();
        sendBtn.disabled = isSending ? false : !hasText;
        sendBtn.classList.toggle("stop", isSending);
        sendBtn.innerHTML = isSending ? STOP_SVG : SEND_SVG;
        sendBtn.title = isSending ? "停止生成" : "发送";
    }

    inputEl.addEventListener("input", function () {
        autoResize();
        setSendState();
    });
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    sendBtn.addEventListener("click", function () {
        if (isSending) { stopStreaming(); finalizeReply(null); }
        else sendMessage();
    });

    function autoResize() {
        inputEl.style.height = "auto";
        inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
    }

    function ensureSession() {
        if (activeId) return Promise.resolve(activeId);
        return API.post("/api/aichat/sessions", { title: "新对话" }).then(function (s) {
            activeId = s.session_id;
            currentMessages = [];
            clearBtn.style.display = "";
            return activeId;
        });
    }

    function sendMessage() {
        var text = inputEl.value.trim();
        if (!text || isSending) return;

        inputEl.value = "";
        autoResize();
        isSending = true;
        setSendState();

        if (!activeId) {
            hideHero();
            topbarTitle.textContent = text.length > 14 ? text.slice(0, 14) + "…" : text;
        }

        messagesEl.appendChild(createMessage({ role: "user", content: text, time: hhmm() }));
        scrollToBottom(true);

        var placeholder = createMessage({ role: "assistant", content: "", time: "" });
        var bubble = placeholder.querySelector(".msg-bubble");
        bubble.innerHTML = '<span class="loading-dots"><span></span><span></span><span></span></span>';
        var meta = placeholder.querySelector(".msg-meta");
        var timeSpan = meta.querySelector("span");
        messagesEl.appendChild(placeholder);
        scrollToBottom(true);

        ensureSession().then(function (sid) {
            abortCtrl = new AbortController();
                // 统一走 aegisFetch：自动带上 X-Aegis-Token（T01）；
                // auth.js 万一未加载则退回原生 fetch，不让页面直接白屏。
                var doFetch = (typeof window.aegisFetch === "function")
                    ? window.aegisFetch
                    : window.fetch.bind(window);
                return doFetch("/api/aichat/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sid, message: text, use_web: webEnabled }),
                signal: abortCtrl.signal,
            }).then(function (resp) {
                if (!resp.ok) {
                    return resp.json().catch(function () { return {}; }).then(function (err) {
                        throw new Error(err.msg || ("HTTP " + resp.status));
                    });
                }
                return readSSE(resp, function (evt) { handleEvent(evt, bubble, timeSpan, meta, placeholder); });
            });
        }).catch(function (e) {
            if (e && e.name === "AbortError") return; // 用户主动停止，finalizeReply 已处理
            bubble.classList.add("plain");
            bubble.textContent = "请求失败: " + (e.message || "未知错误");
            bubble.parentElement.classList.add("error");
            showToast(e.message || "请求失败", "error");
            finalizeReply(null);
        });

        // SSE 事件处理
        function handleEvent(evt, bubble, timeSpan, meta, placeholder) {
            var type = evt.type;
            // 未知事件类型（如 Agent 的 run_start / agent_step / tool_call …）
            // 一律直接忽略：不抛错、不改动 DOM，保证旧页面零回归。
            if (KNOWN_TYPES.indexOf(type) === -1) {
                return;
            }
            if (type === "chunk" || type === "notice") {
                if (!bubble.dataset.text) {
                    bubble.dataset.text = "";
                    bubble.textContent = "";
                }
                bubble.dataset.text += evt.content || "";
                bubble.textContent = bubble.dataset.text;
                var cursor = document.createElement("span");
                cursor.className = "typing-cursor";
                bubble.appendChild(cursor);
                scrollToBottom(false);
            } else if (type === "provider") {
                if (evt.label) {
                    modelTag.textContent = evt.label + (evt.model ? " · " + evt.model : "");
                }
            } else if (type === "web") {
                // 联网状态提示：仅在实际发起过检索时由后端下发；不写入回复、不入库
                var main = placeholder.querySelector(".msg-main");
                if (main) {
                    var old = main.querySelector(".web-chip");
                    if (old) old.remove();
                    var chip = document.createElement("div");
                    chip.className = "web-chip " + (evt.ok ? "web-ok" : "web-off");
                    if (evt.ok) {
                        chip.textContent = "🌐 已联网检索 " + (evt.count || 0) + " 条相关结果";
                    } else {
                        chip.textContent = "未检索到相关资料，已基于本地知识回答";
                    }
                    main.insertBefore(chip, main.firstChild);
                }
            } else if (type === "blocked") {
                placeholder.classList.add("blocked");
                bubble.dataset.text = evt.content || "";
                bubble.textContent = evt.content || "";
            } else if (type === "error") {
                placeholder.classList.add("error");
                bubble.dataset.text = evt.content || "";
                bubble.textContent = evt.content || "";
            } else if (type === "done") {
                finalizeReply(bubble, timeSpan, meta);
            }
        }
    }

    // 结束一次回复：去光标、补时间戳与复制按钮、恢复输入
    function finalizeReply(bubble, timeSpan, meta) {
        stopStreaming();
        if (bubble) {
            var cursor = bubble.querySelector(".typing-cursor");
            if (cursor) cursor.remove();
            if (bubble.dataset.text !== undefined) bubble.textContent = bubble.dataset.text;
            timeSpan.textContent = hhmm();
            var old = meta.querySelector(".msg-copy");
            if (old) old.remove();
            meta.appendChild(buildCopyBtn(bubble.dataset.text || ""));
        }
        // 刷新会话列表：首条消息后服务端会自动生成标题、更新排序
        loadSessions().then(function () {
            if (activeId) {
                var s = sessions.find(function (x) { return x.session_id === activeId; });
                if (s) topbarTitle.textContent = s.session_title || "新对话";
            }
        });
        inputEl.focus();
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

    // ── 「联网搜索」开关 ──────────────────────────────
    // 开：每条消息先联网检索并过滤相关结果再回答（闲聊问候除外，由后端判断）
    // 关：请求体 use_web=false，后端完全不发起任何网络请求，仅本地知识作答
    if (webToggle) {
        webToggle.addEventListener("click", function () {
            webEnabled = !webEnabled;
            webToggle.classList.toggle("active", webEnabled);
            webToggle.setAttribute("aria-checked", String(webEnabled));
            showToast(webEnabled ? "联网搜索已开启" : "联网搜索已关闭，将仅基于本地知识回答", "info");
        });
    }

    // ── 进入 /aegis（编码 Agent 工作台）的入口 ──────────
    // 用 JS 挂载而不是改模板，避免旧页面模板多处同步；挂载失败也不影响本页其它功能。
    (function mountAegisEntry() {
        var nav = document.querySelector(".side-nav");
        if (!nav || document.getElementById("aegisEntry")) return;
        var link = document.createElement("a");
        link.className = "side-nav-item";
        link.id = "aegisEntry";
        link.href = "/aegis";
        link.title = "打开 Aegis 编码 Agent：读代码 / 改文件 / 跑命令";
        link.innerHTML =
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg>';
        var label = document.createElement("span");
        label.textContent = "Aegis 编码工作台";
        link.appendChild(label);
        nav.appendChild(link);
    })();

    // ── 初始化 ────────────────────────────────────────
    initTheme();
    refreshGreeting();
    loadSessions();
    setSendState();
    // 进入页面即可直接输入：自动聚焦输入框
    inputEl.focus();
})();
