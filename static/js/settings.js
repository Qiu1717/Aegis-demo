/**
 * Aegis — 模型服务商设置弹窗（AEG-K01 / K03 / K04 / K05）
 *
 * 依赖：auth.js（aegisFetch / aegisFetchJSON）、common.js（escapeText / showToast）
 *
 * 对外暴露：
 *   window.AegisSettings = {
 *       open(), close(), refresh(), saveConfig(payload), testKey(...),
 *       fetchBootstrap(), fetchProviders(), applyBanner(), state,
 *       guardState, fetchGuard(), renderGuard(), guardApi(),
 *       setGuardEnabled(), saveRefusal(), addCategory(), deleteCategory(),
 *       updateKeyword(), deleteKeyword(), submitBatchKeywords(),
 *       exportGuard(), importGuardFile(), runGuardCheck(), parseKeywordBatch()
 *   }
 *
 * 板块：
 *   · 服务商配置（st*）；
 *   · 回答限制规则（gd*，见下方 Guard 段落，接口基础路径 /api/guard）。
 *
 * 安全约定：
 *   · 所有动态内容一律经 escapeText / escapeAttrText 后再拼接；
 *   · 外链只放行 http(s)；
 *   · API Key 默认掩码，「显示」需二次点击且 10 秒后自动收回。
 */
