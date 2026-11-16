(function () {
    "use strict";
    var auth = window.AegisAuth;
    var select = document.getElementById("awModelSelect");
    var hint = document.getElementById("awModelContext");
    if (!auth || !select) return;
    var view = null, running = false, pending = false, refreshId = 0;

    function sync() {
        select.disabled = running || pending || !view;
        window.dispatchEvent(new CustomEvent("aegis:model-selection-changed"));
    }
    function render(data) {
        view = data;
        var provider = (data.providers || []).filter(function (p) { return p.key === data.active; })[0];
        select.textContent = "";
        if (!provider) { view = null; hint.textContent = "请先配置模型"; sync(); return; }
        var models = (provider.candidate_models || []).slice();
        if (provider.model && models.indexOf(provider.model) < 0) models.unshift(provider.model);
        models.forEach(function (name) {
            var option = document.createElement("option");
            option.value = name;
            option.textContent = name;
            select.appendChild(option);
        });
        select.value = provider.model;
        select.title = "切换模型：" + provider.model;
        var windowSize = Number(provider.context_length_effective) || 32768;
        hint.textContent = provider.context_source === "inferred"
            ? "窗口未知（保守 " + windowSize.toLocaleString() + "）"
            : "窗口 " + windowSize.toLocaleString();
        hint.title = "总窗口 token 数；实际输入预算还会扣除输出、系统提示与工具开销";
        sync();
    }
    function refresh() {
        if (pending) return;
        var id = ++refreshId;
        return auth.get("/api/settings/providers").then(function (data) {
            if (id === refreshId) render(data);
        }).catch(function () { if (id === refreshId) hint.textContent = "模型信息加载失败，请打开设置"; });
    }
    select.addEventListener("change", function () {
        if (running || pending || !view) return;
        var provider = (view.providers || []).filter(function (p) { return p.key === view.active; })[0];
        if (!provider || !select.value || select.value === provider.model) return;
        var previous = view, model = select.value;
        pending = true;
        refreshId++;
        sync();
        return auth.put("/api/settings/providers", {active: provider.key,
            providers: [{key: provider.key, model: model, context_length: 0}]
        }).then(function (data) {
            if (data.ok === false || (data.key_failed || []).length) throw new Error("配置保存失败");
            render(data);
        }).catch(function () {
            render(previous);
            hint.textContent = "模型切换失败，已保留原模型";
        }).then(function () { pending = false; sync(); });
    });
    window.AegisModelPicker = {
        setBusy: function (busy) { running = !!busy; select.disabled = running || pending || !view; },
        saving: function () { return pending; }
    };
    window.addEventListener("aegis:settings-changed", refresh);
    window.addEventListener("focus", refresh);
    refresh();
})();
