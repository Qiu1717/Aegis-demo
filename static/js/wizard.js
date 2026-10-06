/**
 * Aegis — 首次启动向导（3 步，AEG-K03）
 *
 * 第 1 步 欢迎说明
 * 第 2 步 选择服务商（智谱 ⭐推荐 + 免费额度说明 + 申请链接）
 * 第 3 步 填 API Key + 选工作区 + 测试连接
 *
 * 可「以后再说」跳过，但跳过后：
 *   · 输入框禁用；
 *   · 顶部常驻黄条「⚠️ 尚未配置模型服务，Aegis 现在无法工作」+ [立即配置]。
 *
 * 依赖：auth.js、settings.js（AegisSettings）、common.js（escapeText / showToast / showPrompt）
 */
(function () {
    "use strict";

    var STEP_COUNT = 3;

    var state = {
        step: 1,
        presets: [],
        providerKey: "",
        workspace: "",
        tested: false,
        testing: false,
        bootstrap: null,
    };

    // ── 小工具（复用 settings.js 暴露的转义/提示，缺失时自建）──
    function esc(text) {
        if (window.AegisSettings && window.AegisSettings.esc) return window.AegisSettings.esc(text);
        if (typeof window.escapeText === "function") return window.escapeText(text);
        var div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }

    function escAttr(text) {
        if (window.AegisSettings && window.AegisSettings.escAttr) return window.AegisSettings.escAttr(text);
        if (typeof window.escapeAttrText === "function") return window.escapeAttrText(text);
        return esc(text).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function safeUrl(raw) {
        if (window.AegisSettings && window.AegisSettings.safeUrl) return window.AegisSettings.safeUrl(raw);
        if (!raw) return "";
        try {
            var url = new URL(String(raw), window.location.origin);
            if (url.protocol === "http:" || url.protocol === "https:") return url.href;
        } catch (e) { /* 非法 URL 丢弃 */ }
        return "";
    }

    function toast(msg, type) {
        if (window.AegisSettings && window.AegisSettings.toast) {
            window.AegisSettings.toast(msg, type);
            return;
        }
        if (typeof window.showToast === "function") {
            window.showToast(msg, type || "info", 3000);
        }
    }

    function request(url, opts) {
        if (window.AegisSettings && window.AegisSettings.request) {
            return window.AegisSettings.request(url, opts);
        }
        return fetch(url, opts).then(function (r) { return r.json(); });
    }

    function $(id) { return document.getElementById(id); }

    function findPreset(key) {
        for (var i = 0; i < state.presets.length; i++) {
            if (state.presets[i].key === key) return state.presets[i];
        }
        return null;
    }

    // ── 弹窗与步骤切换 ──────────────────────────────────────
    function openModal() {
        var modal = $("wizardModal");
        if (!modal) return;
        modal.classList.add("is-open");
        modal.setAttribute("aria-hidden", "false");
    }

    function closeModal() {
        var modal = $("wizardModal");
        if (!modal) return;
        modal.classList.remove("is-open");
        modal.setAttribute("aria-hidden", "true");
    }

    function isOpen() {
        var modal = $("wizardModal");
        return !!modal && modal.classList.contains("is-open");
    }

    var TITLES = ["欢迎使用 Aegis 桌面版", "选择模型服务商", "填写密钥与工作区"];

    function goStep(step) {
        state.step = Math.min(STEP_COUNT, Math.max(1, step));

        for (var i = 1; i <= STEP_COUNT; i++) {
            var pane = $("wzPane" + i);
            if (pane) pane.hidden = (i !== state.step);
            var dot = $("wizardSteps");
            if (dot) {
                var items = dot.querySelectorAll(".aegis-steps__item");
                for (var j = 0; j < items.length; j++) {
                    items[j].classList.toggle("is-active", j === state.step - 1);
                    items[j].classList.toggle("is-done", j < state.step - 1);
                }
            }
        }

        var title = $("wizardTitle");
        if (title) title.textContent = TITLES[state.step - 1] || "";
        var stepText = $("wizardStepText");
        if (stepText) stepText.textContent = "第 " + state.step + " / " + STEP_COUNT + " 步";

        var prev = $("wzPrevBtn");
        if (prev) prev.hidden = (state.step === 1);
        var next = $("wzNextBtn");
        if (next) next.hidden = (state.step === STEP_COUNT);
        var finish = $("wzFinishBtn");
        if (finish) finish.hidden = (state.step !== STEP_COUNT);

        if (state.step === 2) renderProviders();
        if (state.step === 3) prepareStep3();
    }

    // ── 第 2 步：服务商列表 ─────────────────────────────────
    function renderProviders() {
        var ul = $("wzProviderList");
        if (!ul) return;

        if (!state.providerKey) {
            for (var i = 0; i < state.presets.length; i++) {
                if (state.presets[i].recommended) { state.providerKey = state.presets[i].key; break; }
            }
            if (!state.providerKey && state.presets.length) {
                state.providerKey = state.presets[0].key;
            }
        }

        var html = "";
        for (var k = 0; k < state.presets.length; k++) {
            var p = state.presets[k];
            var href = safeUrl(p.key_url);
            html += '<li class="aegis-provider-item'
                + (p.key === state.providerKey ? " is-selected" : "") + '"'
                + ' data-key="' + escAttr(p.key) + '" role="button" tabindex="0">'
                + '<div class="aegis-provider-item__main">'
                + '<span class="aegis-radio"></span>'
                + '<span class="aegis-provider-item__label">' + esc(p.label) + "</span>"
                + (p.recommended ? '<span class="aegis-badge">⭐推荐</span>' : "")
                + (p.has_key ? '<span class="aegis-badge aegis-badge--ok">已配置</span>' : "")
                + "</div>"
                + '<div class="aegis-provider-item__sub">'
                + esc(p.free_note || "")
                + (href ? ' · <a href="' + escAttr(href) + '" target="_blank" rel="noopener noreferrer">申请 Key ↗</a>' : "")
                + "</div>"
                + "</li>";
        }
        ul.innerHTML = html;
    }

    // ── 第 3 步：预填与交互 ─────────────────────────────────
    function prepareStep3() {
        var preset = findPreset(state.providerKey) || {};

        var modelList = $("wzModelList");
        if (modelList) {
            var models = preset.candidate_models || [];
            var html = "";
            for (var i = 0; i < models.length; i++) {
                html += '<option value="' + escAttr(models[i]) + '"></option>';
            }
            modelList.innerHTML = html;
        }
        var modelInput = $("wzModel");
        if (modelInput && !modelInput.value) {
            modelInput.value = preset.default_model || "";
        }

        var wsInput = $("wzWorkspace");
        if (wsInput && !wsInput.value) {
            wsInput.value = (state.bootstrap && (state.bootstrap.workspace || state.bootstrap.workspace_suggest)) || "";
        }

        var urlHint = $("wzKeyUrl");
        if (urlHint) {
            var href = safeUrl(preset.key_url);
            if (href) {
                urlHint.innerHTML = '申请地址：<a href="' + escAttr(href)
                    + '" target="_blank" rel="noopener noreferrer">' + esc(href) + "</a>";
            } else {
                urlHint.textContent = preset.requires_key === false ? "该服务商无需 API Key" : "";
            }
        }

        var keyInput = $("wzApiKey");
        if (keyInput) {
            keyInput.placeholder = preset.requires_key === false ? "（可选）留空即可" : "粘贴你的 API Key";
        }

        var result = $("wzTestResult");
        if (result) { result.hidden = true; result.textContent = ""; }
        state.tested = false;
    }

    function showTestResult(ok, text) {
        var result = $("wzTestResult");
        if (!result) return;
        result.hidden = false;
        result.className = "aegis-test-result " + (ok ? "is-ok" : "is-fail");
        result.textContent = text;
    }

    function testConnection() {
        if (state.testing) return Promise.resolve(null);
        var preset = findPreset(state.providerKey) || {};
        var apiKey = $("wzApiKey") ? $("wzApiKey").value.trim() : "";
        var model = $("wzModel") ? $("wzModel").value.trim() : "";

        if (preset.requires_key !== false && !apiKey) {
            toast("请先填写 API Key", "error");
            return Promise.resolve(null);
        }

        state.testing = true;
        var btn = $("wzTestBtn");
        if (btn) { btn.disabled = true; btn.textContent = "测试中…"; }
        showTestResult(false, "正在测试连接…");

        return request("/api/settings/test", {
            method: "POST",
            body: JSON.stringify({
                provider_key: state.providerKey,
                api_key: apiKey,
                base_url: preset.base_url || "",
                model: model || preset.default_model || "",
                timeout: 30,
            }),
        }).then(function (data) {
            data = data || {};
            if (data.ok) {
                state.tested = true;
                showTestResult(true, "✅ 连接成功（延迟 " + (data.latency_ms || 0) + "ms）");
            } else {
                var err = data.error || {};
                var text = "⚠️ " + (err.reason || "连接失败");
                if (err.action) text += "\n下一步：" + err.action;
                showTestResult(false, text);
            }
            return data;
        }).catch(function (err) {
            showTestResult(false, "⚠️ 测试连接失败："
                + (err && err.message ? err.message : "请查看服务日志"));
            return null;
        }).then(function (data) {
            state.testing = false;
            if (btn) { btn.disabled = false; btn.textContent = "测试连接"; }
            return data;
        });
    }

    // ── 完成 / 跳过 ─────────────────────────────────────────
    function finish() {
        var preset = findPreset(state.providerKey) || {};
        var apiKey = $("wzApiKey") ? $("wzApiKey").value.trim() : "";
        var model = $("wzModel") ? $("wzModel").value.trim() : (preset.default_model || "");
        var workspace = $("wzWorkspace") ? $("wzWorkspace").value.trim() : "";

        if (preset.requires_key !== false && !apiKey) {
            toast("请先填写 API Key", "error");
            return Promise.resolve(null);
        }
        // 注意：工作区不再是「保存密钥」的前置条件。
        // 早前两者耦合，导致目录选不上时密钥也一并保存失败；
        // 现在允许先保存密钥，工作区可随后在左侧「选择目录」补选。

        var payload = {
            active: state.providerKey,
            chain: [state.providerKey, "offline"],
            providers: [{
                key: state.providerKey,
                enabled: true,
                base_url: preset.base_url || "",
                model: model,
                api_key: apiKey,
                timeout: 60,
            }],
        };
        if (workspace) payload.workspace = workspace;

        var btn = $("wzFinishBtn");
        if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }

        return (window.AegisSettings
            ? window.AegisSettings.saveConfig(payload)
            : request("/api/settings/providers", {
                method: "PUT",
                body: JSON.stringify(payload),
            }))
            .then(function () {
                closeModal();
                return refreshSetupState();
            })
            .then(function (boot) {
                if (!workspace) {
                    toast("密钥已保存；请点左下角「选择目录」设置工作区后开始使用", "info");
                }
                if (boot && boot.need_setup) {
                    toast("已保存，但服务仍不可用，请检查密钥或工作区", "error");
                } else {
                    toast("配置完成，Aegis 已就绪", "success");
                }
                return boot;
            })
            .catch(function (err) {
                toast("保存失败：" + (err && err.message ? err.message : "请查看服务日志"), "error");
                return null;
            })
            .then(function (boot) {
                if (btn) { btn.disabled = false; btn.textContent = "完成，开始使用"; }
                return boot;
            });
    }

    function skip() {
        closeModal();
        if (window.AegisSettings) window.AegisSettings.applyBanner(true);
        toast("已跳过配置：Aegis 现在无法工作，可随时点击顶部提示进行配置", "info");
    }

    /** 重新拉取引导状态并同步顶部黄条 / 输入框可用性 */
    function refreshSetupState() {
        var fetcher = window.AegisSettings
            ? window.AegisSettings.fetchBootstrap()
            : request("/api/settings/bootstrap", { method: "GET" }).then(function (r) {
                return (r && r.data) || r;
            });

        return fetcher.then(function (boot) {
            boot = boot || {};
            state.bootstrap = boot;
            state.presets = boot.presets || state.presets;
            if (window.AegisSettings) window.AegisSettings.applyBanner(!!boot.need_setup);
            return boot;
        }).catch(function () {
            return null;
        });
    }

    function start(force) {
        if (!$("wizardModal")) return Promise.resolve(null);
        if (!force && !state.presets.length) {
            return refreshSetupState().then(function (boot) {
                if (!boot) return null;
                if (!boot.need_setup) return null;
                openModal();
                goStep(1);
                return boot;
            });
        }
        openModal();
        goStep(1);
        return Promise.resolve(state.bootstrap);
    }

    // ── 事件绑定 ────────────────────────────────────────────
    var bound = false;

    function pickWorkspace() {
        var input = $("wzWorkspace");
        if (!input) return;

        // 只走桌面壳的系统原生目录选择对话框（可浏览各磁盘分区）。
        // pywebview 只在桌面壳里注入 window.pywebview；api 方法由 Python 侧异步注入，
        // ready 事件挂在 window 上（window.pywebview 本身没有 addEventListener 方法）。
        function desktopBridge() {
            var pw = window.pywebview;
            if (pw && pw.api && typeof pw.api.open_folder_dialog === "function") return pw.api;
            return null;
        }

        var bridge = desktopBridge();
        if (bridge) {
            Promise.resolve(bridge.open_folder_dialog()).then(function (path) {
                path = (path || "").trim();
                if (path) {
                    input.value = path;
                } else {
                    // 用户取消，或对话框异常被兜底为空——都给明确反馈，不再静默
                    toast("未选择任何目录", "info");
                }
            }).catch(function () {
                toast("系统目录选择器不可用，请重启 Aegis 后重试", "error");
            });
            return;
        }

        if (window.pywebview && window.pywebview.platform) {
            // 桌面壳但桥还没好：轮询等待（最长约 6 秒）
            toast("正在唤醒系统目录选择器…", "info");
            var tries = 0;
            var timer = setInterval(function () {
                var b = desktopBridge();
                if (b) {
                    clearInterval(timer);
                    Promise.resolve(b.open_folder_dialog()).then(function (path) {
                        path = (path || "").trim();
                        if (path) input.value = path;
                        else toast("未选择任何目录", "info");
                    }).catch(function () {
                        toast("系统目录选择器不可用，请重启 Aegis 后重试", "error");
                    });
                    return;
                }
                tries += 1;
                if (tries >= 15) {
                    clearInterval(timer);
                    toast("桌面组件未就绪，请完全退出 Aegis 后重新打开再试", "error");
                }
            }, 400);
            return;
        }
        toast("请在 Aegis 桌面应用内选择工作区；浏览器环境不支持系统目录选择器", "error");
    }

    function bind() {
        var modal = $("wizardModal");
        if (bound || !modal) return;
        bound = true;

        var next = $("wzNextBtn");
        if (next) next.addEventListener("click", function () {
            if (state.step === 2 && !state.providerKey) {
                toast("请先选择一个服务商", "error");
                return;
            }
            goStep(state.step + 1);
        });

        var prev = $("wzPrevBtn");
        if (prev) prev.addEventListener("click", function () { goStep(state.step - 1); });

        var skipBtn = $("wzSkipBtn");
        if (skipBtn) skipBtn.addEventListener("click", skip);

        var finishBtn = $("wzFinishBtn");
        if (finishBtn) finishBtn.addEventListener("click", function () { finish(); });

        var testBtn = $("wzTestBtn");
        if (testBtn) testBtn.addEventListener("click", function () { testConnection(); });

        var browseBtn = $("wzBrowseBtn");
        if (browseBtn) browseBtn.addEventListener("click", pickWorkspace);

        var list = $("wzProviderList");
        if (list) {
            list.addEventListener("click", function (e) {
                var li = e.target.closest ? e.target.closest(".aegis-provider-item") : null;
                if (!li || (e.target.tagName === "A")) return;
                state.providerKey = li.getAttribute("data-key") || state.providerKey;
                renderProviders();
            });
            list.addEventListener("keydown", function (e) {
                if (e.key !== "Enter" && e.key !== " ") return;
                var li = e.target.closest ? e.target.closest(".aegis-provider-item") : null;
                if (!li) return;
                e.preventDefault();
                state.providerKey = li.getAttribute("data-key") || state.providerKey;
                renderProviders();
            });
        }

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && isOpen()) skip();
        });
    }

    // ── 导出 ────────────────────────────────────────────────
    var AegisWizard = {
        state: state,
        start: start,
        close: closeModal,
        goStep: goStep,
        finish: finish,
        skip: skip,
        testConnection: testConnection,
        refreshSetupState: refreshSetupState,
        bind: bind,
    };

    window.AegisWizard = AegisWizard;

    function init() {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", function () {
                bind();
                if ($("wizardModal")) start(false);
            });
        } else {
            bind();
            if ($("wizardModal")) start(false);
        }
    }
    init();
})();