(function () {
    "use strict";

    var REVEAL_TIMEOUT_MS = 10000;   // 明文收回时间
    var ARM_TIMEOUT_MS = 4000;       // 「再点一次」的等待窗口

    /** 模块内状态（不持久化任何密钥） */
    var state = {
        loaded: false,
        loading: false,
        providers: [],     // 后端返回的服务商视图（含掩码）
        presets: [],       // 预设（下拉数据源）
        active: "",
        current: "",       // 当前编辑的服务商 key
        draft: {},         // { providerKey: { api_key, base_url, model, timeout, enabled } }
        keystore: null,
        bootstrap: null,
    };

    // ── 小工具 ──────────────────────────────────────────────
    function esc(text) {
        if (typeof window.escapeText === "function") return window.escapeText(text);
        var div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }

    function escAttr(text) {
        if (typeof window.escapeAttrText === "function") return window.escapeAttrText(text);
        return esc(text).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function toast(msg, type) {
        if (typeof window.showToast === "function") {
            window.showToast(msg, type || "info", 3000);
        } else if (window.console && window.console.warn) {
            window.console.warn(msg);
        }
    }

    /** 只放行 http(s) 链接，杜绝 javascript: / data: 等协议 */
    function safeUrl(raw) {
        if (!raw) return "";
        try {
            var url = new URL(String(raw), window.location.origin);
            if (url.protocol === "http:" || url.protocol === "https:") return url.href;
        } catch (e) { /* 非法 URL 一律丢弃 */ }
        return "";
    }

    function request(url, opts) {
        var task;
        if (window.AegisAuth && typeof window.AegisAuth.fetchJSON === "function") {
            task = window.AegisAuth.fetchJSON(url, opts);
        } else if (typeof window.aegisFetch === "function") {
            task = window.aegisFetch(url, opts).then(function (r) { return r.json(); });
        } else {
            task = fetch(url, opts).then(function (r) { return r.json(); });
        }
        return task.then(function (data) {
            if (data && Object.prototype.hasOwnProperty.call(data, "data")
                    && Object.prototype.hasOwnProperty.call(data, "code")) {
                if (Number(data.code) !== 0) throw new Error(data.msg || "请求失败");
                return data.data;
            }
            return data;
        });
    }

    function $(id) { return document.getElementById(id); }

    function findProvider(key) {
        for (var i = 0; i < state.providers.length; i++) {
            if (state.providers[i].key === key) return state.providers[i];
        }
        return null;
    }

    function findPreset(key) {
        for (var i = 0; i < state.presets.length; i++) {
            if (state.presets[i].key === key) return state.presets[i];
        }
        return null;
    }

    function draftOf(key) {
        if (!state.draft[key]) state.draft[key] = {};
        return state.draft[key];
    }

    // ── 数据加载 ────────────────────────────────────────────
    function fetchBootstrap() {
        return request("/api/settings/bootstrap", { method: "GET" })
            .then(function (data) {
                state.bootstrap = data || {};
                state.presets = (data && data.presets) || state.presets;
                return state.bootstrap;
            })
            .catch(function (err) {
                toast("读取启动引导信息失败：" + (err && err.message ? err.message : "请查看服务日志"), "error");
                return null;
            });
    }

    function fetchProviders() {
        state.loading = true;
        return request("/api/settings/providers", { method: "GET" })
            .then(function (data) {
                data = data || {};
                state.providers = data.providers || [];
                state.presets = data.presets || state.presets;
                state.active = data.active || "";
                state.keystore = data.keystore || null;
                if ($("stTemp")) $("stTemp").value = data.temperature == null ? 0.7 : data.temperature;
                if ($("stMaxTokens")) $("stMaxTokens").value = data.max_tokens == null ? 4096 : data.max_tokens;
                state.loaded = true;
                if (!state.current && state.providers.length) {
                    state.current = (state.active && findProvider(state.active))
                        ? state.active : state.providers[0].key;
                }
                return data;
            })
            .catch(function (err) {
                toast("读取服务商配置失败：" + (err && err.message ? err.message : "请查看服务日志"), "error");
                return null;
            })
            .then(function (data) {
                state.loading = false;
                return data;
            });
    }

    // ── 渲染 ────────────────────────────────────────────────
    function renderProviderSelect() {
        var sel = $("stProviderSel");
        if (!sel) return;
        var html = "";
        for (var i = 0; i < state.providers.length; i++) {
            var p = state.providers[i];
            var star = p.recommended ? " · 推荐" : "";
            var mark = p.has_key ? " · 已配置" : "";
            html += '<option value="' + escAttr(p.key) + '"'
                + (p.key === state.current ? " selected" : "") + '>'
                + esc(p.label + star + mark) + "</option>";
        }
        sel.innerHTML = html;
    }

    function renderModels() {
        var list = $("stModelList");
        var p = findProvider(state.current);
        var preset = findPreset(state.current);
        if (list) {
            var models = (p && p.candidate_models) || (preset && preset.candidate_models) || [];
            var html = "";
            for (var i = 0; i < models.length; i++) {
                html += '<option value="' + escAttr(models[i]) + '"></option>';
            }
            list.innerHTML = html;
        }
        var input = $("stModel");
        if (input) {
            input.value = (draftOf(state.current).model)
                || (p && p.model) || (preset && preset.default_model) || "";
        }
    }

    function renderProvider() {
        var p = findProvider(state.current);
        var preset = findPreset(state.current) || {};
        var draft = draftOf(state.current);

        var baseUrl = $("stBaseUrl");
        if (baseUrl) {
            baseUrl.value = draft.base_url || (p && p.base_url) || preset.base_url || "";
        }

        var keyInput = $("stApiKey");
        if (keyInput) {
            keyInput.value = draft.api_key || "";
            keyInput.type = "password";
            keyInput.placeholder = (p && p.has_key && p.masked_key)
                ? "已保存 " + p.masked_key + "，留空表示不修改"
                : "留空表示不修改已保存的密钥";
        }

        var maskHint = $("stMaskHint");
        if (maskHint) {
            maskHint.textContent = (p && p.has_key && p.masked_key)
                ? "已保存：" + p.masked_key + (p.from_env ? "（来自环境变量）" : "")
                : "已保存：无";
        }

        var toggleBtn = $("stToggleKey");
        if (toggleBtn) toggleBtn.textContent = "显示";

        var keyUrl = $("stKeyUrl");
        if (keyUrl) {
            var href = safeUrl((p && p.key_url) || preset.key_url);
            if (href) {
                keyUrl.href = href;
                keyUrl.hidden = false;
            } else {
                keyUrl.removeAttribute("href");
                keyUrl.hidden = true;
            }
        }

        var note = $("stFreeNote");
        if (note) {
            var text = (p && p.free_note) || preset.free_note || "";
            note.textContent = text;
            note.hidden = !text;
        }

        var timeoutInput = $("stTimeout");
        if (timeoutInput) {
            timeoutInput.value = draft.timeout || (p && p.timeout) || 60;
        }

        /* 上下文窗口：0 = 未配置（按模型自动推断）。
           回填时把「当前实际生效值」一并提示给用户，避免他误以为填 0 就
           表示无限制。 */
        var ctxInput = $("stContextLen");
        var ctxHint = $("stContextHint");
        if (ctxInput) {
            var configured = (draft.context_length != null) ? draft.context_length
                            : (p && p.context_length) || 0;
            ctxInput.value = configured > 0 ? configured : 0;
        }
        if (ctxHint) {
            var eff = (p && p.context_length_effective) || 0;
            var shown = (p && p.context_length) || 0;
            ctxHint.textContent = shown > 0
                ? "已设置总窗口 " + shown + " token；扣除输出、系统与工具开销后用于历史。设置不能扩展服务商能力。"
                : (eff > 0
                    ? "留 0 = 安全默认预算；模型窗口估计约 " + eff + " token。设置不能扩展服务商能力。"
                    : "留 0 = 按模型自动推断；无法识别时按 32768 保守处理。");
        }

        /* 删除按钮：内置预设也允许删（用户通常都是自带 Key 接自己的中转），
           只有 offline 兜底档不给删。此前对非 custom 直接隐藏按钮，
           用户根本看不到「删除」这个选项。 */
        var custDel = $("stCustDelBtn");
        if (custDel) {
            custDel.hidden = !(p && p.key !== "offline");
            custDel.textContent = (p && p.custom) ? "删除该厂商" : "删除该预设";
        }

        renderModels();
        renderKeystoreNote();
        clearStatus();
    }

    function renderKeystoreNote() {
        var el = $("stKeyStoreNote");
        if (!el || !state.keystore) return;
        var ks = state.keystore;
        var text = "密钥存储：" + (ks.backend_label || ks.backend || "未知");
        if (ks.protection_low) {
            text += " · 保护级别较低（混淆级），建议改用系统凭据管理器";
        } else {
            text += " · 保护级别高";
        }
        el.textContent = text;
        el.classList.toggle("aegis-warn", !!ks.protection_low);
    }

    // ── 状态与错误展示 ──────────────────────────────────────
    function setStatus(kind, text) {
        var dot = $("stStatusDot");
        var label = $("stStatusText");
        if (dot) {
            dot.className = "aegis-dot aegis-dot--"
                + (kind === "ok" ? "ok" : (kind === "fail" ? "fail" : (kind === "busy" ? "busy" : "idle")));
        }
        if (label) label.textContent = text || "";
    }

    function clearStatus() {
        setStatus("idle", "尚未测试");
        var box = $("stError");
        if (box) box.hidden = true;
    }

    function showError(info) {
        var box = $("stError");
        if (!box) return;
        var reason = (info && info.reason) || "连接失败";
        var action = (info && info.action) || "";
        $("stErrorReason").textContent = reason;
        $("stErrorAction").textContent = action ? "下一步：" + action : "";
        box.hidden = false;
        setStatus("fail", reason);
    }

    // ── 密钥显示（二次点击 + 10 秒自动收回）─────────────────
    var revealArmed = false;
    var armTimer = null;
    var revealTimer = null;

    function hideKey() {
        var input = $("stApiKey");
        var btn = $("stToggleKey");
        if (revealTimer) { clearTimeout(revealTimer); revealTimer = null; }
        if (armTimer) { clearTimeout(armTimer); armTimer = null; }
        revealArmed = false;
        if (input) input.type = "password";
        if (btn) btn.textContent = "显示";
    }

    function onToggleKey() {
        var input = $("stApiKey");
        var btn = $("stToggleKey");
        if (!input) return;

        if (input.type === "text") { hideKey(); return; }

        if (!input.value) {
            toast("输入框为空；出于安全考虑，服务端只返回掩码，请重新粘贴后再查看", "info");
            return;
        }
        if (!revealArmed) {
            revealArmed = true;
            if (btn) btn.textContent = "再点一次";
            if (armTimer) clearTimeout(armTimer);
            armTimer = setTimeout(function () {
                revealArmed = false;
                if (btn) btn.textContent = "显示";
            }, ARM_TIMEOUT_MS);
            return;
        }
        revealArmed = false;
        if (armTimer) { clearTimeout(armTimer); armTimer = null; }
        input.type = "text";
        if (btn) btn.textContent = "隐藏";
        if (revealTimer) clearTimeout(revealTimer);
        revealTimer = setTimeout(hideKey, REVEAL_TIMEOUT_MS);
    }

    // ── 测试连接 ────────────────────────────────────────────
    function testConnection(silentOnSuccess) {
        var key = state.current;
        if (!key) { toast("请先选择服务商", "error"); return Promise.resolve(null); }

        var baseUrl = $("stBaseUrl") ? $("stBaseUrl").value.trim() : "";
        var model = $("stModel") ? $("stModel").value.trim() : "";
        var apiKey = $("stApiKey") ? $("stApiKey").value.trim() : "";
        var timeout = parseInt(($("stTimeout") || {}).value, 10) || 60;

        setStatus("busy", "正在测试连接…");
        var box = $("stError");
        if (box) box.hidden = true;

        return request("/api/settings/test", {
            method: "POST",
            body: JSON.stringify({
                provider_key: key,
                api_key: apiKey,
                base_url: baseUrl,
                model: model,
                timeout: timeout,
            }),
        }).then(function (data) {
            data = data || {};
            if (data.ok) {
                var ms = data.latency_ms || 0;
                setStatus("ok", "已连接（延迟 " + ms + "ms）");
                if (!silentOnSuccess) toast("连接成功（延迟 " + ms + "ms）", "success");
            } else {
                showError(data.error || {});
            }
            return data;
        }).catch(function (err) {
            showError({
                reason: "测试连接失败：" + (err && err.message ? err.message : "请查看服务日志"),
                action: "检查网络或代理设置后重试",
                code: "NETWORK",
            });
            return null;
        });
    }

    // ── 保存 ────────────────────────────────────────────────
    function collectPayload() {
        // 把当前编辑框里的值固化到 draft，避免切换服务商时丢失
        var draft = draftOf(state.current);
        var baseUrl = $("stBaseUrl");
        var model = $("stModel");
        var apiKey = $("stApiKey");
        var timeout = $("stTimeout");
        if (baseUrl) draft.base_url = baseUrl.value.trim();
        if (model) draft.model = model.value.trim();
        if (apiKey) draft.api_key = apiKey.value.trim();
        if (timeout) draft.timeout = parseInt(timeout.value, 10) || 60;
        var ctxLen = $("stContextLen");
        if (ctxLen) {
            var n = parseInt(ctxLen.value, 10);
            // 显式下发 0，才能重置之前保存的窗口。
            draft.context_length = (n > 0) ? n : 0;
        }

        var providers = [];
        for (var i = 0; i < state.providers.length; i++) {
            var p = state.providers[i];
            var d = state.draft[p.key] || {};
            var item = {
                key: p.key,
                enabled: (d.enabled === undefined) ? !!p.enabled : !!d.enabled,
                base_url: d.base_url || p.base_url || "",
                model: d.model || p.model || "",
                timeout: d.timeout || p.timeout || 60,
                label: p.custom ? (p.label || p.key) : undefined,
            };
            if (d.context_length != null) item.context_length = d.context_length;
            // 留空复用服务端已保存的密钥。
            if (d.api_key) item.api_key = d.api_key;
            providers.push(item);
        }
        var payload = { active: state.current, providers: providers };
        if ($("stTemp")) payload.temperature = Number($("stTemp").value);
        if ($("stMaxTokens")) payload.max_tokens = Number($("stMaxTokens").value);
        return payload;
    }

    function checkSaved(data) {
        if (data.ok === false) throw new Error((data.error && data.error.reason) || "保存失败");
        if (data.key_failed && data.key_failed.length) throw new Error("密钥保存失败，请检查本机密钥存储后重试");
        if (data.invalid_providers && data.invalid_providers.length) {
            throw new Error(data.invalid_providers[0].reason || "厂商配置无效");
        }
    }

    function save(silent) {
        var payload = collectPayload();
        return request("/api/settings/providers", {
            method: "PUT",
            body: JSON.stringify(payload),
        }).then(function (data) {
            data = data || {};
            checkSaved(data);
            state.providers = data.providers || state.providers;
            state.active = data.active || state.active;
            state.keystore = data.keystore || state.keystore;
            // 保存成功后清空明文草稿，避免密钥长期留在内存/页面上
            state.draft = {};
            renderProviderSelect();
            renderProvider();
            if (!silent) toast("配置已保存并立即生效", "success");
            var bad = data.invalid_providers || [];
            for (var i = 0; i < bad.length; i++) {
                toast((bad[i].key || "厂商") + " 未保存：" + (bad[i].reason || "配置无效"), "error");
            }
            // 通知其他页面组件（如工作台 modelTag）刷新模型状态
            try {
                window.dispatchEvent(new CustomEvent("aegis:settings-changed"));
            } catch (e) { /* 老浏览器忽略 */ }
            return data;
        }).catch(function (err) {
            /* 失败时**不吞掉错误**、也不静默返回 null。
               此前这里 return null，配合调用方的 `if (data) closeModal()`
               会造成「点了保存但窗口纹丝不动」的观感 —— 用户既看不到
               明确原因，也不知该重试还是放弃。改为：抛出带可读消息的
               错误，由调用方决定提示与是否关闭。 */
            var msg = (err && err.message) ? err.message
                : (err && err.msg) ? err.msg
                : "请查看服务日志";
            var e2 = new Error(msg);
            e2.detail = err;
            e2.silent = !!silent;   // 向导等静默调用方不必弹 toast
            throw e2;
        });
    }

    /** 供向导调用：直接提交一份完整 payload */
    function saveConfig(payload) {
        return request("/api/settings/providers", {
            method: "PUT",
            body: JSON.stringify(payload || {}),
        }).then(function (data) {
            checkSaved(data || {});
            state.providers = (data && data.providers) || state.providers;
            state.active = (data && data.active) || state.active;
            return data;
        });
    }

    // ── 拉取模型清单（OpenAI 兼容端点 GET {base_url}/models）──────────
    /**
     * 支持两种填法：
     *   1) 手动填写模型名（始终可用）
     *   2) 点「拉取模型」自动填入下拉候选，选中即写入输入框
     * 端点不支持 /models 时不报错，而是回退到提示 + 保留手填能力。
     */
    function fetchModels() {
        var url = (($("stCustUrl") || {}).value || "").trim();
        var hint = $("stFetchModelsHint");
        var btn = $("stFetchModelsBtn");
        var list = $("stCustModelList");

        function say(text, kind) {
            if (!hint) return;
            hint.textContent = text || "";
            hint.style.display = text ? "block" : "none";
            hint.classList.toggle("aegis-warn", kind === "warn");
        }

        if (!url || !/^https?:\/\//i.test(url)) {
            toast("请先填写 API 地址（需以 http:// 或 https:// 开头）", "error");
            say("需要先填写 API 地址", "warn");
            return;
        }

        if (btn) { btn.disabled = true; btn.textContent = "拉取中…"; }
        say("");

        var apiKey = (($("stCustApiKey") || {}).value || "").trim();
        return request("/api/settings/models", {
            method: "POST",
            body: JSON.stringify({
                provider_key: "custom",
                base_url: url,
                api_key: apiKey || undefined
            }),
        }).then(function (res) {
            var data = res || {};
            var models = data.models || [];
            if (list) {
                list.textContent = "";
                for (var i = 0; i < models.length; i++) {
                    var opt = document.createElement("option");
                    opt.value = models[i];
                    list.appendChild(opt);
                }
            }
            if (data.ok) {
                var src = data.source === "preset" ? "（来自内置候选）" : "";
                say("已获取 " + models.length + " 个模型" + src + "，点击输入框下拉选择", "ok");
                toast("已获取 " + models.length + " 个模型", "success");
            } else {
                var reason = (data.error && data.error.reason) || "拉取失败";
                var action = (data.error && data.error.action) || "";
                say(reason + (action ? "。" + action : ""), "warn");
                if (models.length) {
                    say(reason + "，已回退到内置候选 " + models.length + " 个", "warn");
                }
                toast(reason, "error");
            }
            return data;
        }).catch(function (err) {
            say("拉取失败：" + ((err && err.message) || "请查看服务日志"), "warn");
            toast("拉取失败：" + ((err && err.message) || "请查看服务日志"), "error");
        }).then(function (data) {
            if (btn) { btn.disabled = false; btn.textContent = "拉取模型"; }
            return data;
        });
    }

    /** 对当前选中的厂商拉取模型清单，填进 stModelList 下拉 */
    function fetchModelsFor(providerKey) {
        var key = providerKey || state.current || "custom";
        var btn = $("stFetchModelsBtn2");
        var baseUrl = (($("stBaseUrl") || {}).value || "").trim();
        var apiKey = (($("stApiKey") || {}).value || "").trim();
        var list = $("stModelList");

        function say(text, warn) {
            var h = $("stContextHint");
            if (!h) return;
            if (text) h.textContent = text;
            h.classList.toggle("aegis-warn", !!warn);
        }

        if (btn) { btn.disabled = true; btn.textContent = "拉取中…"; }
        return request("/api/settings/models", {
            method: "POST",
            body: JSON.stringify({
                provider_key: key,
                base_url: baseUrl || undefined,
                api_key: apiKey || undefined
            }),
        }).then(function (res) {
            var data = res || {};
            var models = data.models || [];
            if (list) {
                list.textContent = "";
                for (var i = 0; i < models.length; i++) {
                    var opt = document.createElement("option");
                    opt.value = models[i];
                    list.appendChild(opt);
                }
            }
            if (data.ok) {
                say("已获取 " + models.length + " 个模型，点击「模型」输入框下拉选择。", false);
                toast("已获取 " + models.length + " 个模型", "success");
            } else {
                var reason = (data.error && data.error.reason) || "拉取失败";
                var action = (data.error && data.error.action) || "";
                say(reason + (action ? "。" + action : "")
                    + (models.length ? "（内置候选仅供参考 " + models.length + " 个）" : ""), true);
                toast(reason, "error");
            }
            return data;
        }).catch(function (err) {
            say("拉取失败：" + ((err && err.message) || "请查看服务日志"), true);
            toast("拉取失败：" + ((err && err.message) || "请查看服务日志"), "error");
        }).then(function (d) {
            if (btn) { btn.disabled = false; btn.textContent = "拉取模型"; }
            return d;
        });
    }

    // ── 自定义厂商（任意 OpenAI 兼容端点）───────────────────
    function addCustomProvider() {
        var label = (($("stCustLabel") || {}).value || "").trim();
        var keyRaw = (($("stCustKey") || {}).value || "").trim();
        var url = (($("stCustUrl") || {}).value || "").trim();
        var model = (($("stCustModel") || {}).value || "").trim();
        var apiKey = (($("stCustApiKey") || {}).value || "").trim();
        if (!label) { toast("请填写显示名称", "error"); return; }
        if (!url || !/^https?:\/\//i.test(url)) {
            toast("API 地址必须以 http(s):// 开头", "error");
            return;
        }
        if (!model) { toast("请填写默认模型名", "error"); return; }
        var key = keyRaw || ("custom_" + Date.now().toString(36));
        if (!/^[a-z][a-z0-9_]{1,31}$/.test(key)) {
            toast("标识只能用小写字母开头的字母数字下划线（2-32 位）", "error");
            return;
        }
        var entry = { enabled: true, base_url: url, model: model, label: label };
        if (apiKey) entry.api_key = apiKey;
        var payload = { active: key, providers: {} };
        payload.providers[key] = entry;
        request("/api/settings/providers", {
            method: "PUT",
            body: JSON.stringify(payload),
        }).then(function (data) {
            data = data || {};
            checkSaved(data);
            if (data.invalid_providers && data.invalid_providers.length) {
                toast("添加失败：" + (data.invalid_providers[0].reason || "配置无效"), "error");
                return null;
            }
            state.providers = data.providers || state.providers;
            state.active = data.active || key;
            state.keystore = data.keystore || state.keystore;
            state.draft = {};
            state.current = key;
            renderProviderSelect();
            renderProvider();
            ["stCustLabel", "stCustKey", "stCustUrl", "stCustModel", "stCustApiKey"].forEach(function (id) {
                var el = $(id);
                if (el) el.value = "";
            });
            toast("自定义厂商已添加并选中", "success");
            try { window.dispatchEvent(new CustomEvent("aegis:settings-changed")); } catch (e) { /* 忽略 */ }
            return data;
        }).catch(function (err) {
            toast("添加失败：" + (err && err.message ? err.message : "请查看服务日志"), "error");
            return null;
        });
    }

    function deleteCustomProvider() {
        var key = state.current;
        var p = findProvider(key);
        if (!p) {
            toast("请先选择要删除的厂商", "error");
            return;
        }
        /* offline 是「永不停止服务」的兜底档，不允许删除 */
        if (key === "offline") {
            toast("离线兜底档不可删除", "error");
            return;
        }
        var doDelete = function () {
            request("/api/settings/providers", {
                method: "PUT",
                body: JSON.stringify({ remove: [key] }),
            }).then(function (data) {
                data = data || {};
                state.providers = data.providers || state.providers;
                checkSaved(data);
                state.active = data.active || state.active;
                state.keystore = data.keystore || state.keystore;
                state.presets = data.presets || state.presets;
                state.current = (state.providers[0] && state.providers[0].key) || "offline";
                renderProviderSelect();
                renderProvider();
                toast("已删除厂商「" + (p.label || key) + "」", "success");
                try { window.dispatchEvent(new CustomEvent("aegis:settings-changed")); } catch (e) { /* 忽略 */ }
                return data;
            }).catch(function (err) {
                toast("删除失败：" + (err && err.message ? err.message : "请查看服务日志"), "error");
            });
        };
        var kind = p.custom ? "自定义厂商" : "内置预设";
        var tip = p.custom
            ? "删除后其密钥也会一并删除。"
            : "删除后可在下方「添加厂商」里重新添加，密钥会一并删除。";
        if (typeof window.showConfirm === "function") {
            window.showConfirm("确定删除" + kind + "「" + (p.label || key) + "」？\n" + tip)
                .then(function (yes) { if (yes) doDelete(); });
        } else {
            doDelete();
        }
    }

    // ══ 回答限制规则（Guard）══════════════════════════════════════════
    //
    // 后端契约（apps 侧实现），基础路径 /api/guard：
    //   GET    /rules                 完整配置
    //   PUT    /rules                 局部更新 { enabled?, refusal_message? }
    //   POST   /categories            { id?, label, enabled? }
    //   PUT    /categories/<id>       { label?, enabled? }
    //   DELETE /categories/<id>       内置类别会被后端拒绝
    //   POST   /keywords              单个对象或对象数组（批量）
    //   PUT    /keywords/<id>         局部更新
    //   DELETE /keywords/<id>
    //   GET    /export                导出配置 JSON
    //   POST   /import                导入配置 JSON（全量覆盖）
    //   POST   /check                 { text } → { blocked, reason, category, keyword, mode, priority }
    //
    // 约定：
    //   · 所有请求走 window.AegisAuth.get/post/put/del，它们内部已解包 {code,msg,data}
    //     并返回 data，这里绝不能再取一次 .data（历史上踩过双重解包的坑）；
    //   · 任何一次写操作成功后都重新拉取 /rules 并重渲染，保证前后端状态一致；
    //   · 本模块整体失败不得影响弹窗里其它板块，因此 open() 中单独 try/catch。
    // ═══════════════════════════════════════════════════════════════

    var GUARD_BASE = "/api/guard";
    var DEFAULT_REFUSAL = "该问题涉及受限内容，我无法回答";
    var MODE_EXACT = "exact";
    var MODE_FUZZY = "fuzzy";
    var DEFAULT_PRIORITY = 100;
    var KEYWORD_MAX_LEN = 100;      // 前端上限，小于后端 120，先看本地先报错
    var MAX_BATCH = 500;            // 单次批量上限，与后端保持一致

    /** 回答限制模块状态（与后端配置结构一一对应） */
    var guardState = {
        loaded: false,
        version: 1,
        enabled: true,
        refusal_message: DEFAULT_REFUSAL,
        categories: [],   // [{ id, label, built_in, enabled }]
        keywords: [],     // [{ id, word, mode, priority, enabled, category, alias }]
    };

    /**
     * guard 的统一请求出口：优先 window.AegisAuth 的 get/post/put/del。
     *
     * @param {string} method "GET" | "POST" | "PUT" | "DELETE"
     * @param {string} path   相对 GUARD_BASE 的路径
     * @param {*} [body]      请求体（GET/DELETE 不传）
     * @returns {Promise<*>}  后端 data 字段（已解包）
     */
    function guardApi(method, path, body) {
        var url = GUARD_BASE + path;
        var hasBody = (body !== undefined && body !== null);
        var auth = window.AegisAuth;
        if (auth) {
            if (method === "GET" && typeof auth.get === "function") {
                return auth.get(url);
            }
            if (method === "POST" && typeof auth.post === "function") {
                return auth.post(url, hasBody ? body : undefined);
            }
            if (method === "PUT" && typeof auth.put === "function") {
                return auth.put(url, hasBody ? body : undefined);
            }
            if (method === "DELETE" && typeof auth.del === "function") {
                return auth.del(url);
            }
            if (typeof auth.fetchJSON === "function") {
                var jsonOpts = { method: method };
                if (hasBody) jsonOpts.body = JSON.stringify(body);
                return auth.fetchJSON(url, jsonOpts);
            }
        }
        var opts = { method: method };
        if (hasBody) opts.body = JSON.stringify(body);
        return request(url, opts);
    }

    function errText(err) {
        return (err && err.message) ? err.message : "请查看服务日志";
    }

    /** 二次确认：优先既有 showConfirm，缺失时用 window.confirm 兜底 */
    function confirmThen(message, fn) {
        if (typeof window.showConfirm === "function") {
            window.showConfirm(message, "请确认").then(function (yes) {
                if (yes) fn();
            });
            return;
        }
        var agreed = false;
        try {
            agreed = !!(window.confirm && window.confirm(message));
        } catch (e) {
            agreed = true;   // 无弹窗能力时放行，避免功能彻底不可用
        }
        if (agreed) fn();
    }

    function findCategory(id) {
        for (var i = 0; i < guardState.categories.length; i++) {
            if (guardState.categories[i].id === id) return guardState.categories[i];
        }
        return null;
    }

    function findKeyword(id) {
        for (var i = 0; i < guardState.keywords.length; i++) {
            if (guardState.keywords[i].id === id) return guardState.keywords[i];
        }
        return null;
    }

    function categoryLabel(id) {
        var cat = findCategory(id);
        if (cat) return cat.label || cat.id;
        return id ? id : "未分类";
    }

    /** 类别名 -> id（同时兼容中文显示名匹配，大小写不敏感） */
    function resolveCategoryId(raw) {
        var key = String(raw == null ? "" : raw).trim().toLowerCase();
        if (!key) return "";
        for (var i = 0; i < guardState.categories.length; i++) {
            var c = guardState.categories[i];
            if (String(c.id || "").toLowerCase() === key) return c.id;
        }
        for (var j = 0; j < guardState.categories.length; j++) {
            var d = guardState.categories[j];
            if (String(d.label || "").trim().toLowerCase() === key) return d.id;
        }
        return "";
    }

    function defaultCategoryId() {
        if (guardState.categories.length) {
            return guardState.categories[0].id || guardState.categories[0].label || "";
        }
        return "";
    }

    function modeLabel(mode) {
        return (mode === MODE_EXACT) ? "精确" : "模糊";
    }

    function normalizeMode(raw) {
        var key = String(raw == null ? "" : raw).trim().toLowerCase();
        if (key === MODE_EXACT || key === "exact" || key === "精确" || key === "精准" || key === "e") return MODE_EXACT;
        if (key === "" || key === MODE_FUZZY || key === "fuzzy" || key === "模糊" || key === "包含" || key === "f") return MODE_FUZZY;
        return "";
    }

    // ── Guard 读写 ──────────────────────────────────────────
    function applyGuardConfig(cfg) {
        cfg = cfg || {};
        guardState.version = Number(cfg.version) || guardState.version || 1;
        guardState.enabled = (cfg.enabled === undefined) ? true : !!cfg.enabled;
        guardState.refusal_message = (typeof cfg.refusal_message === "string" && cfg.refusal_message)
            ? cfg.refusal_message : DEFAULT_REFUSAL;
        guardState.categories = Array.isArray(cfg.categories) ? cfg.categories : [];
        guardState.keywords = Array.isArray(cfg.keywords) ? cfg.keywords : [];
        guardState.loaded = true;
        return guardState;
    }

    function fetchGuard() {
        return guardApi("GET", "/rules").then(function (data) {
            return applyGuardConfig(data || {});
        });
    }

    /** 写操作统一模板：成功后重新拉取 + 重渲染，失败 toast */
    function guardWrite(method, path, body, failMsg) {
        return guardApi(method, path, body)
            .then(function (data) {
                return fetchGuard()
                    .then(function () { renderGuard(); return data; })
                    .catch(function () { renderGuard(); return data; });
            })
            .catch(function (err) {
                toast(failMsg + "：" + errText(err), "error");
                return null;
            });
    }

    // ── Guard 渲染 ──────────────────────────────────────────
    function renderGuardSummary() {
        var summary = $("gdSummary");
        if (!summary) return;
        if (!guardState.loaded) {
            summary.textContent = "尚未加载";
            return;
        }
        var enabledCount = 0;
        for (var i = 0; i < guardState.keywords.length; i++) {
            if (guardState.keywords[i].enabled !== false) enabledCount++;
        }
        summary.textContent = (guardState.enabled ? "已启用" : "已停用")
            + " · 类别 " + guardState.categories.length
            + " · 关键词 " + guardState.keywords.length + "（生效 " + enabledCount + "）";
    }

    function renderGuardRefusal() {
        var input = $("gdRefusal");
        if (!input) return;
        // 正在输入时不要被服务端回写的内容覆盖
        if (document.activeElement === input) return;
        input.value = guardState.refusal_message || "";
        input.placeholder = DEFAULT_REFUSAL;
    }

    function renderGuardCategories() {
        var box = $("gdCatList");
        if (!box) return;
        if (!guardState.categories.length) {
            box.innerHTML = '<div class="gd-empty">暂无类别</div>';
            return;
        }
        var html = "";
        for (var i = 0; i < guardState.categories.length; i++) {
            var c = guardState.categories[i];
            var id = String(c.id == null ? "" : c.id);
            var label = String(c.label == null ? id : c.label);
            var builtIn = !!c.built_in;
            var checked = (c.enabled === undefined) ? true : !!c.enabled;
            html += '<div class="gd-cat" data-id="' + escAttr(id) + '">'
                + '<label class="gd-inline">'
                + '<input class="gd-checkbox" type="checkbox" data-field="enabled" data-id="' + escAttr(id) + '"'
                + (checked ? " checked" : "") + '>'
                + '<span class="gd-cat__label">' + esc(label) + "</span>"
                + "</label>"
                + '<span class="gd-cat__id">' + esc(id) + "</span>"
                + (builtIn ? '<span class="gd-tag">内置</span>' : '<span class="gd-tag gd-tag--soft">自定义</span>')
                + '<span class="gd-cat__ops">'
                + '<button type="button" class="aegis-icon-btn aegis-btn-xs" data-act="rename" data-id="' + escAttr(id) + '"'
                + ' aria-label="重命名类别" title="重命名">' + "✎" + "</button>"
                + '<button type="button" class="aegis-icon-btn aegis-btn-xs" data-act="delete" data-id="' + escAttr(id) + '"'
                + (builtIn ? " disabled" : "")
                + ' aria-label="删除类别" title="' + (builtIn ? "内置类别不支持删除" : "删除类别") + '">' + "✕" + "</button>"
                + "</span>"
                + "</div>";
        }
        box.innerHTML = html;
    }

    function buildCategoryOptions(selectedId) {
        var html = '<option value=""' + (selectedId ? "" : " selected") + '>未分类</option>';
        for (var i = 0; i < guardState.categories.length; i++) {
            var c = guardState.categories[i];
            var id = String(c.id == null ? "" : c.id);
            html += '<option value="' + escAttr(id) + '"'
                + (id === selectedId ? " selected" : "") + ">"
                + esc(String(c.label == null ? id : c.label)) + "</option>";
        }
        // 后端若存了已删除的类别，也要把原值展示出来，避免静默改数据
        if (selectedId && resolveCategoryId(selectedId) === "") {
            html += '<option value="' + escAttr(selectedId) + '" selected>'
                + esc(selectedId) + "（未知类别）</option>";
        }
        return html;
    }

    function buildModeOptions(mode) {
        var exactSel = (mode === MODE_EXACT) ? " selected" : "";
        var fuzzySel = (mode === MODE_EXACT) ? "" : " selected";
        return '<option value="' + MODE_EXACT + '"' + exactSel + ">精确</option>"
            + '<option value="' + MODE_FUZZY + '"' + fuzzySel + ">模糊</option>";
    }

    function renderGuardKeywords() {
        var box = $("gdKwList");
        if (!box) return;
        if (!guardState.keywords.length) {
            box.innerHTML = '<div class="gd-empty">暂无关键词，可在下方文本框里批量添加</div>';
            return;
        }
        var html = "";
        for (var i = 0; i < guardState.keywords.length; i++) {
            var k = guardState.keywords[i];
            var id = String(k.id == null ? "" : k.id);
            var word = String(k.word == null ? "" : k.word);
            var mode = (k.mode === MODE_EXACT) ? MODE_EXACT : MODE_FUZZY;
            var priority = (k.priority === undefined || k.priority === null)
                ? DEFAULT_PRIORITY : Number(k.priority);
            if (isNaN(priority)) priority = DEFAULT_PRIORITY;
            var enabled = (k.enabled === undefined) ? true : !!k.enabled;
            html += '<div class="gd-kw' + (enabled ? "" : " is-off") + '" data-id="' + escAttr(id) + '">'
                + '<span class="gd-kw__word" title="' + escAttr(word) + '">' + esc(word) + "</span>"
                + '<select class="gd-select" data-field="mode" data-id="' + escAttr(id) + '" aria-label="匹配方式">'
                + buildModeOptions(mode) + "</select>"
                + '<input class="gd-input gd-input--num" type="number" step="1" data-field="priority"'
                + ' data-id="' + escAttr(id) + '" value="' + priority + '" aria-label="优先级">'
                + '<select class="gd-select" data-field="category" data-id="' + escAttr(id) + '" aria-label="所属类别">'
                + buildCategoryOptions(String(k.category == null ? "" : k.category)) + "</select>"
                + '<input class="gd-checkbox" type="checkbox" data-field="enabled" data-id="' + escAttr(id) + '"'
                + (enabled ? " checked" : "") + ' aria-label="启用">'
                + '<button type="button" class="aegis-icon-btn aegis-btn-xs" data-act="del-kw"'
                + ' data-id="' + escAttr(id) + '" aria-label="删除关键词" title="删除关键词">' + "✕" + "</button>"
                + "</div>";
        }
        box.innerHTML = html;
    }

    function renderGuard() {
        var enabledBox = $("gdEnabled");
        if (enabledBox) enabledBox.checked = !!guardState.enabled;
        renderGuardSummary();
        renderGuardRefusal();
        renderGuardCategories();
        renderGuardKeywords();
    }

    // ── Guard 操作 ──────────────────────────────────────────
    function setGuardEnabled(next) {
        return guardWrite("PUT", "/rules", { enabled: !!next }, "保存总开关失败").then(function (data) {
            if (data) toast(next ? "已启用回答限制" : "已停用回答限制", "success");
            return data;
        });
    }

    function saveRefusal(value) {
        var input = $("gdRefusal");
        var text = String(value == null ? "" : value).trim();
        if (!text) {
            toast("拒绝话术不能为空，已还原为原来的内容", "error");
            if (input) input.value = guardState.refusal_message || DEFAULT_REFUSAL;
            return Promise.resolve(null);
        }
        if (text === guardState.refusal_message) return Promise.resolve(guardState);
        return guardWrite("PUT", "/rules", { refusal_message: text }, "保存拒绝话术失败")
            .then(function (data) {
                if (data) toast("拒绝话术已保存", "success");
                return data;
            });
    }

    function resetRefusal() {
        var input = $("gdRefusal");
        if (input) input.value = DEFAULT_REFUSAL;
        return saveRefusal(DEFAULT_REFUSAL);
    }

    function addCategory() {
        var input = $("gdCatLabel");
        var label = String((input && input.value) || "").trim();
        if (!label) {
            toast("请先填写类别名称", "error");
            return Promise.resolve(null);
        }
        return guardWrite("POST", "/categories", { label: label, enabled: true }, "添加类别失败")
            .then(function (data) {
                if (!data) return null;
                if (input) input.value = "";
                toast("类别「" + label + "」已添加", "success");
                return data;
            });
    }

    function renameCategory(id) {
        var cat = findCategory(id);
        if (!cat) return;
        if (typeof window.showPrompt === "function") {
            window.showPrompt("重命名类别", String(cat.label == null ? id : cat.label), "新的类别名称")
                .then(function (value) {
                    if (value === null) return;
                    commitRename(id, value);
                });
            return;
        }
        var typed = window.prompt ? window.prompt("重命名类别为：", String(cat.label == null ? id : cat.label)) : null;
        if (typed === null) return;
        commitRename(id, typed);
    }

    function commitRename(id, label) {
        var text = String(label == null ? "" : label).trim();
        if (!text) {
            toast("类别名称不能为空", "error");
            return;
        }
        guardWrite("PUT", "/categories/" + encodeURIComponent(id), { label: text }, "重命名失败")
            .then(function (data) {
                if (data) toast("类别已重命名", "success");
            });
    }

    function toggleCategory(id, enabled) {
        guardWrite("PUT", "/categories/" + encodeURIComponent(id), { enabled: !!enabled }, "更新类别失败")
            .then(function (data) {
                if (data) toast(enabled ? "类别已启用" : "类别已停用", "success");
            });
    }

    function deleteCategory(id) {
        var cat = findCategory(id);
        if (!cat) return;
        if (cat.built_in) {
            toast("内置类别「" + (cat.label || id) + "」不支持删除，可以取消勾选来停用它", "error");
            return;
        }
        var usedCount = 0;
        for (var i = 0; i < guardState.keywords.length; i++) {
            if (guardState.keywords[i].category === id) usedCount++;
        }
        var message = "删除类别「" + (cat.label || id) + "」？";
        if (usedCount > 0) message += "该类别下还有 " + usedCount + " 条关键词，删除后它们会变成未分类。";
        confirmThen(message, function () {
            guardWrite("DELETE", "/categories/" + encodeURIComponent(id), undefined, "删除类别失败")
                .then(function (data) {
                    if (data) toast("类别已删除", "success");
                });
        });
    }

    /**
     * 解析批量关键词文本。
     * 一行一个关键词，支持全角逗号，格式：词语,类别,模式,优先级,别名1|别名2
     * 缺字段用默认值；解析失败的行会被收集到 errors 里并返回给调用方提示，
     * 绝不静默丢弃。
     *
     * @param {string} text
     * @returns {{items: Array<Object>, errors: Array<string>, notes: Array<string>}}
     */
    function parseKeywordBatch(text) {
        var items = [];
        var errors = [];
        var notes = [];
        var lines = String(text == null ? "" : text).split(/\r?\n/);

        for (var i = 0; i < lines.length; i++) {
            var no = i + 1;
            var raw = lines[i];
            if (!String(raw).trim()) continue;

            // 容错：全角逗号、顿号、分号、制表符都当作字段分隔符
            var parts = String(raw).replace(/[，、；;]/g, ",").replace(/\t/g, ",").split(",");
            var buf = [];
            for (var p = 0; p < parts.length; p++) buf.push(String(parts[p] == null ? "" : parts[p]).trim());

            var word = buf.shift() || "";
            if (!word) {
                errors.push("第 " + no + " 行：缺少关键词");
                continue;
            }
            if (word.length > KEYWORD_MAX_LEN) {
                errors.push("第 " + no + " 行：关键词超过 " + KEYWORD_MAX_LEN + " 字");
                continue;
            }

            var catRaw = buf.shift() || "";
            var modeRaw = buf.shift() || "";
            var prioRaw = buf.shift() || "";
            var aliasRaw = buf.shift() || "";

            var mode = normalizeMode(modeRaw);
            if (modeRaw && !mode) {
                errors.push("第 " + no + " 行：匹配方式「" + modeRaw + "」无法识别，请填 精确 或 模糊");
                continue;
            }

            var priority = DEFAULT_PRIORITY;
            if (prioRaw) {
                var parsed = parseInt(prioRaw, 10);
                if (isNaN(parsed)) {
                    notes.push("第 " + no + " 行：优先级「" + prioRaw + "」不是数字，已按默认 " + DEFAULT_PRIORITY + " 处理");
                } else {
                    priority = parsed;
                }
            }

            var category = "";
            if (catRaw) {
                var resolved = resolveCategoryId(catRaw);
                if (resolved) {
                    category = resolved;
                } else {
                    category = catRaw;
                    notes.push("第 " + no + " 行：类别「" + catRaw + "」暂不存在，已按原样保存");
                }
            } else {
                category = defaultCategoryId();
            }

            var alias = [];
            if (aliasRaw) {
                var pieces = aliasRaw.split(/[|｜/]/);
                for (var a = 0; a < pieces.length; a++) {
                    var one = String(pieces[a] || "").trim();
                    if (one) alias.push(one);
                }
            }

            items.push({
                word: word,
                mode: mode || MODE_FUZZY,
                priority: priority,
                enabled: true,
                category: category,
                alias: alias,
            });
        }
        return { items: items, errors: errors, notes: notes };
    }

    function submitBatchKeywords() {
        var box = $("gdBatch");
        var text = String((box && box.value) || "");
        if (!text.trim()) {
            toast("请先输入要添加的关键词，一行一个", "error");
            return Promise.resolve(null);
        }
        var result = parseKeywordBatch(text);
        if (result.errors.length) {
            toast("以下行无法解析，已跳过：" + result.errors.join("；"), "error");
        }
        if (!result.items.length) {
            if (!result.errors.length) toast("没有解析到有效关键词", "error");
            return Promise.resolve(null);
        }
        if (result.items.length > MAX_BATCH) {
            toast("一次最多添加 " + MAX_BATCH + " 条关键词，当前解析到 "
                + result.items.length + " 条，请分批粘贴", "error");
            return Promise.resolve(null);
        }
        // 契约：body 可以是对象数组，一次请求完成批量新增
        return guardWrite("POST", "/keywords", result.items, "批量添加失败")
            .then(function (data) {
                if (!data) return null;
                if (box) box.value = "";
                // 后端可能按自己的校验规则跳过个别条目（返回 {added, skipped}），
                // 因此成功条数以服务端返回的份数为准，取不到时退回本地份数。
                var addedCount = Array.isArray(data) ? data.length
                    : (data && Array.isArray(data.added)) ? data.added.length
                        : result.items.length;
                toast("已添加 " + addedCount + " 条关键词", "success");
                // 服务端跳过的条目必须显式告知，不能静默吞掉
                var skipped = (data && Array.isArray(data.skipped)) ? data.skipped : [];
                if (skipped.length) {
                    var reasons = [];
                    for (var s = 0; s < skipped.length && s < 5; s++) {
                        reasons.push("第 " + (Number(skipped[s].index) + 1) + " 条："
                            + (skipped[s].reason || "未通过校验"));
                    }
                    if (skipped.length > 5) reasons.push("……共 " + skipped.length + " 条未通过");
                    toast("有 " + skipped.length + " 条未被采纳：" + reasons.join("；"), "error");
                }
                if (result.notes.length) {
                    toast(result.notes.join("；"), "info");
                }
                return data;
            });
    }

    function updateKeyword(id, patch, failMsg) {
        return guardWrite("PUT", "/keywords/" + encodeURIComponent(id), patch, failMsg || "更新关键词失败")
            .then(function (data) {
                if (data) toast("关键词已更新", "success");
                return data;
            });
    }

    function deleteKeyword(id) {
        var kw = findKeyword(id);
        if (!kw) return;
        confirmThen("删除关键词「" + (kw.word || id) + "」？删除后相关提问将不再被拦截。", function () {
            guardWrite("DELETE", "/keywords/" + encodeURIComponent(id), undefined, "删除关键词失败")
                .then(function (data) {
                    if (data) toast("关键词已删除", "success");
                });
        });
    }

    // ── Guard 导入 / 导出 ───────────────────────────────────
    function exportGuard() {
        return guardApi("GET", "/export").then(function (data) {
            data = data || {};
            // 后端返回 { payload, filename }；兼容直接返回配置对象的情况。
            // 只导出 payload（纯规则配置），导出的文件可直接再导入。
            var payload = (data.payload && typeof data.payload === "object") ? data.payload : data;
            var filename = (typeof data.filename === "string" && data.filename)
                ? data.filename : "aegis-guard-rules.json";
            var text = JSON.stringify(payload, null, 2);
            try {
                var blob = new Blob([text], { type: "application/json;charset=utf-8" });
                var url = URL.createObjectURL(blob);
                var link = document.createElement("a");
                link.href = url;
                link.download = filename;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                setTimeout(function () {
                    try { URL.revokeObjectURL(url); } catch (e) { /* 忽略 */ }
                }, 2000);
                toast("规则已导出为 " + filename, "success");
            } catch (e) {
                toast("浏览器不支持文件下载：" + (e && e.message ? e.message : "未知原因"), "error");
            }
            return data;
        }).catch(function (err) {
            toast("导出失败：" + errText(err), "error");
            return null;
        });
    }

    function handleImportText(text) {
        var cfg = null;
        try {
            cfg = JSON.parse(text);
        } catch (e) {
            toast("导入失败：文件不是合法的 JSON", "error");
            return;
        }
        if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) {
            toast("导入失败：JSON 根节点必须是一个对象", "error");
            return;
        }
        confirmThen("导入会覆盖当前全部回答限制规则（类别与关键词），确定继续吗？", function () {
            guardApi("POST", "/import", cfg)
                .then(function () {
                    return fetchGuard();
                })
                .then(function () {
                    renderGuard();
                    toast("规则已导入", "success");
                })
                .catch(function (err) {
                    toast("导入失败：" + errText(err), "error");
                });
        });
    }

    function importGuardFile(file) {
        if (!file) return;
        if (typeof FileReader !== "function") {
            toast("当前环境不支持读取本地文件", "error");
            return;
        }
        var reader = new FileReader();
        reader.onload = function () {
            handleImportText(String(reader.result == null ? "" : reader.result));
        };
        reader.onerror = function () {
            toast("文件读取失败，请重试", "error");
        };
        reader.readAsText(file);
    }

    // ── Guard 调试自检 ──────────────────────────────────────
    function renderCheckResult(data) {
        var box = $("gdTestResult");
        if (!box) return;
        var lines = [];
        var blocked = !!data.blocked;
        box.hidden = false;
        box.classList.remove("is-ok", "is-fail");
        box.classList.add(blocked ? "is-fail" : "is-ok");
        if (blocked) {
            lines.push("结果：会被拦截");
            lines.push("命中类别：" + (data.category ? categoryLabel(data.category) : "-"));
            lines.push("命中关键词：" + (data.keyword || "-"));
            lines.push("匹配方式：" + modeLabel(data.mode));
            lines.push("优先级：" + (data.priority === undefined || data.priority === null ? "-" : data.priority));
            lines.push("原因：" + (data.reason || "-"));
        } else {
            lines.push("结果：不会拦截，AI 会正常回答");
            if (data.reason) lines.push("说明：" + data.reason);
        }
        box.textContent = lines.join("\n");
    }

    function runGuardCheck() {
        var input = $("gdTestInput");
        var text = String((input && input.value) || "").trim();
        if (!text) {
            toast("请先输入要测试的一句话", "error");
            return Promise.resolve(null);
        }
        return guardApi("POST", "/check", { text: text })
            .then(function (data) {
                renderCheckResult(data || {});
                return data;
            })
            .catch(function (err) {
                toast("自检失败：" + errText(err), "error");
                return null;
            });
    }

    // ── Guard 事件绑定 ──────────────────────────────────────
    function onGuardCategoryClick(e) {
        var target = e.target;
        if (!target || !target.getAttribute) return;
        var act = target.getAttribute("data-act");
        if (act !== "rename" && act !== "delete") return;
        var id = target.getAttribute("data-id") || "";
        if (act === "rename") renameCategory(id);
        else deleteCategory(id);
    }

    function onGuardCategoryChange(e) {
        var target = e.target;
        if (!target || !target.getAttribute) return;
        var id = target.getAttribute("data-id") || "";
        if (target.getAttribute("data-field") !== "enabled") return;
        toggleCategory(id, !!target.checked);
    }

    function onGuardKeywordClick(e) {
        var target = e.target;
        if (!target || !target.getAttribute) return;
        if (target.getAttribute("data-act") !== "del-kw") return;
        deleteKeyword(target.getAttribute("data-id") || "");
    }

    function onGuardKeywordChange(e) {
        var target = e.target;
        if (!target || !target.getAttribute) return;
        var id = target.getAttribute("data-id") || "";
        var field = target.getAttribute("data-field") || "";
        if (!id || !field) return;
        var kw = findKeyword(id);
        if (!kw) return;

        if (field === "mode") {
            updateKeyword(id, { mode: (target.value === MODE_EXACT) ? MODE_EXACT : MODE_FUZZY }, "更新匹配方式失败");
            return;
        }
        if (field === "category") {
            updateKeyword(id, { category: target.value || "" }, "更新所属类别失败");
            return;
        }
        if (field === "enabled") {
            updateKeyword(id, { enabled: !!target.checked }, "更新关键词状态失败");
            return;
        }
        if (field === "priority") {
            var num = parseInt(target.value, 10);
            if (isNaN(num)) {
                toast("优先级必须是整数", "error");
                renderGuardKeywords();
                return;
            }
            updateKeyword(id, { priority: num }, "更新优先级失败");
        }
    }

    function bindGuard() {
        var master = $("gdEnabled");
        if (master) {
            master.addEventListener("change", function () {
                setGuardEnabled(master.checked);
            });
        }

        var refusal = $("gdRefusal");
        if (refusal) refusal.addEventListener("change", function () { saveRefusal(refusal.value); });
        if ($("gdRefusalReset")) $("gdRefusalReset").addEventListener("click", resetRefusal);

        var catLabel = $("gdCatLabel");
        if (catLabel) {
            catLabel.addEventListener("keydown", function (e) {
                if (e.key === "Enter") addCategory();
            });
        }
        if ($("gdCatAddBtn")) $("gdCatAddBtn").addEventListener("click", addCategory);

        var catList = $("gdCatList");
        if (catList) {
            catList.addEventListener("click", onGuardCategoryClick);
            catList.addEventListener("change", onGuardCategoryChange);
        }

        var kwList = $("gdKwList");
        if (kwList) {
            kwList.addEventListener("click", onGuardKeywordClick);
            kwList.addEventListener("change", onGuardKeywordChange);
        }

        if ($("gdBatchAddBtn")) $("gdBatchAddBtn").addEventListener("click", submitBatchKeywords);
        if ($("gdExportBtn")) $("gdExportBtn").addEventListener("click", exportGuard);

        var importBtn = $("gdImportBtn");
        var fileInput = $("gdFile");
        if (importBtn && fileInput) {
            importBtn.addEventListener("click", function () {
                try { fileInput.click(); } catch (e) { toast("无法打开文件选择框", "error"); }
            });
            fileInput.addEventListener("change", function () {
                var file = (fileInput.files && fileInput.files[0]) ? fileInput.files[0] : null;
                importGuardFile(file);
                // 允许连续导入同一个文件
                try { fileInput.value = ""; } catch (e) { /* 忽略 */ }
            });
        }

        if ($("gdTestBtn")) $("gdTestBtn").addEventListener("click", runGuardCheck);
        var testInput = $("gdTestInput");
        if (testInput) {
            testInput.addEventListener("keydown", function (e) {
                if (e.key === "Enter") runGuardCheck();
            });
        }
    }

    // ── 弹窗开关 ────────────────────────────────────────────
    function openModal() {
        var modal = $("settingsModal");
        if (!modal) return;
        /* 与 closeModal 的内联 display 兜底对称：清掉它，保证二次打开不会被
           上一次关闭留下的 inline style 卡住。 */
        try { modal.style.display = ""; } catch (e) { /* 忽略 */ }
        modal.classList.add("is-open");
        modal.setAttribute("aria-hidden", "false");
    }

    function closeModal() {
        /* hideKey() 只是「收回密钥明文」的辅助动作，**绝不能影响关窗**。
           此前它一旦抛错，后面的 classList.remove 就执行不到，
           表现为「点了 × / 保存完全没反应」。这里整体包 try 保证关闭必定完成。 */
        try { hideKey(); } catch (e) { /* 忽略：关窗优先 */ }
        var modal = $("settingsModal");
        if (!modal) return;
        modal.classList.remove("is-open");
        modal.setAttribute("aria-hidden", "true");
        /* 兜底：万一某个宿主样式仍让面板可见，直接内联隐藏。
           仅在 class 移除后元素仍占据布局时生效（display 非 none）。 */
        try {
            var cs = window.getComputedStyle ? window.getComputedStyle(modal) : null;
            if (cs && cs.display && cs.display !== "none") {
                modal.style.display = "none";
            }
        } catch (e2) { /* 忽略 */ }
    }

    function isOpen() {
        var modal = $("settingsModal");
        return !!modal && modal.classList.contains("is-open");
    }

    function open() {
        if (!$("settingsModal")) {
            toast("设置面板未加载", "error");
            return Promise.resolve(null);
        }
        openModal();
        // 「回答限制」与服务商配置彼此独立：guard 取数失败只影响它自己那一块，
        // 所以拆成并行的两条链各自兜底，绝不中断 open()。
        var providersTask = fetchProviders().then(function (data) {
            if (!data) return null;
            renderProviderSelect();
            renderProvider();
            return data;
        });
        var guardTask = fetchGuard()
            .then(function () {
                renderGuard();
                return guardState;
            })
            .catch(function (err) {
                toast("读取回答限制规则失败：" + errText(err), "error");
                try {
                    renderGuard();   // 渲染空态，保证面板不是空白
                } catch (e) { /* 渲染异常也不能冒泡到 open() */ }
                return null;
            });
        return Promise.all([providersTask, guardTask]).then(function (results) {
            return results[0];
        });
    }

    // ── 顶部黄条（未完成配置时的常驻提示）───────────────────
    function applyBanner(needSetup) {
        var banner = $("aegisSetupBanner");
        var input = $("chatInput");
        var sendBtn = $("sendBtn");
        if (banner) banner.hidden = !needSetup;
        if (input) {
            input.disabled = !!needSetup;
            if (needSetup) {
                input.setAttribute("placeholder", "请先在设置中配置模型服务…");
            } else {
                input.setAttribute("placeholder", "给 Aegis 发送消息…");
            }
        }
        // 未配置时彻底禁用发送；已配置时交还给页面自身的输入监听逻辑
        if (sendBtn && needSetup) sendBtn.disabled = true;
    }

    // ── 事件绑定 ────────────────────────────────────────────
    var bound = false;

    function bind() {
        if (bound || !$("settingsModal")) return;
        bound = true;

        var modal = $("settingsModal");

        modal.addEventListener("click", function (e) {
            /* 必须逐级向上找带 data-act 的祖先，**不能只看 e.target**。
               × 按钮内部包着 <svg>（还有 <path>），真实点击时 e.target
               就是 svg / path —— 它们没有 data-act 属性，
               直接读 e.target.getAttribute("data-act") 恒为 null，
               于是「× 关不掉、取消却能关」（取消是直接绑 id 的，不走委托）。
               这正是该现象的根因。 */
            var act = null;
            var node = e.target;
            while (node && node !== modal) {
                if (node.getAttribute) {
                    var a = node.getAttribute("data-act");
                    if (a) { act = a; break; }
                }
                node = node.parentNode;
            }
            if (act === "close") closeModal();
        });

        // ESC 关闭：遮罩点击与关闭按钮之外的键盘关闭方式。
        // 只切 is-open（display:none），不改 body 布局/滚动，
        // 因此关闭后主界面完整恢复，无遮罩残留。
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" || e.key === "Esc") {
                if (isOpen()) closeModal();
            }
        });

        var sel = $("stProviderSel");
        if (sel) {
            sel.addEventListener("change", function () {
                // 切换前固化当前编辑值
                var baseUrl = $("stBaseUrl");
                var model = $("stModel");
                var apiKey = $("stApiKey");
                var timeout = $("stTimeout");
                var draft = draftOf(state.current);
                if (baseUrl) draft.base_url = baseUrl.value.trim();
                if (model) draft.model = model.value.trim();
                if (apiKey) draft.api_key = apiKey.value;
                if (timeout) draft.timeout = parseInt(timeout.value, 10) || 60;
                var ctxLen = $("stContextLen");
                if (ctxLen) draft.context_length = Math.max(0, parseInt(ctxLen.value, 10) || 0);

                state.current = sel.value;
                hideKey();
                renderProvider();
            });
        }

        if ($("stToggleKey")) $("stToggleKey").addEventListener("click", onToggleKey);
        if ($("stCustAddBtn")) $("stCustAddBtn").addEventListener("click", addCustomProvider);
        if ($("stFetchModelsBtn")) $("stFetchModelsBtn").addEventListener("click", fetchModels);
        /* 模型区也放了一个「拉取模型」：对应当前选中的厂商，
           拉到的清单同时写入 stModelList 下拉。 */
        if ($("stFetchModelsBtn2")) $("stFetchModelsBtn2").addEventListener("click", function () {
            fetchModelsFor(state.current);
        });
        if ($("stCustDelBtn")) $("stCustDelBtn").addEventListener("click", deleteCustomProvider);
        if ($("stTestBtn")) $("stTestBtn").addEventListener("click", function () { testConnection(false); });
        if ($("stCancelBtn")) $("stCancelBtn").addEventListener("click", closeModal);
    if ($("stSaveBtn")) $("stSaveBtn").addEventListener("click", function () {
        var btn = $("stSaveBtn");
        if (btn && btn.disabled) return;
        /* 保存期间禁用按钮，避免用户连点造成重复提交 */
        if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
        save(false).then(function () {
            /* 成功：关闭设置窗 */
            closeModal();
        }).catch(function (err) {
            /* 失败：**保持窗口打开**并给出明确原因，让用户能就地修改后重试。
               err 可能是字符串、网络异常对象等非 Error 实例，
               统一用 String() 兜底，避免提示语本身再抛错导致「点了没反应」。 */
            var msg = "";
            try {
                msg = (err && err.message) ? err.message
                    : (err && err.msg) ? err.msg
                    : (err ? String(err) : "");
            } catch (e) { msg = ""; }
            toast("保存失败：" + (msg || "请查看服务日志"), "error");
        }).then(function () {
            if (btn) { btn.disabled = false; btn.textContent = "保存"; }
        });
    });

        var bannerBtn = $("aegisBannerConfigBtn");
        if (bannerBtn) bannerBtn.addEventListener("click", function () { open(); });

        try {
            bindGuard();
        } catch (e) {
            // 回答限制面板绑定失败不影响设置弹窗的其它功能
            if (window.console && window.console.warn) {
                window.console.warn("bindGuard 失败：", e);
            }
        }

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && isOpen()) closeModal();
        });
    }

    // ── 导出 ────────────────────────────────────────────────
    var AegisSettings = {
        state: state,
        open: open,
        close: closeModal,
        isOpen: isOpen,
        refresh: fetchProviders,
        fetchBootstrap: fetchBootstrap,
        fetchProviders: fetchProviders,
        save: save,
        saveConfig: saveConfig,
        testConnection: testConnection,
        applyBanner: applyBanner,
        hideKey: hideKey,
        esc: esc,
        escAttr: escAttr,
        safeUrl: safeUrl,
        request: request,
        toast: toast,
        bind: bind,
        // ── 回答限制规则 ──
        guardState: guardState,
        fetchGuard: fetchGuard,
        renderGuard: renderGuard,
        guardApi: guardApi,
        parseKeywordBatch: parseKeywordBatch,
        setGuardEnabled: setGuardEnabled,
        saveRefusal: saveRefusal,
        addCategory: addCategory,
        deleteCategory: deleteCategory,
        updateKeyword: updateKeyword,
        deleteKeyword: deleteKeyword,
        submitBatchKeywords: submitBatchKeywords,
        exportGuard: exportGuard,
        importGuardFile: importGuardFile,
        runGuardCheck: runGuardCheck,
    };

    window.AegisSettings = AegisSettings;

    function init() {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", function () { bind(); });
        } else {
            bind();
        }
    }
    init();
})();
