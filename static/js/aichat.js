/**
 * AI聊天对话模块 — 主框架逻辑
 * ChatGPT 风格侧边栏（含搜索、时间戳、置顶分组）
 */

var sessions = [];
var currentSessionId = null;
var searchKeyword = "";

document.addEventListener("DOMContentLoaded", async function() {
    await loadSessions();
    document.getElementById("newChatBtn").addEventListener("click", createSession);

    // 搜索框
    document.getElementById("sessionSearch").addEventListener("input", function(e) {
        searchKeyword = e.target.value.trim().toLowerCase();
        renderSessions();
    });
});

// ── 加载会话列表 ──────────────────────────────────
async function loadSessions() {
    try {
        sessions = await API.get("/api/aichat/sessions");
        if (!sessions || sessions.length === 0) {
            var session = await API.post("/api/aichat/sessions", {});
            sessions = [session];
        }
        renderSessions();
        if (sessions.length > 0 && !currentSessionId) {
            selectSession(sessions[0].session_id);
        } else if (currentSessionId) {
            var exists = sessions.find(function(s) { return s.session_id === currentSessionId; });
            if (!exists && sessions.length > 0) {
                selectSession(sessions[0].session_id);
            } else {
                loadChatFrame(currentSessionId);
            }
        }
    } catch (e) {
        showToast("加载会话列表失败: " + e.message, "error");
    }
}

// ── 渲染会话列表 ──────────────────────────────────
function renderSessions() {
    var list = document.getElementById("sessionList");
    list.innerHTML = "";

    if (!sessions || sessions.length === 0) {
        list.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text-dark-secondary);font-size:13px;">暂无会话</div>';
        return;
    }

    // 按搜索关键词过滤
    var filtered = sessions.filter(function(s) {
        if (!searchKeyword) return true;
        return (s.session_title || "").toLowerCase().indexOf(searchKeyword) !== -1;
    });

    if (filtered.length === 0) {
        list.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text-dark-secondary);font-size:13px;">未找到匹配的会话</div>';
        return;
    }

    // 分组：置顶 / 全部会话
    var pinned = filtered.filter(function(s) { return parseInt(s.is_pinned) === 1; });
    var normal = filtered.filter(function(s) { return parseInt(s.is_pinned) !== 1; });

    if (pinned.length > 0) {
        var label = document.createElement("div");
        label.className = "session-group-label";
        label.textContent = "置顶";
        list.appendChild(label);
        pinned.forEach(function(s) { list.appendChild(createSessionItem(s)); });
    }

    if (normal.length > 0) {
        var label2 = document.createElement("div");
        label2.className = "session-group-label";
        label2.textContent = searchKeyword ? "搜索结果" : "历史会话";
        list.appendChild(label2);
        normal.forEach(function(s) { list.appendChild(createSessionItem(s)); });
    }
}

// ── 创建单个会话项 ────────────────────────────────
function createSessionItem(s) {
    var pinned = parseInt(s.is_pinned) === 1;
    var time = s.last_message_time || s.created_time;

    var item = document.createElement("div");
    item.className = "session-item" + (s.session_id === currentSessionId ? " active" : "");
    item.dataset.id = s.session_id;

    var pinSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';
    var unpinSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14l-1.5-3a4 4 0 0 1-1.5-3V7a2 2 0 0 1 2-2H6a2 2 0 0 1 2 2v4a4 4 0 0 1-1.5 3z"/></svg>';
    var editSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
    var delSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

    item.innerHTML =
        (pinned ? '<span class="session-pin-icon">' + pinSvg + '</span>' : '') +
        '<div class="session-info-wrap">' +
            '<div class="session-title-text">' + escapeHtml(s.session_title) + '</div>' +
            '<div class="session-time-text">' + formatTime(time) + '</div>' +
        '</div>' +
        '<div class="session-actions">' +
            '<button class="session-action-btn" title="' + (pinned ? "取消置顶" : "置顶") + '" data-act="pin">' + (pinned ? pinSvg : unpinSvg) + '</button>' +
            '<button class="session-action-btn" title="重命名" data-act="rename">' + editSvg + '</button>' +
            '<button class="session-action-btn danger" title="删除" data-act="delete">' + delSvg + '</button>' +
        '</div>';

    item.addEventListener("click", function(e) {
        if (e.target.closest(".session-actions")) return;
        selectSession(s.session_id);
    });

    item.querySelectorAll(".session-action-btn").forEach(function(btn) {
        btn.addEventListener("click", function(e) {
            e.stopPropagation();
            var act = btn.dataset.act;
            if (act === "pin") togglePin(s.session_id, pinned);
            else if (act === "rename") renameSession(s);
            else if (act === "delete") deleteSession(s);
        });
    });

    return item;
}

// ── 选中会话 ──────────────────────────────────────
function selectSession(sessionId) {
    currentSessionId = sessionId;
    document.querySelectorAll(".session-item").forEach(function(el) {
        el.classList.toggle("active", parseInt(el.dataset.id) === sessionId);
    });
    loadChatFrame(sessionId);
}

// ── 加载聊天 iframe ───────────────────────────────
function loadChatFrame(sessionId) {
    var iframe = document.getElementById("chatFrame");
    iframe.src = "/aichat/chat?session_id=" + sessionId;
}

// ── 新建会话 ──────────────────────────────────────
async function createSession() {
    try {
        var session = await API.post("/api/aichat/sessions", {});
        currentSessionId = session.session_id;
        // 不手动往 sessions 里 unshift：新会话缺少最后消息时间，
        // 客户端插到首位会与服务端排序不一致，刷新后又跳走。
        // 统一交给 loadSessions 按服务端排序渲染。
        await loadSessions();
        showToast("新会话已创建", "success");
    } catch (e) {
        showToast("创建会话失败: " + e.message, "error");
    }
}

// ── 置顶/取消置顶 ──────────────────────────────────
async function togglePin(sessionId, currentlyPinned) {
    try {
        await API.put("/api/aichat/sessions/" + sessionId, {
            action: "pin",
            is_pinned: !currentlyPinned,
        });
        await loadSessions();
        showToast(currentlyPinned ? "已取消置顶" : "已置顶", "success");
    } catch (e) {
        showToast("操作失败: " + e.message, "error");
    }
}

// ── 重命名会话 ─────────────────────────────────────
async function renameSession(session) {
    var newTitle = await showPrompt("重命名会话", session.session_title, "请输入会话标题");
    if (newTitle === null) return;                  // 用户取消
    if (newTitle === session.session_title) return;  // 内容未变化
    if (!newTitle) {
        showToast("会话标题不能为空", "error");
        return;
    }

    try {
        await API.put("/api/aichat/sessions/" + session.session_id, {
            action: "rename",
            title: newTitle,
        });
        await loadSessions();
        if (session.session_id === currentSessionId) {
            loadChatFrame(currentSessionId);
        }
        showToast("重命名成功", "success");
    } catch (e) {
        showToast("重命名失败: " + e.message, "error");
    }
}

// ── 删除会话 ──────────────────────────────────────
async function deleteSession(session) {
    var ok = await showConfirm(
        '确定删除会话"' + session.session_title + '"吗？关联的所有聊天消息将一并删除。',
        "删除会话"
    );
    if (!ok) return;

    try {
        await API.del("/api/aichat/sessions/" + session.session_id);
        if (session.session_id === currentSessionId) {
            currentSessionId = null;
        }
        await loadSessions();
        showToast("删除成功", "success");
    } catch (e) {
        showToast("删除失败: " + e.message, "error");
    }
}

// ── HTML 转义 ─────────────────────────────────────
function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
}
