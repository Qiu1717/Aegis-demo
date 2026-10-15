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
            if (s.resource_root) main.appendChild(el("div", "sk-desc", "资源目录：" + s.resource_root));
            if (s.package) {
                var details = el("details", "sk-desc");
                details.appendChild(el("summary", "", "查看完整技能指令"));
                var preview = el("pre", "", s.instructions || "");
                preview.style.whiteSpace = "pre-wrap";
                preview.style.maxHeight = "240px";
                preview.style.overflow = "auto";
                details.appendChild(preview);
                main.appendChild(details);
            }
            row.appendChild(main);

            // 操作列：固定宽度，不参与压缩
            var ctrl = el("div", "sk-ctrl");
            if (s.built_in) ctrl.appendChild(el("span", "sk-badge is-builtin", "内置"));
            if (s.package) ctrl.appendChild(el("span", "sk-badge", "资源包"));
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

    var pending = null;   // 待上传原始文件，由后端校验并完整保存
    var githubUrl = $("skGithubUrl");
    var githubBtn = $("btnImportGithub");
    var githubResult = $("skGithubResult");
    var fileBusy = false;

    function setPending(payload, label) {
        pending = payload;
        if (nameEl) nameEl.textContent = label || "未选择文件";
        if (importBtn) importBtn.disabled = !payload;
        if (clearBtn) clearBtn.disabled = !payload;
    }

    function clearPending(keepResult) {
        pending = null;
        if (fileInput) fileInput.value = "";
        if (nameEl) nameEl.textContent = "未选择文件";
        if (importBtn) importBtn.disabled = true;
        if (clearBtn) clearBtn.disabled = true;
        if (resultEl && keepResult !== true) { resultEl.hidden = true; resultEl.textContent = ""; }
    }

    function showResult(ok, title, lines, target) {
        target = target || resultEl;
        if (!target) return;
        target.textContent = "";
        target.className = "sk-result " + (ok ? "is-ok" : "is-err");
        target.hidden = false;
        target.appendChild(el("div", "sk-result-title", title));
        (lines || []).forEach(function (t) {
            target.appendChild(el("div", "sk-result-line" + (ok ? "" : " is-err"), t));
        });
    }

    /** 原文件上传：保留二进制资产与完整 SKILL.md，不在浏览器重写内容。 */
    function readFile(file) {
        if (!file || fileBusy) return;
        if (!(/\.json$|\.zip$|^SKILL\.md$/i).test(file.name) || file.size > 10 * 1024 * 1024) {
            setPending(null, "未选择文件");
            showResult(false, "文件不受支持", ["请选择不超过 10 MiB 的 JSON、SKILL.md 或 zip 技能包"]);
            return;
        }
        setPending(file, file.name);
        showResult(true, "待导入", ["点击导入后由服务端校验并保存；导入完成后需手动启用。"]);
    }

    function sendImport(url, body, headers, target, button, onSuccess) {
        if (!window.AegisAuth) {
            toast("鉴权组件未加载，请刷新页面重试", "error");
            return Promise.resolve();
        }
        var token = window.AegisAuth.getToken ? window.AegisAuth.getToken() : "";
        headers["X-Aegis-Token"] = token;
        if (button) button.disabled = true;
        showResult(true, "正在导入", ["正在下载或校验文件，请稍候。"], target);
        return fetch(url, {
            method: "POST", headers: headers, body: body
        }).then(function (resp) {
            return resp.json().then(function (json) {
                if (!resp.ok && json.code === 0) throw new Error("HTTP 请求失败，技能未注册");
                return json;
            });
        }).then(function (json) {
            var data = (json && json.data) || {};
            var imported = data.imported || [];
            var ok = json && json.code === 0 && data.ok === true && imported.length > 0;
            var lines = ok ? ["已保存并注册 " + imported.length + " 个技能："
                + imported.map(function (x) { return x.name; }).join("、"),
                "默认禁用，请检查指令后在上方列表启用；启用后用于新任务。"]
                : [(json && json.msg) || "导入失败，技能未注册"];
            (data.skipped || []).forEach(function (x) {
                lines.push("· " + (x.name || ("第 " + x.index + " 条")) + " —— " + x.reason);
            });
            if (data.resource_boundary) lines.push(data.resource_boundary);
            showResult(ok, ok ? "已导入，待启用" : "导入失败", lines, target);
            toast(ok ? "技能已注册，请手动启用" : lines[0], ok ? "success" : "error");
            if (ok) {
                if (onSuccess) onSuccess();
                loadSkills();
            }
        }).catch(function (e) {
            showResult(false, "导入失败", ["请求失败：" + errText(e)], target);
            toast("导入失败：" + errText(e), "error");
        }).finally(function () {
            if (button) button.disabled = button === importBtn ? !pending : false;
        });
    }

    function doImport() {
        if (!pending || fileBusy) return;
        var body = new FormData();
        body.append("file", pending);
        if ($("skOverwrite") && $("skOverwrite").checked) body.append("overwrite", "true");
        fileBusy = true;
        if (clearBtn) clearBtn.disabled = true;
        sendImport("/api/skills/import/file", body, {}, resultEl, importBtn, function () {
            clearPending(true);
            if ($("skOverwrite")) $("skOverwrite").checked = false;
        }).finally(function () {
            fileBusy = false;
            if (clearBtn) clearBtn.disabled = !pending;
        });
    }

    function importGithub() {
        var url = (githubUrl && githubUrl.value || "").trim();
        if (!url || (githubBtn && githubBtn.disabled)) return;
        sendImport("/api/skills/import/github", JSON.stringify({
            url: url, overwrite: !!($("skGithubOverwrite") && $("skGithubOverwrite").checked)
        }), { "Content-Type": "application/json" }, githubResult, githubBtn, function () {
            if ($("skGithubOverwrite")) $("skGithubOverwrite").checked = false;
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
        dropEl.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                if (fileInput) fileInput.click();
            }
        });
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
    if (githubBtn) githubBtn.addEventListener("click", importGithub);

    loadSkills();
})();
