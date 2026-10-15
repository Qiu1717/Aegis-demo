/* 工作台的真实附件、技能导入与持久外观设置。 */
(function () {
    "use strict";
    var AUTH = window.AegisAuth;
    if (!AUTH) return;
    function $(id) { return document.getElementById(id); }
    function notify(message) {
        var node = $("awToast");
        if (!node) return;
        node.textContent = message;
        node.classList.add("is-on");
        setTimeout(function () { node.classList.remove("is-on"); }, 3500);
    }
    function emit(name) { window.dispatchEvent(new CustomEvent(name)); }
    function upload(url, file) {
        var form = new FormData();
        form.append("file", file);
        return window.aegisFetchJSON(url, {method: "POST", body: form});
    }
    var pending = [], uploads = 0, generation = 0, busy = false;
    function renderAttachments() {
        var list = $("awAttachments");
        if (list) {
            list.textContent = "";
            pending.forEach(function (file) {
                var item = document.createElement("div");
                item.className = "aw-attachment";
                var label = document.createElement("span");
                label.textContent = file.name;
                item.appendChild(label);
                var remove = document.createElement("button");
                remove.type = "button";
                remove.textContent = "×";
                remove.setAttribute("aria-label", "移除 " + file.name);
                remove.addEventListener("click", function () {
                    remove.disabled = true;
                    AUTH.del("/api/agent/attachments/" + file.id).then(function () {
                        pending = pending.filter(function (f) { return f.id !== file.id; });
                        renderAttachments();
                    }).catch(function (e) { remove.disabled = false; notify(e.message); });
                });
                item.appendChild(remove);
                list.appendChild(item);
            });
        }
        var button = $("awBtnAttach");
        if (button) { button.disabled = busy || uploads > 0; button.title = uploads ? "正在上传文件…" : "添加 UTF-8 文本或代码文件（每个最多 2 MiB）"; }
        emit("aegis:attachments-changed");
    }
    window.AegisWorkspaceExtras = {
        files: function () { return pending.slice(); },
        uploading: function () { return uploads > 0; },
        setBusy: function (value) { busy = value; if ($("awBtnAttach")) $("awBtnAttach").disabled = busy || uploads > 0; },
        take: function () { var files = pending.slice(); pending = []; renderAttachments(); return files; },
        restore: function (files) { pending = files.slice(); renderAttachments(); },
        clear: function () {
            generation++;
            pending.forEach(function (file) { AUTH.del("/api/agent/attachments/" + file.id).catch(function () {}); });
            pending = []; renderAttachments();
        }
    };
    if ($("awBtnAttach")) $("awBtnAttach").addEventListener("click", function () { $("awAttachInput").click(); });
    if ($("awAttachInput")) $("awAttachInput").addEventListener("change", function () {
        var files = Array.prototype.slice.call(this.files || []), myGeneration = generation;
        this.value = "";
        if (pending.length + files.length > 8 || files.reduce(function (n, f) { return n + f.size; }, pending.reduce(function (n, f) { return n + f.size; }, 0)) > 8 * 1024 * 1024) {
            notify("每轮最多 8 个文件，总计不能超过 8 MiB"); return;
        }
        files.forEach(function (file) {
            uploads++; renderAttachments();
            upload("/api/agent/attachments", file).then(function (saved) {
                if (generation !== myGeneration) { AUTH.del("/api/agent/attachments/" + saved.id).catch(function () {}); return; }
                pending.push(saved);
            }).catch(function (e) { notify(file.name + "：" + e.message); }).then(function () { uploads--; renderAttachments(); });
        });
    });

    var preferences = {theme: "system", font_size: 14, sidebar_collapsed: false, task_details: true};
    var appearanceRevision = 0;
    function applyAppearance(values) {
        preferences = values;
        document.documentElement.setAttribute("data-theme", values.theme);
        document.documentElement.style.setProperty("--aw-user-font-size", values.font_size + "px");
        document.body.classList.toggle("aw-sidebar-collapsed", values.sidebar_collapsed);
        document.body.classList.toggle("aw-task-compact", !values.task_details);
        if ($("awMenuBtn")) $("awMenuBtn").setAttribute("aria-expanded", values.sidebar_collapsed ? "false" : "true");
    }
    function saveAppearance(updates) {
        var revision = ++appearanceRevision;
        return AUTH.put("/api/agent/appearance", updates).then(function (values) {
            if (revision === appearanceRevision) applyAppearance(values);
        });
    }
    window.addEventListener("aegis:sidebar-toggle", function () {
        saveAppearance({sidebar_collapsed: !preferences.sidebar_collapsed}).catch(function (e) { notify(e.message); });
    });
    AUTH.get("/api/agent/appearance").then(function (values) {
        if (!appearanceRevision) applyAppearance(values);
    }).catch(function (e) { notify("读取外观设置失败：" + e.message); });
    if ($("awBtnAppearance")) $("awBtnAppearance").addEventListener("click", function () {
        $("awTheme").value = preferences.theme;
        $("awFontSize").value = String(preferences.font_size);
        $("awSidebarCollapsed").checked = preferences.sidebar_collapsed;
        $("awTaskDetails").checked = preferences.task_details;
        $("awAppearanceModal").hidden = false;
    });
    function closeAppearance() { $("awAppearanceModal").hidden = true; }
    ["awAppearanceClose", "awAppearanceMask"].forEach(function (id) { if ($(id)) $(id).addEventListener("click", closeAppearance); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && $("awAppearanceModal")) closeAppearance(); });
    if ($("awAppearanceSave")) $("awAppearanceSave").addEventListener("click", function () {
        var button = this; button.disabled = true;
        saveAppearance({theme: $("awTheme").value, font_size: Number($("awFontSize").value), sidebar_collapsed: $("awSidebarCollapsed").checked, task_details: $("awTaskDetails").checked})
            .then(function () { closeAppearance(); notify("外观设置已保存"); })
            .catch(function (e) { notify(e.message); }).then(function () { button.disabled = false; });
    });

    var skillBusy = false;
    function importSkill(action) {
        if (skillBusy) return;
        skillBusy = true;
        var status = $("awSkillImportStatus");
        status.textContent = "正在导入…";
        ["awSkillImportFile", "awSkillImportGithub", "awSkillCreate"].forEach(function (id) { $(id).disabled = true; });
        action().then(function (result) {
            status.textContent = "导入成功：" + (result.total || 1) + " 个技能，请在列表中启用。";
            if (result.skipped && result.skipped.length) status.textContent += " 部分条目跳过，请检查技能包。";
            emit("aegis:skills-changed");
        }).catch(function (e) { status.textContent = "导入失败：" + e.message; })
            .then(function () { skillBusy = false; ["awSkillImportFile", "awSkillImportGithub", "awSkillCreate"].forEach(function (id) { $(id).disabled = false; }); });
    }
    if ($("awSkillImportFile")) $("awSkillImportFile").addEventListener("click", function () { $("awSkillFile").click(); });
    if ($("awSkillFile")) $("awSkillFile").addEventListener("change", function () {
        var file = this.files[0]; this.value = "";
        if (file) importSkill(function () { return upload("/api/skills/import/file", file); });
    });
    if ($("awSkillImportGithub")) $("awSkillImportGithub").addEventListener("click", function () {
        var url = $("awSkillGithub").value.trim();
        if (!url) { notify("请填写 GitHub 技能链接"); return; }
        importSkill(function () { return AUTH.post("/api/skills/import/github", {url: url}); });
    });
    if ($("awSkillCreate")) $("awSkillCreate").addEventListener("click", function () {
        importSkill(function () { return AUTH.post("/api/skills", {name: $("awSkillName").value.trim(), instructions: $("awSkillInstructions").value.trim(), enabled: false}); });
    });
})();
