/**
 * AI规则控制模块 - 前端逻辑
 * 负责规则表格渲染、行选中高亮、新建/编辑/删除/保存/取消
 */

const MAX_ROWS = 15;        // 表格最大显示行数
let rules = [];              // 从后端加载的规则数据
let tableData = [];          // 表格当前数据（含编辑态）
let selectedIndex = -1;      // 当前选中行索引
let editingIndex = -1;       // 当前编辑行索引

document.addEventListener("DOMContentLoaded", async () => {
    // 绑定按钮事件
    document.getElementById("btnNew").addEventListener("click", addNewRule);
    document.getElementById("btnEdit").addEventListener("click", editSelected);
    document.getElementById("btnDelete").addEventListener("click", deleteSelected);
    document.getElementById("btnSave").addEventListener("click", saveRules);
    document.getElementById("btnCancel").addEventListener("click", cancelEdit);

    await loadRules();
});

// ── 加载规则数据 ──────────────────────────────────────────
async function loadRules() {
    try {
        rules = await API.get("/api/aicontrol/rules");
        tableData = (rules || []).map(r => ({
            id: r.id,
            rule_content: r.rule_content || "",
            rule_status: parseInt(r.rule_status) || 1,
            sort_no: parseInt(r.sort_no) || 0,
            updated_time: r.updated_time || "",
            isNew: false,
        }));
        selectedIndex = -1;
        editingIndex = -1;
        renderTable();
    } catch (e) {
        showToast("加载规则失败: " + e.message, "error");
    }
}

// ── 渲染表格 ─────────────────────────────────────────────
function renderTable() {
    const tbody = document.getElementById("ruleTableBody");
    tbody.innerHTML = "";

    // 填充有效规则行
    tableData.forEach((rule, idx) => {
        const tr = createRow(rule, idx, false);
        tbody.appendChild(tr);
    });

    // 填充空白行（至 MAX_ROWS）
    const emptyCount = MAX_ROWS - tableData.length;
    for (let i = 0; i < Math.max(0, emptyCount); i++) {
        const tr = document.createElement("tr");
        tr.className = "empty-row";
        const colspan = 5;
        tr.innerHTML = `<td colspan="${colspan}">&nbsp;</td>`;
        tbody.appendChild(tr);
    }
}

// ── 创建表格行 ─────────────────────────────────────────────
function createRow(rule, idx, isNew) {
    const tr = document.createElement("tr");
    tr.dataset.index = idx;
    if (idx === selectedIndex) tr.classList.add("selected");

    const statusBadge = rule.rule_status === 1
        ? '<span class="status-badge status-on">启用</span>'
        : '<span class="status-badge status-off">禁用</span>';

    const isEditing = idx === editingIndex;
    const inputDisabled = !isEditing;

    tr.innerHTML = `
        <td class="col-seq">${idx + 1}</td>
        <td>
            <input class="rule-input" type="text"
                   value="${escapeAttr(rule.rule_content)}"
                   placeholder="请输入规则关键词"
                   ${inputDisabled ? "disabled" : ""}
                   data-field="rule_content" />
        </td>
        <td class="col-status">${statusBadge}</td>
        <td class="col-sort">
            <input class="rule-input" type="number" min="0"
                   value="${rule.sort_no || idx + 1}"
                   style="width:50px;text-align:center;"
                   ${inputDisabled ? "disabled" : ""}
                   data-field="sort_no" />
        </td>
        <td class="col-time">${rule.updated_time ? formatFullTime(rule.updated_time) : "—"}</td>
    `;

    // 行点击选中
    tr.addEventListener("click", () => {
        selectRow(idx);
    });

    // 编辑态：输入框点击不触发行选中冒泡
    tr.querySelectorAll("input").forEach(input => {
        input.addEventListener("click", (e) => e.stopPropagation());
        input.addEventListener("input", (e) => {
            const field = e.target.dataset.field;
            tableData[idx][field] = e.target.value;
        });
    });

    return tr;
}

