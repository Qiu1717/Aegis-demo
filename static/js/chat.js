/**
 * AI聊天窗口逻辑（iframe 内嵌页面）
 * ChatGPT 风格 — 无气泡全宽消息块
 */

var sessionId = null;
var messages = [];
var isSending = false;

// ── 刷新右上角模型角标 ──────────────────────────────
// 服务端会在流式开始时告知本次实际使用的模型，
// 降级到本地模式时角标也会同步变化。
function updateModelTag(label, model) {
    var tag = document.querySelector(".chat-model-tag");
    if (!tag) return;
    if (label && model) {
        tag.textContent = label + " · " + model;
    } else if (label) {
        tag.textContent = label;
    }
}

document.addEventListener("DOMContentLoaded", async function() {
    var params = new URLSearchParams(window.location.search);
    sessionId = params.get("session_id");

    var input = document.getElementById("chatInput");
    var sendBtn = document.getElementById("sendBtn");

    // 缺少 session_id 时直接给出提示并禁用输入，
    // 否则会去请求 /api/aichat/sessions/null/messages 造成无意义报错
    if (!sessionId) {
        document.getElementById("chatMessagesInner").innerHTML =
            '<div class="chat-empty">' +
                '<div class="chat-empty-title">会话不存在</div>' +
                '<div class="chat-empty-subtitle">请从左侧选择一个会话后再开始聊天</div>' +
            '</div>';
        input.disabled = true;
        sendBtn.disabled = true;
        return;
    }

    sendBtn.addEventListener("click", sendMessage);
    input.addEventListener("keydown", function(e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    input.addEventListener("input", autoResize);

    await loadMessages();
    input.focus();
});

// ── 加载历史消息 ──────────────────────────────────
async function loadMessages() {
    try {
        var data = await API.get("/api/aichat/sessions/" + sessionId + "/messages");
        messages = data || [];
        renderMessages();
        updateTitle();
        scrollToBottom();
    } catch (e) {
        showToast("加载消息失败: " + e.message, "error");
    }
}

// ── 更新顶部标题 ──────────────────────────────────
function updateTitle() {
    var titleEl = document.getElementById("chatTitle");
    if (window.parent && window.parent.sessions) {
        var session = window.parent.sessions.find(function(s) { return s.session_id == sessionId; });
        if (session) {
            titleEl.textContent = session.session_title;
            return;
        }
    }
    titleEl.textContent = "AI 对话";
}

// ── 渲染全部消息 ──────────────────────────────────
function renderMessages() {
    var container = document.getElementById("chatMessagesInner");

    if (messages.length === 0) {
        container.innerHTML =
            '<div class="chat-empty">' +
                '<div class="chat-empty-icon">' +
                    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>' +
                '</div>' +
                '<div class="chat-empty-title">开始对话</div>' +
                '<div class="chat-empty-subtitle">在下方输入框中提出你的问题</div>' +
            '</div>';
        return;
    }

    container.innerHTML = "";
    messages.forEach(function(msg) {
        container.appendChild(createMessageElement(msg));
    });
}

// ── 创建消息 DOM（ChatGPT 风格：无气泡）──────────────
function createMessageElement(msg) {
    var isUser = msg.message_role === "user";
    var div = document.createElement("div");
    div.className = "msg" + (isUser ? " user" : "");

    var avatarText = isUser ? "ME" : "AI";

    div.innerHTML =
        '<div class="msg-avatar ' + (isUser ? "user" : "ai") + '">' + avatarText + '</div>' +
        '<div class="msg-body">' +
            '<div class="msg-content">' + escapeHtml(msg.message_content) + '</div>' +
        '</div>';
    return div;
}

// ── 发送消息 ──────────────────────────────────────
async function sendMessage() {
    var input = document.getElementById("chatInput");
    var text = input.value.trim();
    if (!text || isSending) return;

    isSending = true;
    input.value = "";
    autoResize();
    setSendDisabled(true);

    // 添加用户消息
    var userMsg = { message_role: "user", message_content: text };
    messages.push(userMsg);
    var container = document.getElementById("chatMessagesInner");
    if (messages.length === 1) container.innerHTML = "";
    container.appendChild(createMessageElement(userMsg));
    scrollToBottom();

    // AI 回复占位
    var aiDiv = document.createElement("div");
    aiDiv.className = "msg";
    aiDiv.innerHTML =
        '<div class="msg-avatar ai">AI</div>' +
        '<div class="msg-body">' +
            '<div class="msg-content">' +
                '<div class="loading-dots"><span></span><span></span><span></span></div>' +
            '</div>' +
        '</div>';
    container.appendChild(aiDiv);
    scrollToBottom();

    var contentEl = aiDiv.querySelector(".msg-content");

    try {
        var resp = await fetch("/api/aichat/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, message: text }),
        });

        if (!resp.ok) {
            var errData = await resp.json().catch(function() { return {}; });
            throw new Error(errData.msg || ("HTTP " + resp.status));
        }

        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var aiContent = "";
        var isBlocked = false;

        while (true) {
            var result = await reader.read();
            if (result.done) break;
            buffer += decoder.decode(result.value, { stream: true });

            while (buffer.indexOf("\n\n") !== -1) {
                var idx = buffer.indexOf("\n\n");
                var rawEvent = buffer.substring(0, idx);
                buffer = buffer.substring(idx + 2);

                var lines = rawEvent.split("\n");
                for (var i = 0; i < lines.length; i++) {
                    var line = lines[i];
                    if (line.indexOf("data:") !== 0) continue;
                    var dataStr = line.substring(5).trim();
                    if (!dataStr) continue;
                    try {
                        var evt = JSON.parse(dataStr);
                        if (evt.type === "chunk" || evt.type === "notice") {
                            aiContent += evt.content;
                            contentEl.innerHTML = escapeHtml(aiContent) + '<span class="typing-cursor"></span>';
                            scrollToBottom();
                        } else if (evt.type === "provider") {
                            updateModelTag(evt.label, evt.model);
                        } else if (evt.type === "blocked") {
                            isBlocked = true;
                            aiContent = evt.content;
                            aiDiv.classList.add("msg-blocked");
                            contentEl.innerHTML = escapeHtml(aiContent);
                        } else if (evt.type === "error") {
                            aiContent = evt.content;
                            contentEl.innerHTML = escapeHtml(aiContent);
                        }
                    } catch (e) { /* skip */ }
                }
            }
        }

        // 移除光标
        var cursor = contentEl.querySelector(".typing-cursor");
        if (cursor) cursor.remove();

        if (!aiContent) {
            contentEl.innerHTML = '<span style="color:var(--text-light);">（回复为空）</span>';
        }

        messages.push({ message_role: "assistant", message_content: aiContent });

        // 通知父页面刷新会话列表；首次提问时后端会自动生成会话标题，
        // 所以要等列表刷新完再同步本窗口标题，否则顶部会一直显示「新对话」。
        if (window.parent && window.parent.loadSessions) {
            await window.parent.loadSessions();
        }
        updateTitle();

        scrollToBottom();
    } catch (e) {
        contentEl.innerHTML = '<span style="color:var(--danger);">' + escapeHtml("请求失败: " + e.message) + '</span>';
        showToast(e.message, "error");
    } finally {
        isSending = false;
        setSendDisabled(false);
        document.getElementById("chatInput").focus();
    }
}

// ── 辅助函数 ──────────────────────────────────────
function setSendDisabled(disabled) {
    document.getElementById("sendBtn").disabled = disabled;
}

function autoResize() {
    var input = document.getElementById("chatInput");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
}

function scrollToBottom() {
    var container = document.getElementById("chatMessages");
    container.scrollTop = container.scrollHeight;
}

function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
}
