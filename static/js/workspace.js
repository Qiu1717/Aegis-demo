/* =============================================================
   Aegis 智能体工作台 · 交互
   -------------------------------------------------------------
   加载顺序：/token.js → auth.js（AegisAuth，自动带 X-Aegis-Token）→ workspace.js
   接口：
     GET/POST/DELETE /api/aichat/sessions            会话（历史侧边栏）
     GET  /api/aichat/sessions/<id>/messages         历史消息
     POST /api/agent/run                             任务执行（SSE）
     GET  /api/status                                模型与链路状态
     GET  /api/settings/workspace                    工作区
   SSE 事件（/api/agent/run）：
     run_start(task_id,skills) / agent_step(step,title) / tool_call(tool,summary,risk,call_id)
     tool_result(output) / chunk(content) / run_end(stats)
   所有动态文本一律 textContent 写入，禁止 innerHTML 拼接（防 XSS）。
   ============================================================= */

(function () {
    "use strict";

    var AUTH = window.AegisAuth;
    var extras = window.AegisWorkspaceExtras;
    var modelPicker = window.AegisModelPicker;

    function $(id) { return document.getElementById(id); }

    var elSide = $("awSide");
    var elOverlay = $("awOverlay");
    var elMenuBtn = $("awMenuBtn");
    var elToast = $("awToast");
    var elInput = $("awInput");
    var elSend = $("awSend");
    var elForm = $("awComposer");
    var elMsgs = $("awMsgs");
    var elConvList = $("awConvList");
    var elWsChip = $("awWsChip");
    var elStatus = $("awStatus");
    var elStatusText = $("awStatusText");
    var elPermSelect = $("awPermSelect");
    var elThinkSelect = $("awThinkSelect");
    var viewHome = $("awViewHome");
    var viewChat = $("awViewChat");
    var elScroll = $("awScroll");
    var elCards = $("awCards");
    var elBusy = $("awBusy");
    var elBusyText = $("awBusyText");
    var elBusyStop = $("awBusyStop");
    var elRetry = $("awRetry");
    var elRetryText = $("awRetryText");
    var elRetryBtn = $("awRetryBtn");
    var elSkillModal = $("awSkillModal");
    var elSkillList = $("awSkillList");
    var elSkillLoading = $("awSkillLoading");
    var elSkillClose = $("awSkillClose");
    var elSkillMask = $("awSkillMask");

    var elTask = $("awTask");
    var elTaskTitle = $("awTaskTitle");
    var elTaskStatus = $("awTaskStatus");
    var elTaskStatusText = $("awTaskStatusText");
    var elTaskMeta = $("awTaskMeta");
    var elSteps = $("awSteps");
    var elTools = $("awTools");
    var elApprovals = $("awApprovals");
    var elTaskEmpty = $("awTaskEmpty");

    /* sessionId：当前会话；sending：是否有任务在跑（唯一的发送按钮控制位）；
       hasWorkspace：是否已设置工作区 —— 决定编码类卡片是否可用。
       lastPrompt / retryable：失败重试所需的上下文。
       markStopped：本轮的「用户主动停止」标记写入器，由 send() 每轮重建。
       存在的理由：停止走的是 AbortError 分支，与「超时中止」共用同一条 catch，
       若不区分就会把用户主动停止误报成「任务超时未结束」。 */
    var state = { sessionId: null, sending: false,
                  awaitingApproval: false, approvalSince: 0,
                  hasWorkspace: false, lastPrompt: "", retryable: false,
                  markStopped: null, newIntent: 0, lastAttachments: [] };

    /* 单次任务等待的硬上限（毫秒）。需大于后端的审批等待上限
       （DEFAULT_APPROVAL_TIMEOUT = 300s），避免误伤正常的审批等待；
       仅用于兜底：流异常不关闭时强制中止，保证发送状态能复位。 */
    var RUN_WATCHDOG_MS = 600000;

    /* 停顿看门狗：距离「上一个事件」超过这个时间仍无任何动静，就判定流已
       僵死并中止，保证发送状态一定会被解除（多轮对话不再需要重启应用）。
       审批等待期间不计时（那时本来就该等用户决策）。
       取值 120s：明显大于模型首字/长思考的合理间隔，不会误伤正常慢响应。 */
    var RUN_STALL_MS = 120000;

    /* 单次任务里「累计等待审批」的总闸（毫秒）。后端每个审批各等 120s，
       若一次任务连续多次请求审批，累计仍可能拖很久。这里封顶，
       超时即中止，保证发送状态一定解除。 */
    var RUN_APPROVAL_CAP_MS = 240000;

    /* 三点菜单共用浮层（挂在 body 上，避免被侧栏 overflow 裁切） */
    var menuEl = null;

    /* 当前正在跑的那一轮（用于「有新消息就中止上一轮」）。
       id 用于辨认归属：新一轮开始后，旧一轮的收尾不能再动 activeRun。

       taskId：**后端任务 id**，来自 run_start 事件。停止时必须拿它调
       `POST /api/agent/cancel`，否则只是前端不再读取数据，后端 AgentRunner
       仍在跑工具、仍在消耗 token —— 表现为「界面已停、后端仍在跑」。
       此前的实现根本没保存这个值，也没有调用过 cancel 接口。 */
    var runSeq = 0;

    /* 「新建对话」意图序号：resetNew 递增、openSession 也递增（用户改主意）。
       createSessionNow 的异步回调据此判断自己是否已过期 —— 过期就只更新
       侧边栏列表，**不动 state.sessionId、不动视图**，避免把用户从正在看的
       会话里硬拽走（那正是「新建后欢迎页/对话莫名消失」的成因）。 */
    var newIntentSeq = 0;

    var activeRun = { id: 0, controller: null, watchdog: null, stallTimer: null,
                      taskId: "", cancelNotified: false };

    /* 真正停止一轮：先通知后端取消（置 cancel_event、杀子进程、清理挂起审批），
       再断开前端流读取。两步都要——只做前者，界面停而后端空转；
       只做后者，后端继续跑并可能写文件/执行命令。 */
    function abortActiveRun(notifyBackend) {
        var tid = activeRun.taskId;
        /* 只通知一次：重复点击、或 abort 后的收尾流程再次调用时不要重复发 */
        if (notifyBackend !== false && tid && AUTH && !activeRun.cancelNotified) {
            activeRun.cancelNotified = true;
            try {
                AUTH.post("/api/agent/cancel", { task_id: tid }).catch(function () {
                    /* 任务可能已自然结束（后端返回 404），这不算失败：
                       用户点停止时看到的是「已停止」，不该再弹错误。
                       真正需要提示的是网络层异常，而那会由 .catch 静默兜住。 */
                });
            } catch (e) { /* 忽略：前端流仍要断开 */ }
        }
        try { if (activeRun.controller) activeRun.controller.abort(); } catch (e) { /* 忽略 */ }
        try { if (activeRun.watchdog) clearTimeout(activeRun.watchdog); } catch (e) { /* 忽略 */ }
        try { if (activeRun.stallTimer) clearInterval(activeRun.stallTimer); } catch (e) { /* 忽略 */ }
        activeRun.controller = null;
        activeRun.watchdog = null;
        activeRun.stallTimer = null;
        activeRun.taskId = "";
        activeRun.cancelNotified = false;
        state.awaitingApproval = false;
        state.approvalSince = 0;
    }

    /* ── 基础 ───────────────────────────────────────────── */

    var toastTimer = null;
    function toast(msg) {
        if (!elToast) return;
        elToast.textContent = msg || "";
        elToast.classList.add("is-on");
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { elToast.classList.remove("is-on"); }, 2400);
    }

    function setStatus(stateName, text) {
        if (elStatus) elStatus.setAttribute("data-state", stateName);
        if (elStatusText) elStatusText.textContent = text;
    }

    function setView(name) {
        var isChat = name === "chat";
        if (viewHome) viewHome.classList.toggle("is-active", !isChat);
        if (viewChat) viewChat.classList.toggle("is-active", isChat);
        /* 切视图属于「用户主动要看最新」，此时强制置底并恢复跟随；
           流式过程中的自动跟随仍受 stickBottom 约束（见 scrollBottom）。 */
        if (isChat) {
            stickBottom = true;
            scrollBottom(true);
        }
    }

    /* 滚动：流式输出期间每来一个 chunk 都会调这里。若无条件置底，用户手动
       上滚查看历史会被持续拽回底部（此前是 scrollBottom() 直写 scrollHeight）。
       改为「只有用户本来就贴着底部时才跟随」—— 这是聊天类产品（含主流
       对话 App）的标准行为：主动上滚 = 想读历史，不该被强行拉回。 */
    var stickBottom = true;

    function nearBottom() {
        if (!elScroll) return true;
        return (elScroll.scrollHeight - elScroll.scrollTop - elScroll.clientHeight) < 48;
    }

    function scrollBottom(force) {
        if (!elScroll) return;
        if (force === true || stickBottom) {
            elScroll.scrollTop = elScroll.scrollHeight;
        }
    }

    /* 绑定用户滚动意图：距底 >48px 视为「想看历史」，此后不再自动跟随；
       回到底部则恢复跟随。 */
    if (elScroll) {
        elScroll.addEventListener("scroll", function () {
            stickBottom = nearBottom();
        }, { passive: true });
    }

    /* ── 卡片状态机 ────────────────────────────────────── */

    /* 四态同步：is-busy（运行中不可再点）/ is-selected（已用过）/ is-loading
       （进度条）/ aria-disabled（无工作区不可用）。集中在一处更新，
       避免多个事件各改一半导致状态互相打架。

       runState 记录「上一轮怎么结束的」：done / stopped / failed。
       卡片角标据此显示，让视觉状态与真实请求状态一致 —— 停止后如果卡片
       还留着「生成中」的转圈，就成了「界面在跑、后端已停」的假象。 */
    var lastRunState = "";
    var RUN_STATE_TEXT = {
        running: "生成中", stopped: "已停止", done: "已完成", failed: "失败",
        /* armed：已把推荐指令填入输入框、等待用户确认发送。
           这是「待命」而非「已结束」，与 stopped 必须区分开 ——
           否则刚点一下卡片就显示「已停止」，语义完全错位（缺陷3）。 */
        armed: "待发送"
    };

    function syncCards() {
        if (!elCards) return;
        var cards = elCards.querySelectorAll(".aw-card");
        for (var i = 0; i < cards.length; i++) {
            var c = cards[i];
            var needsWs = c.getAttribute("data-needs-ws") === "1";
            var blocked = needsWs && !state.hasWorkspace;
            var busy = state.sending;

            c.classList.toggle("is-busy", busy);
            c.classList.toggle("is-loading", busy);
            /* 运行中不展示上一轮的残留结论，避免与「正在生成」自相矛盾 */
            if (busy) c.setAttribute("data-run-state", "running");
            else if (c.getAttribute("data-run-state") === "running") {
                c.setAttribute("data-run-state", lastRunState || "");
            }
            /* 已选但未发送 → 待命。用户一旦改输入框或发送，就不再是待命态。 */
            if (!busy && c.classList.contains("is-selected")
                && (c.getAttribute("data-run-state") || "") === "") {
                c.setAttribute("data-run-state", "armed");
            }
            var badge = c.querySelector(".aw-card-state");
            if (badge) {
                var st = c.getAttribute("data-run-state") || "";
                badge.textContent = RUN_STATE_TEXT[st] || "";
            }
            /* 无工作区 → 语义上禁用（aria + tabindex 一起，避免键盘仍可触发） */
            c.setAttribute("aria-disabled", blocked ? "true" : "false");
            c.setAttribute("tabindex", blocked ? "-1" : "0");
            c.title = blocked ? "该操作需要先设置工作区（点击左下角文件夹图标选择目录）" : "";
        }
    }

    /* 记录本轮结束方式，供卡片角标展示 */
    function setRunState(s) { lastRunState = s || ""; }

    /* 用户手动改输入框 → 之前选中的卡片不再是「待发送」，
       避免角标停留在「待发送」却与用户实际输入不一致。 */
    function clearArmedState() {
        if (state.sending) return;
        if (lastRunState === "armed") return;   /* 本轮本来就是待命态 */
        if (!elCards) return;
        var armed = elCards.querySelectorAll('.aw-card[data-run-state="armed"]');
        for (var i = 0; i < armed.length; i++) {
            armed[i].setAttribute("data-run-state", "");
        }
    }

    function markCardUsed(kind) {
        if (!elCards || !kind) return;
        var cards = elCards.querySelectorAll(".aw-card");
        for (var i = 0; i < cards.length; i++) {
            if (cards[i].getAttribute("data-kind") === kind) {
                cards[i].classList.add("is-selected");
                /* 待命而非「已结束」：等用户真正发送后才由 setRunState 改写。
                   同时清掉其它卡片的 armed 态，一次只保留一个待命入口。 */
                cards[i].setAttribute("data-run-state", "armed");
            } else if (cards[i].getAttribute("data-run-state") === "armed") {
                cards[i].setAttribute("data-run-state", "");
            }
        }
        syncCards();
    }

    /* 失败重试条：只有真正失败才出现，成功后立即隐藏 */
    function showRetry(prompt, reason) {
        state.lastPrompt = prompt || "";
        state.retryable = !!prompt;
        if (!elRetry) return;
        if (!prompt) { elRetry.hidden = true; return; }
        if (elRetryText) {
            elRetryText.textContent = "上一轮未完成：" + (reason || "未知错误");
        }
        elRetry.hidden = false;
    }

    function hideRetry() {
        state.retryable = false;
        if (elRetry) elRetry.hidden = true;
    }

    /* 生成中指示：run_start 后显示，首个 chunk 到达即隐藏 */
    function showBusy(text) {
        if (elBusyText) elBusyText.textContent = text || "Aegis 正在思考…";
        if (elBusy) elBusy.hidden = false;
    }
    function hideBusy() { if (elBusy) elBusy.hidden = true; }

    function autosize() {
        if (!elInput) return;
        elInput.style.height = "auto";
        elInput.style.height = Math.min(elInput.scrollHeight, 200) + "px";
    }

    /* 唯一的发送按钮控制点：state.sending 或有输入为空 → 置灰。
       它被调用得足够频繁（输入、发送前、收尾），因此内部必须**永不抛异常**，
       否则一次 DOM 差异就把「按钮能否点击」这件事永久锁死（历史 Bug）。 */
    function syncSend() {
        if (!elSend) return;
        try {
            var has = elInput && elInput.value && elInput.value.trim().length > 0;
            /* 只由「是否有内容」决定可点性。
               此前条件是 `state.sending || !has`：只要上一轮还没收尾（后台步骤未跑完、
               流迟迟不关、审批未决……），按钮就一直置灰、悬停显示禁止图标。
               现在改为：有内容就能发；若上一轮仍在跑，send() 会先中止它。 */
            has = has || (extras && extras.files().length > 0);
            elSend.disabled = !state.sending && (!has || (extras && extras.uploading()));
            if (modelPicker) {
                modelPicker.setBusy(state.sending);
                if (!state.sending && modelPicker.saving()) elSend.disabled = true;
            }
            if (extras) extras.setBusy(state.sending);
            /* 生成中把按钮切成「停止」语义：视觉上给出明确反馈，
               也避免用户误以为可以再发一条造成重复提交。 */
            elSend.classList.toggle("is-stop", !!state.sending);
            elSend.setAttribute("aria-label", state.sending ? "停止生成" : "发送");
            elSend.setAttribute("title", state.sending ? "停止生成" : "发送");
            if (elInput) elInput.setAttribute("aria-busy", state.sending ? "true" : "false");
        } catch (e) {
            /* 兜底：宁可放开按钮，也不能让用户彻底发不出消息 */
            elSend.disabled = false;
        }
        syncCards();
    }

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }

    /* ── 侧边栏抽屉（移动端）────────────────────────────── */

    function openDrawer() {
        if (elSide) elSide.classList.add("is-open");
        if (elOverlay) elOverlay.classList.add("is-on");
    }

    function closeDrawer() {
        if (elSide) elSide.classList.remove("is-open");
        if (elOverlay) elOverlay.classList.remove("is-on");
    }

    if (elMenuBtn) {
        elMenuBtn.addEventListener("click", function () {
            if (window.innerWidth > 900) {
                window.dispatchEvent(new CustomEvent("aegis:sidebar-toggle"));
                return;
            }
            if (elSide && elSide.classList.contains("is-open")) closeDrawer();
            else openDrawer();
        });
    }

    if (elOverlay) elOverlay.addEventListener("click", closeDrawer);

    /* ── 状态条 ─────────────────────────────────────────── */

    function loadStatus() {
        if (!AUTH) return;

        AUTH.get("/api/settings/workspace").then(function (res) {
            var d = res || {};
            var path = d.path || d.workspace || "";
            if (elWsChip) {
                elWsChip.textContent = path ? ("工作区：" + path) : "工作区：未设置";
                elWsChip.title = path || "";
            }
            /* 工作区状态直接决定编码类卡片能否使用，必须同步，
               否则用户点了构建卡片才知道没设工作区。 */
            state.hasWorkspace = !!path;
            setWsHint(path);
            syncCards();
        }).catch(function () {
            if (elWsChip) elWsChip.textContent = "工作区：未设置";
            state.hasWorkspace = false;
            setWsHint("");
            syncCards();
        });
    }

    /* ── 历史记录 ───────────────────────────────────────── */

    /* 后端字段是 session_id / session_title（不是 id / title），两者都兜底 */
    function sidOf(s) { return s.id != null ? s.id : s.session_id; }
    function titleOf(s) { return s.title || s.session_title || "新对话"; }
    /* 置顶态字段名兼容 is_pinned / isPinned，缺失按 false 处理 */
    /* 置顶态字段名兼容 is_pinned / isPinned。
       **必须用宽松真值判断，不能用 `=== true`**：
       后端 SQLite/MySQL 的 is_pinned 是整型，真实接口返回的是 int 1 / 0，
       而 JS 里 `1 === true` 为 false —— 用严格等于会让置顶判断**完全失效**
       （排序不生效、is-pinned 样式不触发）。已实测确认接口返回 type=int。 */
    function isPinnedOf(s) {
        if (!s) return false;
        var v = (s.is_pinned != null) ? s.is_pinned : s.isPinned;
        /* 排除 "0" / "false" 这类字符串假值，其余按真值处理 */
        if (v === 0 || v === "0" || v === false || v === "false") return false;
        return !!v;
    }

    /* 最近一次成功拉取的会话列表（本地缓存）。
       作用：删除后可**先**从缓存里剔除并立即重渲染，用户不必等接口往返；
       随后仍以 loadSessions() 的服务端结果为准做最终校正（服务端是唯一真相）。
       并发删除时每层都基于同一份最新数组计算，不存在「各自持有旧计数」的问题。 */
    var sessionsCache = [];
    /* 拉取序号：并发删除会同时发起多次 loadSessions，若响应乱序返回，
       旧响应会覆盖新结果，导致计数回退（删了又变多）。只接受最大序号的响应。 */
    var sessionsReqSeq = 0;
    var sessionsRendered = "";

    function loadSessions() {
        if (!AUTH) return Promise.resolve();
        var mySeq = ++sessionsReqSeq;
        return AUTH.get("/api/aichat/sessions").then(function (res) {
            if (mySeq < sessionsReqSeq) return;   /* 已有更新的请求在飞，丢弃本次结果 */
            sessionsCache = res || [];
            renderSessions(sessionsCache);
        }).catch(function () {
            /* 拉取失败**不要清空侧边栏** —— 那会让用户「所有历史对话突然消失」，
               只是一次网络抖动而已。保留现有列表继续可用即可。
               （此前这里 textContent="" 是个真实存在的体验坑。） */
            if (mySeq === sessionsReqSeq && !sessionsCache.length && elConvList) {
                /* 确实一条都没有（首次进入且拉取失败）才显示空态 */
                elConvList.textContent = "";
            }
        });
    }

    function renderSessions(list) {
        if (!elConvList) return;
        list = list || [];
        var signature = JSON.stringify([state.sessionId, list.map(function (s) {
            return [sidOf(s), titleOf(s), isPinnedOf(s), !!s.archived];
        })]);
        if (signature === sessionsRendered) return;
        sessionsRendered = signature;

        var main = [];
        var archived = [];
        list.forEach(function (s) {
            var sid = sidOf(s);
            if (sid == null) return;
            if (s.archived) archived.push(s);
            else main.push(s);
        });

        /* 后端 list_sessions 已 ORDER BY is_pinned DESC；这里再做一次稳定排序兜底，
           保证置顶项恒在顶部且同组内维持返回顺序（现代引擎 Array.sort 为稳定排序）。 */
        main.sort(function (a, b) {
            return (isPinnedOf(b) ? 1 : 0) - (isPinnedOf(a) ? 1 : 0);
        });

        elConvList.textContent = "";
        if (!main.length) {
            elConvList.appendChild(el("div", "aw-side-empty", "还没有会话"));
        } else {
            main.forEach(function (s) { elConvList.appendChild(buildConvRow(s)); });
        }

        renderArchived(archived);
    }

    /* 单条会话行：文本 + ⋯ 三点菜单按钮（删除改由菜单承担，旧 × 已移除） */
    function buildConvRow(s) {
        var sid = sidOf(s);
        var row = el("div", "aw-conv" +
            (String(sid) === String(state.sessionId) ? " is-active" : "") +
            (isPinnedOf(s) ? " is-pinned" : ""));
        var title = titleOf(s);

        var text = el("span", "aw-conv-text", title);
        text.title = title;
        row.appendChild(text);

        var more = el("button", "aw-conv-more", "⋯");
        more.type = "button";
        more.title = "更多操作";
        more.setAttribute("aria-label", "更多操作");
        more.setAttribute("aria-haspopup", "menu");
        more.addEventListener("click", function (e) {
            e.stopPropagation();
            openMenuFor(more, sid, s);
        });
        row.appendChild(more);

        row.addEventListener("click", function () {
            openSession(sid);
            closeDrawer();
        });

        return row;
    }

    /* 归档区：可折叠分组，数据不丢、仅移出主列表 */
    function renderArchived(archived) {
        var wrap = $("awArch");
        var body = $("awArchBody");
        var count = $("awArchCount");
        if (!wrap || !body) return;

        /* 计数必须**先**无条件同步，再判断是否隐藏。
           此前「无归档时先 wrap.hidden=true 直接 return」导致：
           删掉最后一条归档会话后，数字仍停在删除前的旧值（因为
           return 发生在 count.textContent 赋值之前）。改为顺序化后，
           0 条时数字也归零，与列表真实状态一致。 */
        if (count) count.textContent = String((archived && archived.length) || 0);

        body.textContent = "";
        if (!archived || !archived.length) {
            wrap.hidden = true;
            return;
        }
        wrap.hidden = false;

        var inner = el("div", "aw-arch-inner");
        archived.forEach(function (s) { inner.appendChild(buildConvRow(s)); });
        body.appendChild(inner);
    }

    /* 正在删除中的会话 id 集合：防止同一条被连点两次发出重复请求
       （后端第二次会 404，前端也会误报「删除失败」）。 */
    var deletingSessions = {};

    function deleteSession(sid) {
        if (!AUTH) return Promise.resolve();
        var key = String(sid);
        if (deletingSessions[key]) return Promise.resolve();
        deletingSessions[key] = true;

        /* ① 乐观更新：先从本地缓存剔除并立刻重渲染，列表项与计数马上减少。
              失败时会在 catch 里用服务端数据回滚，不会留下「界面少了但后端没删」的错觉。 */
        var snapshot = sessionsCache;
        sessionsCache = sessionsCache.filter(function (s) {
            return String(sidOf(s)) !== key;
        });
        if (sessionsCache.length !== snapshot.length) {
            renderSessions(sessionsCache);
        }
        if (key === String(state.sessionId)) resetNewKeepList();

        return AUTH.del("/api/aichat/sessions/" + encodeURIComponent(sid))
            .then(function () {
                /* ② 服务端是唯一真相：删除成功后重新拉一次，
                      让列表、计数、置顶/归档顺序与后端完全一致。
                      即便本次响应没带上最新数据，也能靠这一步自愈。 */
                return loadSessions();
            })
            .then(function () {
                toast("会话已删除");
            })
            .catch(function () {
                /* ③ 失败回滚：用删除前的快照重渲染，界面恢复原状 */
                sessionsCache = snapshot;
                renderSessions(sessionsCache);
                toast("删除失败，请稍后重试");
            })
            .then(function () {
                delete deletingSessions[key];
            });
    }

    /* ── 三点菜单 ───────────────────────────────────────── */

    /* 共用浮层：挂在 body 上（position:fixed），避免被侧栏 overflow 裁切；
       同时打开时自动覆盖其它已开的菜单（彼此独立、互不影响）。 */
    function ensureMenuEl() {
        if (menuEl) return menuEl;
        menuEl = el("div", "aw-menu");
        menuEl.setAttribute("role", "menu");
        document.body.appendChild(menuEl);
        return menuEl;
    }

    function closeMenu() {
        if (menuEl) menuEl.classList.remove("is-open");
    }

    function addMenuItem(menu, label, opts, onClick) {
        var item = el("button", "aw-menu-item" + (opts && opts.danger ? " is-danger" : ""));
        item.type = "button";
        item.setAttribute("role", "menuitem");
        item.textContent = label;
        item.addEventListener("click", function (e) {
            e.stopPropagation();
            onClick();
        });
        menu.appendChild(item);
    }

    function openMenuFor(trigger, sid, s) {
        var menu = ensureMenuEl();
        menu.textContent = "";
        var pinned = isPinnedOf(s);
        var archived = !!s.archived;

        addMenuItem(menu, pinned ? "取消置顶" : "置顶", null, function () {
            closeMenu();
            togglePin(sid, pinned);
        });
        addMenuItem(menu, archived ? "取消归档" : "归档", null, function () {
            closeMenu();
            toggleArchive(sid, archived);
        });
        addMenuItem(menu, "删除", { danger: true }, function () {
            closeMenu();
            confirmDelete(sid);
        });

        menu.classList.add("is-open");
        positionMenu(menu, trigger);
    }

    /* 边界自适应：靠近视口底部 / 右侧时自动上翻或左移，绝不被裁切 */
    function positionMenu(menu, trigger) {
        var rect = trigger.getBoundingClientRect();
        var mw = menu.offsetWidth || 160;
        var mh = menu.offsetHeight || 120;
        var vw = window.innerWidth, vh = window.innerHeight;

        var left = rect.right - mw;
        if (left < 8) left = 8;
        if (left + mw > vw - 8) left = vw - mw - 8;

        var top = rect.bottom + 6;
        var originY = "top";
        if (top + mh > vh - 8) {
            top = rect.top - mh - 6;
            originY = "bottom";
        }
        if (top < 8) top = 8;

        menu.style.left = left + "px";
        menu.style.top = top + "px";
        menu.style.transformOrigin = originY + " right";
    }

    function togglePin(sid, isPinned) {
        if (!AUTH) return;
        /* 注意：AUTH.put 已解包为业务 data，禁止再取 .data（历史双重解包坑） */
        AUTH.put("/api/aichat/sessions/" + encodeURIComponent(sid),
                 { action: "pin", is_pinned: !isPinned })
            .then(function () { return loadSessions(); })
            .then(function () { toast(isPinned ? "已取消置顶" : "已置顶"); })
            .catch(function () { toast("操作失败，请稍后重试"); });
    }

    function toggleArchive(sid, isArchived) {
        if (!AUTH) return;
        AUTH.put("/api/aichat/sessions/" + encodeURIComponent(sid) + "/archive",
                 { archived: !isArchived })
            .then(function () { return loadSessions(); })
            .then(function () { toast(isArchived ? "已取消归档" : "已归档"); })
            .catch(function () { toast("操作失败，请稍后重试"); });
    }

    function confirmDelete(sid) {
        if (state.sending && String(sid) === String(state.sessionId)) {
            toast("请先停止当前任务，再删除会话"); return;
        }
        var run = function () { deleteSession(sid); };
        if (typeof window.showConfirm === "function") {
            window.showConfirm("删除后不可恢复，确定要删除这个会话吗？", "删除会话")
                .then(function (ok) { if (ok) run(); });
            return;
        }
        if (typeof window.confirm === "function") {
            if (window.confirm("删除后不可恢复，确定要删除这个会话吗？")) run();
            return;
        }
        run();
    }

    function openSession(sid) {
        if (!AUTH) return;
        if (state.sending) { toast("请先停止当前任务，再切换会话"); return; }
        /* 用户主动点了某个会话 → 作废尚未完成的「新建」意图。
           否则 createSessionNow 的异步回调会把 state.sessionId 改写成新会话、
           并可能把视图拉回欢迎页，让用户刚点开的会话凭空消失。 */
        newIntentSeq++;
        state.newIntent = newIntentSeq;
        var intent = state.newIntent;
        state.sessionId = sid;
        if (extras) extras.clear();
        state.lastAttachments = [];
        if (elMsgs) elMsgs.textContent = "";
        resetTask();
            AUTH.get("/api/aichat/sessions/" + encodeURIComponent(sid) + "/messages")
            .then(function (res) {
                if (intent !== state.newIntent || String(state.sessionId) !== String(sid)) return;
                var msgs = res || [];
                renderMessages(msgs);
                /* 空会话（刚新建、还没发过任何消息）**显示欢迎页**而不是空白对话区 ——
                   否则用户点开自己刚建的那条会话，看到的是一片空白，
                   完全没有「欢迎回来 / 选个入口开始」的引导（缺陷：新建的对话没有欢迎页）。
                   有消息的会话才进聊天视图。 */
                setView(msgs.length ? "chat" : "home");
                setStatus("idle", "空闲");
                return loadSessions();
            })
            .catch(function () {
                if (intent === state.newIntent && String(state.sessionId) === String(sid)) toast("加载会话失败");
            });
    }

    function renderMessages(list) {
        if (!elMsgs) return;
        elMsgs.textContent = "";
        (list || []).forEach(function (m) {
            if (!m) return;
            addMsg(m.message_role === "user" ? "user" : "assistant", m.message_content || "");
        });
    }

    /* 新建对话：主动向后端建会话，并立即在侧边栏插入高亮。
       为什么必须主动建：此前这里只 loadSessions()，而新会话是后端在
       /api/agent/run 的 _ensure_session 里**惰性**创建的 —— 于是
       「点新建任务」时后端根本没有会话存在，列表当然不会多出新项，
       用户要等发出第一条消息后（链尾的 loadSessions）才看得到。
       结果就是：连建几个对话来处理不同工作时，没发过消息的那几个
       在侧边栏完全不存在，无法点击切换。

       改为：点下即 POST /api/aichat/sessions 建会话，成功后把新项
       乐观插入 sessionsCache 顶部并重渲染（与删除的乐观更新对称）。
       失败时回退到「等首次发送时再建」的旧行为，不阻断用户。 */
    function resetNew() {
        if (state.sending) { toast("请先停止当前任务，再新建会话"); return; }
        /* 「新建」意图标记：createSessionNow 是异步的，回调里要靠它判断
           「用户这段时间有没有改主意去别的会话」，避免把视图强行拉回欢迎页。 */
        state.newIntent = ++newIntentSeq;
        state.sessionId = null;
        if (extras) extras.clear();
        state.lastAttachments = [];
        if (elMsgs) elMsgs.textContent = "";
        resetTask();
        hideBusy();
        /* 新任务开始时清掉上一轮的「已用卡片」标记与失败重试条，
           否则用户新建任务后仍看到上次的重试提示，语义已经过期。 */
        hideRetry();
        if (elCards) {
            var used = elCards.querySelectorAll(".aw-card.is-selected");
            for (var i = 0; i < used.length; i++) used[i].classList.remove("is-selected");
        }
        setView("home");
        setStatus("idle", "空闲");
        createSessionNow(state.newIntent);
    }

    /* 主动建会话 + 乐观插入侧边栏。
       intent：本次「新建」意图的序号（由 resetNew 传入）。异步回调里据此判断
              用户是否已改主意（点了别的会话），是则**不**再改动视图，避免把用户
              从正在看的会话里硬拉回欢迎页。
       返回 Promise（成功 resolve 新 id，失败 resolve null），调用方不阻塞。 */
    function createSessionNow(intent) {
        if (!AUTH) return Promise.resolve(null);
        return AUTH.post("/api/aichat/sessions", { title: "新对话" }).then(function (res) {
            var sid = sidOf(res || {});
            if (sid == null) throw new Error("创建会话失败");
            /* 意图已过期：用户在建会话的这几百毫秒里点了别的会话。
               此时**只补列表**（让新会话仍出现在侧边栏），绝不改 state.sessionId
               也不动视图 —— 否则会把用户从刚点开的会话拽走，表现为
               「新建后对话/欢迎页莫名消失」。 */
            var stale = (intent != null && intent !== state.newIntent);
            if (!stale) state.sessionId = sid;
            /* 乐观插入：与后端 create_session 的落库值保持一致 ——
               is_pinned=0（后端建会话时**不**置顶），排序靠 created_time 倒序
               把它顶到前面（后端 ORDER BY is_pinned DESC, last_message_time DESC,
               created_time DESC）。若这里乐观置顶=1 而后端是 0，
               刷新后新项会从顶部跳走，产生位置抖动。 */
            sessionsCache = [{
                session_id: sid, session_title: "新对话", is_pinned: 0, archived: false
            }].concat(sessionsCache);
            renderSessions(sessionsCache);
            /* 再拉一次以校正（标题可能已被后端按首条消息改写、置顶态对齐），
               序号守卫保证不会用旧响应覆盖。 */
            return loadSessions().then(function () { return sid; });
        }).catch(function () {
            /* 建会话失败不阻断用户：state.sessionId 仍为 null，
               发送时后端 _ensure_session 会自行补建，行为退回改动前。 */
            return loadSessions().then(function () { return null; });
        });
    }

    /* resetNew 的「不重新拉列表」变体。
       删除当前会话时，调用方紧接着会 loadSessions()（或已做过乐观更新），
       若这里再拉一次，会出现旧数据覆盖新数据的闪烁，且并发删除时
       多个 in-flight 请求返回顺序不保证，可能让计数回退。 */
    function resetNewKeepList() {
        state.newIntent = ++newIntentSeq;
        state.sessionId = null;
        if (extras) extras.clear();
        state.lastAttachments = [];
        if (elMsgs) elMsgs.textContent = "";
        resetTask();
        hideBusy();
        hideRetry();
        if (elCards) {
            var used = elCards.querySelectorAll(".aw-card.is-selected");
            for (var i = 0; i < used.length; i++) used[i].classList.remove("is-selected");
        }
        setView("home");
        setStatus("idle", "空闲");
    }

    function addMsg(role, text) {
        if (!elMsgs) return null;
        var wrap = el("div", "aw-msg");
        wrap.setAttribute("data-role", role);
        wrap.appendChild(el("div", "aw-msg-avatar", role === "user" ? "你" : "A"));

        var body = el("div", "aw-msg-body");
        body.appendChild(el("div", "aw-msg-role", role === "user" ? "你" : "Aegis"));
        var textEl = el("div", "aw-msg-text", text || "");
        body.appendChild(textEl);
        wrap.appendChild(body);

        elMsgs.appendChild(wrap);
        scrollBottom();
        return textEl;
    }

    /* ── 任务面板 ───────────────────────────────────────── */

    function resetTask() {
        if (elTask) elTask.hidden = true;
        if (elSteps) elSteps.textContent = "";
        if (elTools) elTools.textContent = "";
        if (elApprovals) elApprovals.textContent = "";
        if (elTaskEmpty) {
            elTaskEmpty.hidden = false;
            elTaskEmpty.textContent = "正在启动…";
        }
    }

    function showTask(title) {
        if (!elTask) return;
        elTask.hidden = false;
        if (elTaskTitle) elTaskTitle.textContent = title || "任务";
        if (elTaskStatus) elTaskStatus.setAttribute("data-state", "running");
        if (elTaskStatusText) elTaskStatusText.textContent = "运行中";
        if (elTaskMeta) elTaskMeta.textContent = "";
    }

    function addStep(idx, title) {
        if (!elSteps) return;
        if (elTaskEmpty) elTaskEmpty.hidden = true;
        var row = el("div", "aw-step");
        row.appendChild(el("span", "aw-step-idx", (idx != null ? idx : "-") + "."));
        row.appendChild(el("span", "aw-step-text", title || ""));
        elSteps.appendChild(row);
        scrollBottom();
    }

    function addTool(evt) {
        if (!elTools) return;
        if (elTaskEmpty) elTaskEmpty.hidden = true;
        var box = el("div", "aw-tool");
        box.setAttribute("data-call-id", evt.call_id || "");
        box.appendChild(el("div", "aw-tool-name", evt.tool || "工具"));
        if (evt.summary) box.appendChild(el("div", "aw-tool-cmd", evt.summary));
        elTools.appendChild(box);
        scrollBottom();
    }

    /* 联网搜索结果的展示文本（后端 tool_result 带的是结构化的 data）。
       只把「搜索引擎 + 条数 + 标题/链接」铺开，够用即可，不做富文本。 */
    function webSearchText(data, err) {
        if (!data && err) return String(err.message || err.code || "联网搜索失败");
        if (!data) return "";
        var results = data.results;
        if (!Array.isArray(results)) return "";
        var lines = ["[" + (data.provider || "联网") + "] " + (data.query || "")
            + " · " + (data.count != null ? data.count : results.length) + " 条"];
        for (var i = 0; i < results.length && i < 10; i++) {
            var r = results[i] || {};
            lines.push((i + 1) + ". " + (r.title || "(无标题)"));
            if (r.url) lines.push("   " + r.url);
        }
        if (results.length > 10) {
            lines.push("…（还有 " + (results.length - 10) + " 条）");
        }
        return lines.join("\n");
    }

    function fillToolResult(evt) {
        if (!elTools) return;
        var out = evt.output != null ? evt.output : (evt.content != null ? evt.content : "");
        /* 后端 tool_result 没有 output/content 字段（结果在 data 里），
           因此联网搜索原本会渲染成一片空白。这里补上 data → 文本的转换；
           其余工具保持原行为，不改动既有展示。 */
        if (!out && evt.tool === "web_search") {
            out = webSearchText(evt.data, evt.error);
        }
        var id = evt.call_id || "";
        var nodes = elTools.querySelectorAll(".aw-tool");
        var target = null;
        for (var i = nodes.length - 1; i >= 0; i--) {
            if (!id || nodes[i].getAttribute("data-call-id") === id) { target = nodes[i]; break; }
        }
        if (!target) target = nodes[nodes.length - 1];
        if (!target) return;
        target.appendChild(el("div", "aw-tool-out", String(out).slice(0, 2000)));
        scrollBottom();
    }

    function finishTask(evt, ok) {
        if (elTaskStatus) elTaskStatus.setAttribute("data-state", ok ? "done" : "failed");
        if (elTaskStatusText) elTaskStatusText.textContent = ok ? "已完成" : "失败";
        var stats = evt && evt.stats ? evt.stats : null;
        if (stats && elTaskMeta) {
            var parts = [];
            if (stats.tool_calls != null) parts.push("工具 " + stats.tool_calls);
            if (stats.changes != null) parts.push("变更 " + stats.changes);
            if (stats.elapsed_ms != null) parts.push(Math.round(stats.elapsed_ms / 100) / 10 + "s");
            elTaskMeta.textContent = parts.join(" · ");
        }
    }

    /* 审批卡片独立于可折叠的执行详情，不能随详情一起隐藏。 */
    var AP_KIND_LABELS = {
        file_write: "写入文件",
        file_edit: "修改文件",
        command: "执行命令",
        read: "读取文件",
        search: "检索",
    };
    var AP_DECISION_LABELS = {
        allow_once: "允许一次",
        allow_session: "本次会话允许",
        deny: "拒绝",
    };

    function appendApproval(evt) {
        var container = elApprovals || elTools;
        if (!container) {
            /* 兜底：万一任务面板缺失，也不能把用户卡在等待里。
               退化为原生确认弹窗：确定=允许一次，取消=拒绝。 */
            var allow = false;
            try {
                allow = window.confirm
                    ? window.confirm("Aegis 想要执行：" + (evt.title || "工具操作")
                        + "\n\n确定允许吗？（取消 = 拒绝）")
                    : false;
            } catch (e) { /* 忽略 */ }
            resolveApproval(evt.request_id, allow ? "allow_once" : "deny", null, null);
            return;
        }
        if (elTask) elTask.hidden = false;
        if (elTaskEmpty) elTaskEmpty.hidden = true;

        var node = el("div", "aw-approval" + (evt.dangerous ? " is-dangerous" : ""));
        node.setAttribute("data-request-id", evt.request_id || "");

        var head = el("div", "aw-ap-head");
        head.appendChild(el("span", "aw-ap-kind",
            AP_KIND_LABELS[evt.kind] || evt.kind || "操作"));
        head.appendChild(el("span", "aw-ap-title", evt.title || "需要你确认"));
        node.appendChild(head);

        if (evt.dangerous) {
            node.appendChild(el("div", "aw-ap-warn",
                "高风险操作：只允许「允许一次」，不可在本次会话内豁免。"));
        }
        if (evt.detail) {
            node.appendChild(el("pre", "aw-ap-detail", evt.detail));
        }

        var actions = el("div", "aw-ap-actions");
        var options = (Array.isArray(evt.options) && evt.options.length)
            ? evt.options : ["allow_once", "deny"];
        // 危险操作：即便后端给了 allow_session，前端也强制剔除
        if (evt.dangerous) {
            options = options.filter(function (o) { return o !== "allow_session"; });
        }
        options.forEach(function (decision) {
            var cls = "aw-ap-btn" + (decision === "deny" ? " is-danger"
                : (decision === "allow_session" ? " is-ghost" : " is-primary"));
            var btn = el("button", cls, AP_DECISION_LABELS[decision] || decision);
            btn.type = "button";
            btn.addEventListener("click", function () {
                resolveApproval(evt.request_id, decision, node, actions);
            });
            actions.appendChild(btn);
        });
        node.appendChild(actions);

        container.appendChild(node);

        /* 审批必须让用户一眼看到，否则他会以为「发送按钮卡死」：
           ① 展开任务面板；② 弹 toast；③ 把卡片滚动进可视区；
           ④ 聚焦第一个决策按钮（可直接用回车/空格决策）。 */
        if (elTask) elTask.hidden = false;
        if (elTaskEmpty) elTaskEmpty.hidden = true;
        setStatus("waiting", "等待你确认");
        toast("需要你确认一项操作，请在对话下方选择允许或拒绝");
        try {
            if (node.scrollIntoView) {
                node.scrollIntoView({ block: "center", behavior: "smooth" });
            }
        } catch (e) { /* 忽略 */ }
        try {
            var firstBtn = actions.querySelector("button");
            if (firstBtn) firstBtn.focus();
        } catch (e) { /* 忽略 */ }
    }

    function resolveApproval(requestId, decision, node, actions) {
        if (!requestId || !AUTH) return;
        // 对「无卡片 / 无按钮容器」的兜底场景保持安全，绝不因 DOM 缺失抛错
        var btns = [];
        try {
            btns = actions && actions.querySelectorAll
                ? Array.prototype.slice.call(actions.querySelectorAll("button")) : [];
        } catch (e) { btns = []; }
        var i;
        for (i = 0; i < btns.length; i++) btns[i].disabled = true;

        AUTH.post("/api/agent/approve", {
            request_id: requestId,
            decision: decision
        }).then(function () {
            if (node) {
                node.classList.add("is-resolved");
                node.appendChild(el("span", "aw-ap-result",
                    "已提交：" + (AP_DECISION_LABELS[decision] || decision)));
            }
            setStatus("running", "执行中");
        }).catch(function (err) {
            for (i = 0; i < btns.length; i++) btns[i].disabled = false;
            toast("提交审批失败：" + (err && err.message ? err.message : "未知错误"));
        });
    }

    function markApprovalResolved(evt) {
        var container = elApprovals || elTools;
        if (!container) return;
        var nodes = container.querySelectorAll(".aw-approval");
        var rid = evt.request_id || "";
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute("data-request-id") === rid) {
                nodes[i].classList.add("is-resolved");
                var buttons = nodes[i].querySelectorAll("button");
                for (var j = 0; j < buttons.length; j++) buttons[j].disabled = true;
            }
        }
        setStatus("running", "执行中");
    }

    /* ── 发送（SSE）─────────────────────────────────────── */

    function ensureSession() {
        if (state.sessionId != null) return Promise.resolve(state.sessionId);
        return AUTH.post("/api/aichat/sessions", { title: "新对话" }).then(function (res) {
            var sid = sidOf(res || {});
            if (sid == null) throw new Error("创建会话失败");
            state.sessionId = sid;
            return sid;
        });
    }

    /* 停止生成 —— 发送按钮与「生成中」提示条上的停止按钮共用这一条路径。
       必须做到三件事，缺一就会出现「界面停了但状态没干净」：
         1. 通知后端取消（abortActiveRun 内部完成，避免后端空转）
         2. 立刻解除 sending 锁 —— 否则输入框一直是禁用态，用户无法继续对话
         3. 卡片/按钮从「生成中」切到「已停止」，并清掉上一轮的取消标记
       真正的状态收尾仍由 send() 链尾的 .then 兜底（它一定会执行），
       这里只做「立即可感知」的部分，不等网络往返。 */
    function stopRun() {
        if (!state.sending) return;
        /* 通知本轮 promise 链的收尾逻辑：这是「用户主动停止」而非超时/失败，
           收尾时据此保留已生成内容、不弹错误、不覆盖「已停止」状态。 */
        try { if (typeof state.markStopped === "function") state.markStopped(); }
        catch (e) { /* 忽略 */ }
        abortActiveRun(true);
        state.sending = false;
        state.awaitingApproval = false;
        state.approvalSince = 0;
        hideBusy();
        setStatus("idle", "已停止");
        setRunState("stopped");
        /* 停止不是失败，不弹重试；把输入框交还给用户并聚焦，
           同时 syncSend 让发送按钮恢复可用（取决于输入框是否有内容）。 */
        syncCards();
        syncSend();
        if (elInput) {
            try { elInput.focus(); } catch (e) { /* 忽略 */ }
        }
        toast("已停止生成");
    }

    function send() {
        if (!elInput) return;
        /* 生成中再次点击 = 停止，而不是「中止上一轮并立刻发新的一条」。
           此前无条件走「中止 + 重发」，用户想打断时反而会把自己的旧问题
           再跑一遍，是明确的交互错误。 */
        if (state.sending) {
            stopRun();
            return;
        }
        if (modelPicker && modelPicker.saving()) { toast("请等待模型切换完成"); return; }
        var files = extras ? extras.files() : [];
        var text = elInput.value.trim() || (files.length ? "请分析所附文件。" : "");
        if (!text) return;
        if (extras && extras.uploading()) { toast("请等待文件上传完成"); return; }
        if (!AUTH) { toast("鉴权未就绪，请刷新页面"); return; }

        var myId = ++runSeq;
        state.sending = true;
        state.lastPrompt = text;
        state.lastAttachments = files;
        /* 本轮是否被用户主动停止。与 runId 一起构成「这一轮的因果」：
           只有 myId 仍等于当前轮次时才允许改动标记，避免上一轮的收尾
           误标下一轮（并发场景下会导致新请求刚发出就被判为「已停止」）。 */
        var stoppedByUser = false;
        var finished = false;
        var terminalEvent = null;
        function isCurrentRun() { return myId === runSeq && !stoppedByUser && !finished; }
        function releaseRun() {
            if (watchdog) clearTimeout(watchdog);
            if (stallTimer) clearInterval(stallTimer);
            if (!isCurrentRun()) return;
            finished = true;
            if (activeRun.id === myId) {
                activeRun.controller = null;
                activeRun.watchdog = null;
                activeRun.stallTimer = null;
                activeRun.taskId = "";
                activeRun.cancelNotified = false;
            }
            state.markStopped = null;
            state.awaitingApproval = false;
            state.approvalSince = 0;
            state.sending = false;
            hideBusy();
            syncSend();
            if (elInput) elInput.focus();
        }
        var markStopped = function () { if (myId === runSeq) stoppedByUser = true; };
        state.markStopped = markStopped;
        /* 新一轮开始：清掉上一轮遗留的取消标记，避免 cancel 被误判为「已通知」
           而漏发后端取消（本轮的 taskId 是新的，必须能再取消一次）。 */
        activeRun.cancelNotified = false;
        hideRetry();
        /* 真正开始跑了 → 任何「待发送」标记都作废（角标由 running 接管） */
        if (elCards) {
            var armedCards = elCards.querySelectorAll('.aw-card[data-run-state="armed"]');
            for (var ci = 0; ci < armedCards.length; ci++) {
                armedCards[ci].setAttribute("data-run-state", "");
            }
        }
        syncSend();

        /* 同步准备段：这段任一步抛异常，promise 链就根本没机会建起来，
           state.sending 会永久停在 true —— 表现为发送按钮永久置灰、点击
           无响应，且异常只落在控制台、界面零提示。这里兜住并复位状态。 */
        var answerEl;
        try {
            elInput.value = "";
            autosize();
            setView("chat");
            addMsg("user", text + (files.length ? "\n附件：" + files.map(function (f) { return f.name; }).join("、") : ""));
            if (extras) extras.take();
            answerEl = addMsg("assistant", "");
            resetTask();
            setStatus("running", "运行中");
            showBusy("正在思考");
        } catch (e) {
            state.sending = false;
            try { syncSend(); } catch (e2) { /* 忽略 */ }
            toast("发送失败：界面初始化异常");
            return;
        }
        var acc = "";

        /* 兜底看门狗：任何后端异常（流不关闭、上游挂起、审批长时间未决）都
           不应把发送状态永久卡死。超过硬上限就中止读取，让 promise 链继续
           走完并复位 state.sending，用户至少能重新输入与发送。 */
        var controller = (typeof AbortController !== "undefined")
            ? new AbortController() : null;
        var watchdog = controller ? setTimeout(function () {
            try { controller.abort(); } catch (e) { /* 忽略 */ }
        }, RUN_WATCHDOG_MS) : null;

        /* 停顿看门狗：只要还在持续收到事件就不干预；一旦长时间毫无动静
           （且不是在等用户审批），就判定流已僵死并中止，让收尾逻辑复位
           state.sending。这是「多轮对话不必重启应用」的最后一道保障。 */
        activeRun.id = myId;
        activeRun.controller = controller;
        activeRun.watchdog = watchdog;
        activeRun.stallTimer = null;

        var lastEventAt = Date.now();
        var stallTimer = setInterval(function () {
            if (!isCurrentRun()) { clearInterval(stallTimer); return; }
            try {
                if (state.awaitingApproval) {
                    lastEventAt = Date.now();
                    // 等审批本身是合理的，但累计时长要封顶
                        if (state.approvalSince
                            && Date.now() - state.approvalSince > RUN_APPROVAL_CAP_MS) {
                            activeRun.stallTimer = null;
                            clearInterval(stallTimer);
                        if (controller) { try { controller.abort(); } catch (e) { /* 忽略 */ } }
                    }
                    return;
                }
                if (Date.now() - lastEventAt <= RUN_STALL_MS) return;
                activeRun.stallTimer = null;
                clearInterval(stallTimer);
                if (controller) { try { controller.abort(); } catch (e) { /* 忽略 */ } }
            } catch (e) { /* 忽略 */ }
        }, 5000);
        activeRun.stallTimer = stallTimer;

        /* 首轮要建会话，原实现是 ensureSession().then(...) 严格串行 ——
           建会话那一整个往返（实测数十至数百毫秒）会**直接叠加在首字节之前**，
           表现为「点了发送要等一下才出字」。而 /api/agent/run 本身接受
           session_id 缺省（后端 _ensure_session 会自行新建并回传），
           所以两条路可以并行：先发起 run，建会话的结果只在后续轮次复用。
           这样首字节只剩一次往返。

           副作用控制：run 自己也会建会话（后端 _ensure_session 支持 session_id 缺省），
           因此这里**只在 resetNew 已主动建好会话时才复用它**，避免产生两个重复的空会话。
           正常路径下用户点「新建任务」时 state.sessionId 已就绪（见 createSessionNow），
           首字节只剩一次往返。 */
        var hasSession = state.sessionId != null;
        var sessionReady = hasSession ? Promise.resolve(state.sessionId) : Promise.resolve(null);

        sessionReady.then(function () {
            if (!isCurrentRun()) return;
            showTask(text.length > 40 ? text.slice(0, 40) + "…" : text);
        });

        var token = AUTH.getToken ? AUTH.getToken() : "";
        /* 任务立即发起，不等建会话 —— 这是本次首字节优化的关键 */
        var runStarted = fetch("/api/agent/run", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Aegis-Token": token },
            signal: controller ? controller.signal : undefined,
            body: JSON.stringify({
                message: text,
                attachment_ids: files.map(function (f) { return f.id; }),
                /* 首轮传 null 让后端自己建，省掉串行等待；
                   已有会话时带上，保证多轮上下文连续。 */
                session_id: state.sessionId,
                thinking_level: elThinkSelect ? elThinkSelect.value : "balanced",
                permission_mode: elPermSelect ? elPermSelect.value : "approve",
                use_web: true
            })
        }).then(function (resp) {
            if (!isCurrentRun()) {
                if (resp.body) return resp.body.cancel();
                return;
            }
            if (!resp.ok) {
                return resp.json().catch(function () { return {}; }).then(function (d) {
                    throw new Error((d && d.msg) || ("HTTP " + resp.status));
                });
            }
            return pump(resp, answerEl,
                function (full) { acc = full; },
                function (event) {
                    lastEventAt = Date.now();
                    if (event.type === "run_end" || event.type === "done" || event.type === "error") terminalEvent = event;
                }, isCurrentRun);
        });

        /* 两条并行链路都结束后才算本轮收尾；sessionReady 已在上面挂了
           独立的 then/catch，不会产生未处理拒绝。 */
        Promise.all([runStarted, sessionReady.catch(function () { return null; })])
            .then(function () {
            if (!isCurrentRun()) return;
            // 以「界面实际渲染出的文本」为准判空：只要显示过内容就保留，
            // 真正为空才给占位提示（既不覆盖已显示内容，也不把空响应误判为成功）。
            var shown = (answerEl && answerEl.textContent) ? answerEl.textContent : (acc || "");
            if (!shown.trim() && answerEl) answerEl.textContent = "（本次没有返回文本结果）";
            var reason = terminalEvent && terminalEvent.stats && terminalEvent.stats.stopped_by;
            var failed = terminalEvent && (terminalEvent.type === "error" || ["error", "no_model", "tool_errors"].indexOf(reason) >= 0);
            var stopped = reason === "user" || reason === "denied";
            hideBusy();
            setStatus("idle", failed ? "任务失败" : (stopped ? "已停止" : "空闲"));
            finishTask(terminalEvent, !failed && !stopped);
            if (stopped && elTaskStatusText) elTaskStatusText.textContent = "已停止";
            setRunState(failed ? "failed" : (stopped ? "stopped" : "done"));
            syncCards();
            releaseRun();
            // 侧栏刷新不能阻塞下一轮发送。
            loadSessions();
        }).catch(function (err) {
            if (!isCurrentRun()) return;
            // 该处理函数自身绝不能再抛异常：一旦抛出，后面的复位 .then()
            // 会被跳过，state.sending 再次永久卡死且变成未捕获 rejection。
            try {
                var isAbort = !!(err && (err.name === "AbortError"
                    || (err.message && String(err.message).indexOf("abort") >= 0)));
                /* 两类 AbortError 必须分开处理，混为一谈会产生误导：
                   · stoppedByUser —— 用户主动点的停止：预期路径，静默收尾；
                   · isAbort 且非主动 —— 看门狗超时（RUN_WATCHDOG_MS / RUN_STALL_MS）
                     或审批封顶。此时**后端可能仍在跑**（我们只是不再读它的流），
                     所以除了断流还必须发 /api/agent/cancel 让它真停，
                     并且如实告知「已停止等待」而不是谎称「已停止生成」。 */
                if (isAbort) {
                    abortActiveRun(true);
                    state.sending = false;
                    state.awaitingApproval = false;
                    state.approvalSince = 0;
                    hideBusy();
                    setStatus("idle", "已停止等待");
                    setRunState("stopped");
                    syncCards();
                    syncSend();
                    if (answerEl && !(answerEl.textContent || "").trim()) {
                        answerEl.textContent = "（已停止等待后端返回）";
                    }
                    toast("后端响应超时，已停止等待并通知后端取消");
                    if (elInput) { try { elInput.focus(); } catch (e) { /* 忽略 */ } }
                    return;
                }
                var msg = (err && err.message) ? err.message : "未知错误";
                if (answerEl) {
                    answerEl.classList.add("is-error");
                    answerEl.textContent = "请求失败：" + msg;
                }
                hideBusy();
                setStatus("idle", "空闲");
                setRunState("failed");
                syncCards();
                toast("发送失败：" + msg);
                /* 只有真出错才给一键重试，重试用本轮原文（state.lastPrompt），不丢上下文。 */
                showRetry(text, msg);
            } catch (e) { /* 忽略：复位逻辑必须继续执行 */ }
        }).then(releaseRun);
    }

    /* SSE 解析 */
    function pump(resp, answerEl, onText, onEvent, isCurrentRun) {
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = "";
        var acc = "";

        function cancelReader() {
            try {
                var cancelled = reader.cancel();
                if (cancelled && cancelled.catch) cancelled.catch(function () {});
            } catch (e) { /* 读取收尾不阻塞发送状态复位 */ }
        }

        function step() {
            return reader.read().then(function (r) {
                if (!isCurrentRun()) {
                    cancelReader();
                    return acc;
                }
                buf = (buf + (r.done ? decoder.decode() : decoder.decode(r.value, { stream: true }))).replace(/\r\n/g, "\n");
                if (r.done && buf.trim()) buf += "\n\n";

                var idx;
                while ((idx = buf.indexOf("\n\n")) !== -1) {
                    var raw = buf.substring(0, idx);
                    buf = buf.substring(idx + 2);

                    var lines = raw.split("\n");
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i];
                        if (line.indexOf("data:") !== 0) continue;
                        var ds = line.substring(5).trim();
                        if (!ds) continue;
                        var evt;
                        try { evt = JSON.parse(ds); } catch (e) { continue; }
                        if (!evt) continue;
                        /* 单个事件的渲染失败绝不能中断整条流：
                           一旦这里抛出，pump 的 promise 会直接拒绝，后续事件
                           （尤其是 approval_request）全部被丢弃 —— 表现就是
                           「审批卡片不出现 + 发送按钮一直转」。故逐个吞掉。 */
                        try {
                            handle(evt, answerEl, function (full) { acc = full; });
                        } catch (e) { /* 忽略：继续处理后续事件 */ }
                        // 每收到一个事件就打一次心跳，供停顿看门狗判断是否僵死
                        if (onEvent) { try { onEvent(evt); } catch (e) { /* 忽略 */ } }
                        if (evt.type === "run_end" || evt.type === "done" || evt.type === "error") {
                            if (onText) onText(acc);
                            cancelReader();
                            return acc;
                        }
                    }
                }
                if (onText) onText(acc);
                return r.done ? acc : step();
            });
        }
        return step();
    }

    function handle(evt, answerEl, onText) {
        var t = evt.type;

        if (t === "run_start") {
            if (evt.session_id != null) {
                state.sessionId = evt.session_id;
                // 使任务开始前发出的列表请求失效，避免旧默认标题覆盖新标题。
                sessionsReqSeq++;
                var session = sessionsCache.filter(function (s) {
                    return String(sidOf(s)) === String(evt.session_id);
                })[0];
                if (!session) {
                    session = { session_id: evt.session_id };
                    sessionsCache.unshift(session);
                }
                session.session_title = evt.session_title || titleOf(session);
                if (session.title != null) session.title = session.session_title;
                renderSessions(sessionsCache);
            }
            /* 抓住 task_id：没有它就无法在用户点「停止」时通知后端取消。 */
            if (evt.task_id) {
                activeRun.taskId = String(evt.task_id);
                activeRun.cancelNotified = false;
            }
            if (evt.title) showTask(evt.title);
            /* 任务已真正开始（而不是刚点发送），此时才显示「生成中」指示。
               在此之前 ensureSession 可能还要等一会儿网络。 */
            showBusy(evt.title ? ("正在处理：" + evt.title) : "Aegis 正在思考…");
        } else if (t === "phase") {
            setStatus("running", evt.title || "处理中");
            showBusy(evt.title || "Aegis 正在处理…");
        } else if (t === "agent_step") {
            addStep(evt.step, evt.title);
            setStatus("running", "正在思考");
            showBusy("正在思考");
        } else if (t === "tool_call") {
            addTool(evt);
            showBusy("正在准备工具调用");
        } else if (t === "tool_start") {
            var toolNames = {read_file:"读取文件", list_dir:"浏览目录", search_code:"搜索代码",
                write_file:"写入文件", edit_file:"修改文件", web_search:"联网搜索", read_skill_resource:"读取技能资源"};
            var activity = evt.tool === "run_command" ? "正在运行命令"
                : "正在调用工具：" + (toolNames[evt.tool] || evt.tool || "工具");
            setStatus("running", activity);
            showBusy(activity);
        } else if (t === "terminal") {
            showBusy("正在运行命令");
        } else if (t === "tool_result") {
            fillToolResult(evt);
            showBusy("正在思考");
            setStatus("running", "正在思考");
        } else if (t === "approval_request") {
            state.awaitingApproval = true;
            if (!state.approvalSince) state.approvalSince = Date.now();
            showBusy("等待你的批准…");
            /* 关键：权限模式为「需用户批准」时，Runner 会发出审批请求并
               阻塞等待（最长 DEFAULT_APPROVAL_TIMEOUT=300s）。本页此前缺少该
               分支，用户看不到任何提示也无法决策，SSE 一直挂着 → pump() 永不
               resolve → state.sending 卡死 → 发送按钮置灰且点击无响应。
               这里补上审批卡片，让用户能真正「允许 / 拒绝」。 */
            appendApproval(evt);
        } else if (t === "approval_resolved") {
            state.awaitingApproval = false;
            state.approvalSince = 0;
            showBusy("Aegis 正在继续…");
            markApprovalResolved(evt);
        } else if (t === "paused") {
            setStatus("paused", "已暂停");
            showBusy("已暂停，等待继续");
        } else if (t === "resumed") {
            setStatus("running", "运行中");
            showBusy("正在思考");
        } else if (t === "chunk" || t === "notice") {
            var acc2 = (answerEl ? answerEl.textContent : "") + (evt.content || "");
            if (answerEl) answerEl.textContent = acc2;
            if (evt.type === "chunk" && evt.content) {
                showBusy("正在生成回复");
                setStatus("running", "正在生成回复");
            }
            /* 不再无条件置底：用户上滑查看历史时保持当前位置（见 scrollBottom）。 */
            scrollBottom();
            // 关键：把累积结果回报给 pump/send 的累加器。
            // 此前只更新 DOM、不回传，导致 acc 始终为空，
            // 末尾 send() 误判「无文本」并把已显示的内容覆盖成占位文案。
            if (onText) onText(acc2);
        } else if (t === "run_end") {
            if (evt.session_id != null) state.sessionId = evt.session_id;
            state.awaitingApproval = false;
            // 执行状态持续到权威结束事件，不因收到部分文本而提前收起。
            hideBusy();
            // 后端 run_end 目前只带 task_id/stats（无 final 字段），
            // 但保留兜底：若将来带回最终文本且界面还没内容，则填充并同步累加器。
            var finalText = evt.final || evt.text || evt.content || "";
            if (finalText && answerEl && !answerEl.textContent) {
                answerEl.textContent = finalText;
                if (onText) onText(finalText);
            }
            finishTask(evt, true);
        } else if (t === "done") {
            /* done 事件带回真实 session_id（首轮是后端刚为本次新建的）。
               此前前端**完全没处理这个事件**，导致 state.sessionId 一直为 null，
               每一轮都在开新会话 —— 看着像在同一个对话里问，实际上上下文每次都断。
               这里把它记下来，后续轮次才能真正延续上下文。 */
            var sidDone = evt.session_id;
            if (sidDone != null) {
                state.sessionId = sidDone;
            }
        } else if (t === "error") {
            state.awaitingApproval = false;
            // 后端 error 事件带 content/reason，此前被静默丢弃（界面无任何说明），
            // 最终以「本次没有返回文本结果」呈现，误导用户。这里显式回显。
            var errText = evt.content || evt.reason || "";
            if (errText && answerEl && !answerEl.textContent) {
                answerEl.classList.add("is-error");
                answerEl.textContent = errText;
                if (onText) onText(errText);
            }
            finishTask(evt, false);
        }
    }

    /* ── 事件 ───────────────────────────────────────────── */

    if (elInput) {
        /* autosize 依赖 scrollHeight，个别内核/隐藏态下可能取不到；
           即便它抛异常，也必须保证 syncSend() 被调用 —— 否则用户输入后
           发送按钮不会亮起（同一个 handler 里前一步挂了会吞掉后一步）。 */
        elInput.addEventListener("input", function () {
            try { autosize(); } catch (e) { /* 忽略：不影响发送 */ }
            /* 用户一旦自己动手改，「待发送」角标就不再成立 */
            clearArmedState();
            syncSend();
        });
        elInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
                e.preventDefault();
                send();
            }
        });
    }

    if (elForm) {
        elForm.addEventListener("submit", function (e) { e.preventDefault(); send(); });
    }

    /* 卡片点击：**只把推荐指令填入输入框并进入待命状态，不自动发送**。

       为什么改成不自动发：入口卡是「给出一个可编辑的起点」，不是「立即执行」。
       自动 send() 会连带触发整套工作流（建会话、跑 Agent、可能调工具），
       用户还没看过、没修改过这句话就已产生实际开销，且：
         · 卡片立刻被标成「已完成/已停止」，与用户尚未确认的事实不符；
         · 无工作区时点「构建」会弹目录框、点「探索」会直接开跑，都属越权操作。
       现在点击只做三件事：填 prompt、标记为「已选（待命）」、聚焦输入框。
       用户按下发送（或 Ctrl+Enter）才真正触发工作流。 */
    var cards = document.querySelectorAll(".aw-card");
    for (var i = 0; i < cards.length; i++) {
        (function (card) {
            card.addEventListener("click", function () {
                /* 运行中：不打断当前任务。卡片已呈 is-busy 态，这里再兜一层，
                   避免「误触就丢掉正在跑的活」。用户想停止请点发送/停止按钮。 */
                if (state.sending) {
                    toast("当前任务进行中，可点发送按钮停止后再试");
                    return;
                }
                var p = card.getAttribute("data-prompt") || "";
                if (!p) return;

                /* 无工作区**不再拦截点击**：填一句 prompt 本身不需要工作区，
                   真正发送时若需要工作区，后端会给明确报错，届时再引导设置。
                   此前直接 return 掉，用户点了毫无反馈。 */
                markCardUsed(card.getAttribute("data-kind"));
                if (elInput) {
                    elInput.value = p;
                    try { autosize(); } catch (e) { /* 忽略 */ }
                    syncSend();
                    /* 停在首页视图，让用户看见自己将要执行什么；
                       聚焦输入框提示「可编辑后发送」。 */
                    setView("home");
                    try { elInput.focus(); } catch (e) { /* 忽略 */ }
                }
            });
        })(cards[i]);
    }

    /* 失败重试：用本轮原文重发，不让用户重新打字 */
    if (elRetryBtn) {
        elRetryBtn.addEventListener("click", function () {
            if (!state.lastPrompt) { hideRetry(); return; }
            if (extras) extras.restore(state.lastAttachments);
            if (elInput) {
                elInput.value = state.lastPrompt;
                try { autosize(); } catch (e) { /* 忽略 */ }
                syncSend();
            }
            setView("home");
            send();
        });
    }

    /* 停止按钮（生成中指示条上） */
    if (elBusyStop) {
        elBusyStop.addEventListener("click", function () {
            stopRun();
        });
    }

    var navNew = $("awNavNew");
    if (navNew) {
        navNew.addEventListener("click", function () {
            resetNew();
            closeDrawer();
            if (elInput) elInput.focus();
        });
    }

    /* ── 技能浮层（替代整页跳转 /skills）─────────────────────
       需求要求「对话在当前界面内完成，不跳转」。原先这里与顶栏按钮都是
       window.location.href="/skills"，会离开工作台、丢失当前对话上下文。
       这里改为页内浮层，读写仍走既有 /api/skills 接口，不新增后端逻辑。 */
    function openSkills() {
        if (!elSkillModal) { toast("技能面板未加载，请刷新页面"); return; }
        elSkillModal.hidden = false;
        if (elSkillLoading) elSkillLoading.hidden = false;
        if (elSkillList) elSkillList.textContent = "";
        loadSkills();
    }

    function closeSkills() {
        if (elSkillModal) elSkillModal.hidden = true;
    }

    function loadSkills() {
        if (!AUTH) return;
        AUTH.get("/api/skills").then(function (res) {
            var list = (res && res.skills) || [];
            renderSkills(list);
        }).catch(function (e) {
            if (elSkillLoading) {
                elSkillLoading.textContent = "加载失败：" + ((e && e.message) || "未知错误");
                elSkillLoading.hidden = false;
            }
        });
    }
    window.addEventListener("aegis:skills-changed", loadSkills);

    function renderSkills(list) {
        if (!elSkillList) return;
        elSkillList.textContent = "";
        if (elSkillLoading) elSkillLoading.hidden = true;

        if (!list.length) {
            var empty = el("div", "aw-skill-empty", "暂无可用技能");
            elSkillList.appendChild(empty);
            return;
        }

        for (var i = 0; i < list.length; i++) {
            (function (sk) {
                var item = el("div", "aw-skill-item" + (sk.enabled ? "" : " is-off"));

                var main = el("div", "aw-skill-main");
                main.appendChild(el("div", "aw-skill-name", sk.name || sk.id || "未命名"));
                var desc = (sk.description || sk.desc || "");
                if (desc) main.appendChild(el("div", "aw-skill-desc", desc));
                item.appendChild(main);

                var btn = el("button", "aw-skill-toggle"
                    + (sk.enabled ? " is-on" : ""), sk.enabled ? "已启用" : "已停用");
                btn.type = "button";
                btn.addEventListener("click", function () {
                    if (btn.disabled) return;          // 防重复提交
                    var next = !sk.enabled;
                    btn.disabled = true;                 // 加载中
                    btn.textContent = "处理中…";
                    AUTH.put("/api/skills/" + encodeURIComponent(sk.id),
                        { enabled: next }).then(function () {
                        sk.enabled = next;
                        item.classList.toggle("is-off", !next);
                        btn.classList.toggle("is-on", next);
                        btn.textContent = next ? "已启用" : "已停用";
                        toast(next ? ("已启用「" + (sk.name || sk.id) + "」")
                                   : ("已停用「" + (sk.name || sk.id) + "」"));
                    }).catch(function (e) {
                        toast("操作失败：" + ((e && e.message) || "未知错误"));
                    }).then(function () {
                        btn.disabled = false;
                    });
                });
                item.appendChild(btn);
                elSkillList.appendChild(item);
            })(list[i]);
        }
    }

    var navSkills = $("awNavSkills");
    if (navSkills) navSkills.addEventListener("click", function () { openSkills(); closeDrawer(); });

    var btnSkills = $("awBtnSkills");
    if (btnSkills) btnSkills.addEventListener("click", openSkills);

    if (elSkillClose) elSkillClose.addEventListener("click", closeSkills);
    if (elSkillMask) elSkillMask.addEventListener("click", closeSkills);
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && elSkillModal && !elSkillModal.hidden) closeSkills();
    });

    /* 设置：复用既有设置弹窗（settings.js 暴露 window.AegisSettings.open） */
    function openSettings() {
        if (window.AegisSettings && typeof window.AegisSettings.open === "function") {
            window.AegisSettings.open();
        } else {
            toast("设置组件未加载，请刷新页面重试");
        }
    }

    var navSettings = $("awNavSettings");
    if (navSettings) {
        navSettings.addEventListener("click", function () { openSettings(); closeDrawer(); });
    }

    var btnSettings = $("awBtnSettings");
    if (btnSettings) btnSettings.addEventListener("click", openSettings);

    /* 设置保存后刷新顶栏与侧栏状态（settings.js 会派发该事件） */
    window.addEventListener("aegis:settings-changed", function () {
        loadStatus();
    });

    /* 工作区按钮的悬停提示：未设置 -> 「未设置工作区」；已设置 -> 完整目录路径 */
    function setWsHint(path) {
        var btn = $("awBtnWs");
        if (!btn) return;
        var label = $("awWsLabel");
        if (label) label.textContent = path ? path.replace(/\\/g, "/").replace(/\/$/, "").split("/").pop() || path : "选择工作区";
        if (path) {
            btn.title = path;
            btn.setAttribute("aria-label", "工作区：" + path);
        } else {
            btn.title = "未设置工作区";
            btn.setAttribute("aria-label", "未设置工作区");
        }
    }

    /* ── 工作区入口：调起系统目录选择框 ──────────────────
       桥接：desktop/app.py 的 AegisApi.open_folder_dialog（原生系统对话框，
       可浏览任意磁盘分区）。约定：取消返回 ""；出错原样抛出（JS 变 rejected
       promise，前端据此提示，绝不静默吞）。 */
    function pickWorkspace() {
        var btn = $("awBtnWs");
        var api = (window.pywebview && window.pywebview.api) || null;

        if (!api || typeof api.open_folder_dialog !== "function") {
            var path = window.prompt("填写本机工作区目录的完整路径：", btn && btn.title !== "未设置工作区" ? btn.title : "");
            if (path && path.trim()) applyWorkspace(path.trim());
            return;
        }
        if (btn) btn.disabled = true;

        Promise.resolve(api.open_folder_dialog()).then(function (path) {
            // 用户取消：保持原状，不做任何写入、不提示，无任何副作用
            if (!path) return;
            applyWorkspace(String(path));
        }).catch(function (err) {
            toast("打开目录选择器失败：" + (err && err.message ? err.message : "未知错误"));
        }).then(function () {
            if (btn) btn.disabled = false;
        });
    }

    /* 路径校验与写入由后端统一负责（存在性 / 是否目录 / 是否可读），
       前端只负责展示完整路径与反馈结果，避免在浏览器里做不可靠的文件系统判断。 */
    function applyWorkspace(path) {
        if (!AUTH) { toast("鉴权未就绪，请刷新页面重试"); return; }
        AUTH.put("/api/settings/workspace", { path: path }).then(function () {
            if (elWsChip) {
                elWsChip.textContent = "工作区：" + path;
                elWsChip.title = path;
            }
            setWsHint(path);
            /* 立刻解锁编码类卡片：用户选完目录就应能点构建/修复，
               不必等下一次 loadStatus 才生效。 */
            state.hasWorkspace = true;
            syncCards();
            toast("工作区已设置为：" + path, "success");
        }).catch(function (err) {
            toast("设置工作区失败：" + (err && err.message ? err.message : "未知错误"));
        });
    }

    var btnWs = $("awBtnWs");
    if (btnWs) btnWs.addEventListener("click", pickWorkspace);

    /* 归档区折叠 / 三点菜单全局交互 */
    var archHead = $("awArchHead");
    if (archHead) {
        archHead.addEventListener("click", function () {
            var wrap = $("awArch");
            if (!wrap) return;
            var collapsed = wrap.classList.toggle("is-collapsed");
            archHead.setAttribute("aria-expanded", collapsed ? "false" : "true");
        });
    }

    /* 点击菜单外部收起（点击 ⋯ / 菜单项内部已 stopPropagation，不会误关） */
    document.addEventListener("click", function (e) {
        if (!menuEl || !menuEl.classList.contains("is-open")) return;
        if (menuEl.contains(e.target)) return;
        closeMenu();
    });
    /* Esc 关闭菜单 */
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && menuEl && menuEl.classList.contains("is-open")) closeMenu();
    });
    /* 滚动 / 缩放时菜单定位会失效，直接收起 */
    window.addEventListener("scroll", closeMenu, true);
    window.addEventListener("resize", closeMenu);

    /* ── 启动 ───────────────────────────────────────────── */
    window.addEventListener("aegis:attachments-changed", syncSend);
    window.addEventListener("aegis:model-selection-changed", syncSend);
    if (AUTH && elThinkSelect) {
        AUTH.get("/api/settings/thinking").then(function (info) { if (info && info.level) elThinkSelect.value = info.level; }).catch(function () {});
        elThinkSelect.addEventListener("change", function () {
            AUTH.put("/api/settings/thinking", {level: elThinkSelect.value}).catch(function () { toast("思考量级保存失败，本轮仍使用当前选择"); });
        });
    }

    if (AUTH && elPermSelect) {
        var permissionChanged = false;
        AUTH.get("/api/settings/permission").then(function (info) {
            if (!permissionChanged && info && ["approve", "full", "plan", "chat"].indexOf(info.mode) >= 0) {
                elPermSelect.value = info.mode;
            }
        }).catch(function () {});
        elPermSelect.addEventListener("change", function () {
            permissionChanged = true;
            AUTH.put("/api/settings/permission", { mode: elPermSelect.value })
                .catch(function () { toast("权限模式保存失败，本轮仍使用当前选择"); });
        });
    }

    if (!AUTH) {
        toast("鉴权脚本未加载，请刷新页面重试");
    } else {
        loadStatus();
        loadSessions();
    }
    var sidebarRefreshing = false;
    function refreshSidebar() {
        if (document.hidden || sidebarRefreshing) return;
        sidebarRefreshing = true;
        loadSessions().then(function () { sidebarRefreshing = false; });
    }
    window.addEventListener("focus", refreshSidebar);
    document.addEventListener("visibilitychange", function () {
        refreshSidebar();
    });
    setInterval(refreshSidebar, 5000);
    syncSend();
    autosize();
    /* 初始化卡片状态：首屏在 loadStatus 返回前，卡片应先呈「无工作区」禁用态，
       避免用户在这几百毫秒内点到注定失败的入口。 */
    syncCards();
})();
