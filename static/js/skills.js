/* ==========================================================================
   技能中心（独立页面 /skills）
   --------------------------------------------------------------------------
   加载顺序：/token.js → auth.js（AegisAuth，自动带 X-Aegis-Token）→
             common.js（showToast / showConfirm）→ 本文件。

   设计要点：
     · 本页是**独立路由页面**，不是浮层弹窗，因此不存在遮挡主页面、
       背景半透明导致底层透视、以及文字与底层内容重叠的问题。
     · 返回入口指向 /workspace（工作台），不再指向旧的 /aegis，
       避免「工作台 ↔ 技能中心」来回切换时退回旧版界面。
     · 所有数据都来自 apps/skills 的真实读写（持久化到用户配置文件）；
       每次变更后重新拉取服务端，保证展示即真值。
     · 技能名称/描述是用户输入，一律用 textContent 写入，杜绝 XSS。
   ========================================================================== */
(function () {
    "use strict";

    function $(id) { return document.getElementById(id); }

    function toast(msg, type) {
        if (typeof window.showToast === "function") {
            window.showToast(msg, type || "info", 3000);
            return;
        }
        window.alert(msg);
    }

    function ask(msg, title) {
        if (typeof window.showConfirm === "function") {
            return window.showConfirm(msg, title);
        }
        return Promise.resolve(window.confirm(msg));
    }

    /** 创建元素；文本一律走 textContent（不解析 HTML） */
    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function errText(e) {
        return (e && e.message) ? e.message : "未知错误";
    }

    var listEl = $("skList");
    var emptyEl = $("skEmpty");

    // ── 读取并渲染（真实接口）──────────────────────────────
    function loadSkills() {
        if (!window.AegisAuth) {
            toast("鉴权组件未加载，请刷新页面重试", "error");
            return;
        }
        window.AegisAuth.get("/api/skills").then(function (res) {
            var data = (res && res.data) || {};
            renderSkills(data.skills || []);
        }).catch(function (e) {
            toast("读取技能清单失败：" + errText(e), "error");
        });
    }

    function renderSkills(list) {
        if (!listEl) return;
        listEl.textContent = "";

        if (emptyEl) emptyEl.hidden = list.length > 0;
        if (!list.length) return;

        list.forEach(function (s) {
            var id = s.id || "";
            var row = el("div", "sk-row" + (s.enabled ? " is-on" : ""));

            // 文本列：min-width:0 + 可换行，保证永不与右侧按钮重叠
            var main = el("div", "sk-main");
            main.appendChild(el("div", "sk-name", s.name || id));
            if (s.description) main.appendChild(el("div", "sk-desc", s.description));
            row.appendChild(main);

            // 操作列：固定宽度，不参与压缩
            var ctrl = el("div", "sk-ctrl");
            if (s.built_in) ctrl.appendChild(el("span", "sk-badge is-builtin", "内置"));
            ctrl.appendChild(el("span", "sk-badge " + (s.enabled ? "is-on" : "is-off"),
                                s.enabled ? "已启用" : "已禁用"));

            var toggle = el("button", "aw-btn" + (s.enabled ? "" : " aw-btn-primary"),
                            s.enabled ? "禁用" : "启用");
            toggle.type = "button";
            toggle.addEventListener("click", function () { toggleSkill(id, !s.enabled); });
            ctrl.appendChild(toggle);

            // 内置技能不允许删除
            if (!s.built_in) {
                var del = el("button", "aw-btn aw-btn-danger", "删除");
                del.type = "button";
                del.addEventListener("click", function () { removeSkill(id, s.name || id); });
                ctrl.appendChild(del);
            }

            row.appendChild(ctrl);
            listEl.appendChild(row);
        });
    }

    function toggleSkill(id, enabled) {
        if (!id) return;
        window.AegisAuth.put("/api/skills/" + encodeURIComponent(id), { enabled: !!enabled })
            .then(function () {
                toast(enabled ? "技能已启用" : "技能已禁用", "success");
                loadSkills();   // 重新拉取，确保界面即服务端持久化结果
            })
            .catch(function (e) {
                toast("更新技能失败：" + errText(e), "error");
            });
    }

    function removeSkill(id, name) {
        ask("将删除技能「" + name + "」，删除后不可恢复。", "删除技能").then(function (ok) {
            if (!ok) return;
            window.AegisAuth.del("/api/skills/" + encodeURIComponent(id))
                .then(function () {
                    toast("技能已删除", "success");
                    loadSkills();
                })
                .catch(function (e) {
                    toast("删除技能失败：" + errText(e), "error");
                });
        });
    }

    function registerSkill() {
        var nameEl = $("skName"), descEl = $("skDesc"), instrEl = $("skInstr");
        var name = (nameEl && nameEl.value || "").trim();
        var desc = (descEl && descEl.value || "").trim();
        var instr = (instrEl && instrEl.value || "").trim();
        if (!name || !instr) {
            toast("请填写技能名称与指令内容", "info");
            return;
        }
        window.AegisAuth.post("/api/skills", {
            name: name, description: desc, instructions: instr
        }).then(function () {
            toast("技能已注册", "success");
            if (nameEl) nameEl.value = "";
            if (descEl) descEl.value = "";
            if (instrEl) instrEl.value = "";
            loadSkills();
        }).catch(function (e) {
            toast("注册技能失败：" + errText(e), "error");
        });
    }

    var regBtn = $("btnRegSkill");
    if (regBtn) regBtn.addEventListener("click", registerSkill);

    // ── 从文件导入 ─────────────────────────────────────────
    var fileInput = $("skFile");
    var dropEl = $("skDrop");
    var nameEl = $("skFileName");
    var importBtn = $("btnImport");
    var clearBtn = $("btnClearFile");
    var resultEl = $("skImportResult");

    var pending = null;   // 解析成功待导入的载荷

    function setPending(payload, label) {
        pending = payload;
        if (nameEl) nameEl.textContent = label || "未选择文件";
        if (importBtn) importBtn.disabled = !payload;
        if (clearBtn) clearBtn.disabled = !payload;
    }

    function clearPending() {
        pending = null;
        if (fileInput) fileInput.value = "";
        if (nameEl) nameEl.textContent = "未选择文件";
        if (importBtn) importBtn.disabled = true;
        if (clearBtn) clearBtn.disabled = true;
        if (resultEl) { resultEl.hidden = true; resultEl.textContent = ""; }
    }

    function showResult(ok, title, lines) {
        if (!resultEl) return;
        resultEl.textContent = "";
        resultEl.className = "sk-result " + (ok ? "is-ok" : "is-err");
        resultEl.hidden = false;
        resultEl.appendChild(el("div", "sk-result-title", title));
        (lines || []).forEach(function (t) {
            resultEl.appendChild(el("div", "sk-result-line" + (ok ? "" : " is-err"), t));
        });
    }

    /** 读取并解析文件；失败时给出明确原因 */
    function readFile(file) {
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function () {
            var text = String(reader.result || "");
            var data;
            try {
                data = JSON.parse(text);
            } catch (e) {
                setPending(null, "未选择文件");
                showResult(false, "导入失败", ["不是合法的 JSON 文件：" + (e && e.message ? e.message : "解析错误")]);
                toast("文件不是合法的 JSON", "error");
                return;
            }
            if (!data || (typeof data !== "object")) {
                setPending(null, "未选择文件");
                showResult(false, "导入失败", ["JSON 顶层需要是对象或数组"]);
                return;
            }
            var count = Array.isArray(data) ? data.length
                      : (Array.isArray(data.skills) ? data.skills.length : 1);
            setPending(data, file.name + "（识别到 " + count + " 条）");
            showResult(true, "已读取文件", ["共识别 " + count + " 条技能，点击「导入」写入"]);
        };
        reader.onerror = function () {
            showResult(false, "导入失败", ["文件读取失败，请重试或更换文件"]);
        };
        reader.readAsText(file, "utf-8");
    }

    function doImport() {
        if (!pending) return;
        if (!window.AegisAuth) {
            toast("鉴权组件未加载，请刷新页面重试", "error");
            return;
        }
        if (importBtn) importBtn.disabled = true;

        var token = window.AegisAuth.getToken ? window.AegisAuth.getToken() : "";
        fetch("/api/skills/import", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Aegis-Token": token },
            body: JSON.stringify({ payload: pending })
        }).then(function (resp) {
            return resp.json().catch(function () { return {}; });
        }).then(function (json) {
            var code = json && json.code;
            var data = (json && json.data) || {};

            if (code === 0) {
                var imported = data.imported || [];
                var skipped = data.skipped || [];
                var lines = ["成功导入 " + imported.length + " 个技能："
                             + imported.map(function (x) { return x.name; }).join("、")];
                if (skipped.length) {
                    lines.push("跳过 " + skipped.length + " 条：");
                    skipped.forEach(function (x) {
                        lines.push("· " + (x.name || ("第 " + x.index + " 条")) + " —— " + x.reason);
                    });
                }
                showResult(true, "导入完成", lines);
                toast("成功导入 " + imported.length + " 个技能", "success");
                clearPending();
                loadSkills();
            } else {
                var msg = (json && json.msg) || "导入失败";
                var sk = data.skipped || [];
                var errLines = [msg];
                sk.forEach(function (x) {
                    errLines.push("· " + (x.name || ("第 " + x.index + " 条")) + " —— " + x.reason);
                });
                showResult(false, "导入失败", errLines);
                toast(msg, "error");
                if (importBtn) importBtn.disabled = false;
            }
        }).catch(function (e) {
            showResult(false, "导入失败", ["请求失败：" + errText(e)]);
            toast("导入失败：" + errText(e), "error");
            if (importBtn) importBtn.disabled = false;
        });
    }

    if (fileInput) {
        fileInput.addEventListener("change", function () {
            var f = fileInput.files && fileInput.files[0];
            if (f) readFile(f);
        });
    }

    if (dropEl) {
        dropEl.addEventListener("click", function () { if (fileInput) fileInput.click(); });
        dropEl.addEventListener("dragover", function (e) {
            e.preventDefault();
            dropEl.classList.add("is-over");
        });
        dropEl.addEventListener("dragleave", function () {
            dropEl.classList.remove("is-over");
        });
        dropEl.addEventListener("drop", function (e) {
            e.preventDefault();
            dropEl.classList.remove("is-over");
            var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) readFile(f);
        });
    }

    if (importBtn) importBtn.addEventListener("click", doImport);
    if (clearBtn) clearBtn.addEventListener("click", clearPending);

    loadSkills();
})();