// ── 选中行 ────────────────────────────────────────────────
function selectRow(idx) {
    // 取消之前选中与编辑
    if (editingIndex !== -1 && editingIndex !== idx) {
        // 提交当前编辑行内容到 tableData
        commitEdit();
    }

    selectedIndex = idx;
    editingIndex = -1;
    renderTable();
}

// ── 提交编辑内容 ──────────────────────────────────────────
function commitEdit() {
    if (editingIndex === -1) return;
    const row = document.querySelector(`tr[data-index="${editingIndex}"]`);
    if (row) {
        const contentInput = row.querySelector('[data-field="rule_content"]');
        const sortInput = row.querySelector('[data-field="sort_no"]');
        if (contentInput) tableData[editingIndex].rule_content = contentInput.value;
        if (sortInput) tableData[editingIndex].sort_no = parseInt(sortInput.value) || 0;
    }
    editingIndex = -1;
}

// ── 新建规则 ──────────────────────────────────────────────
function addNewRule() {
    // 先提交当前编辑
    commitEdit();

    // 表格固定 15 行，超出后不再新增（PRD 4 / PDD 4.2.2 要求）
    if (tableData.length >= MAX_ROWS) {
        showToast(`最多只能配置 ${MAX_ROWS} 条规则`, "error");
        return;
    }

    const newRule = {
        id: null,
        rule_content: "",
        rule_status: 1,
        sort_no: tableData.length + 1,
        updated_time: "",
        isNew: true,
    };
    tableData.push(newRule);
    selectedIndex = tableData.length - 1;
    editingIndex = tableData.length - 1;
    renderTable();

    // 聚焦到新行输入框
    setTimeout(() => {
        const row = document.querySelector(`tr[data-index="${selectedIndex}"]`);
        if (row) {
            const input = row.querySelector('[data-field="rule_content"]');
            if (input) input.focus();
        }
    }, 50);

    showToast("已添加新规则行，请输入内容", "info");
}

// ── 编辑选中行 ─────────────────────────────────────────────
function editSelected() {
    if (selectedIndex === -1) {
        showToast("请先选择一条规则", "info");
        return;
    }
    commitEdit();
    editingIndex = selectedIndex;
    renderTable();

    setTimeout(() => {
        const row = document.querySelector(`tr[data-index="${selectedIndex}"]`);
        if (row) {
            const input = row.querySelector('[data-field="rule_content"]');
            if (input) input.focus();
        }
    }, 50);
}

// ── 删除选中行 ─────────────────────────────────────────────
async function deleteSelected() {
    if (selectedIndex === -1) {
        showToast("请先选择一条规则", "info");
        return;
    }

    const rule = tableData[selectedIndex];
    const ok = await showConfirm(
        `确定删除规则"${rule.rule_content || '(空)'}"吗？`,
        "删除规则"
    );
    if (!ok) return;

    // 从表格数据中移除
    tableData.splice(selectedIndex, 1);
    selectedIndex = -1;
    editingIndex = -1;
    renderTable();
    showToast("已删除（点击保存后生效）", "info");
}

// ── 保存全部规则 ──────────────────────────────────────────
async function saveRules() {
    // 先提交当前编辑
    commitEdit();

    // 过滤掉空内容规则
    const validRules = tableData.filter(r => r.rule_content.trim());
    if (validRules.length !== tableData.length) {
        const ok = await showConfirm(
            "存在内容为空的规则行，保存时将自动过滤。是否继续？",
            "提示"
        );
        if (!ok) return;
    }

    try {
        const payload = validRules.map((r, i) => ({
            rule_content: r.rule_content.trim(),
            rule_status: r.rule_status,
            sort_no: r.sort_no || (i + 1),
        }));
        await API.post("/api/aicontrol/rules", { rules: payload });
        await loadRules();
        showToast("规则保存成功", "success");
    } catch (e) {
        showToast("保存失败: " + e.message, "error");
    }
}

// ── 取消编辑，还原数据 ────────────────────────────────────
async function cancelEdit() {
    await loadRules();
    showToast("已还原全部修改", "info");
}

// ── HTML 转义 ─────────────────────────────────────────────
function escapeAttr(text) {
    if (!text) return "";
    // &amp; 必须最先处理，否则会把后续转义出来的实体再转义一次
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}
function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
}
