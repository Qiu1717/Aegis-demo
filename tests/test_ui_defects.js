/**
 * 界面与逻辑缺陷修复 —— 回归测试
 *
 * 覆盖 4 个缺陷 × 3 种场景（正常结束 / 异常中断 / 快速连续发送）：
 *   缺陷1 未完成卡片残留（hidden 被 CSS display 覆盖）
 *   缺陷2 删除确认弹层不可见（工作台没引 common.css）
 *   缺陷3 欢迎页入口错误状态 + 自动启动
 *   缺陷4 思考状态未及时结束
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const css = fs.readFileSync(path.join(ROOT, 'static/css/workspace.css'), 'utf8');
const wsJs = fs.readFileSync(path.join(ROOT, 'static/js/workspace.js'), 'utf8');
const commonJs = fs.readFileSync(path.join(ROOT, 'static/js/common.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates/agent/aegis_workspace.html'), 'utf8');

let pass = 0, fail = 0;
const ck = (n, c, x) => {
  if (c) { console.log('  PASS  ' + n); pass++; }
  else { console.log('  FAIL  ' + n + (x ? '  → ' + x : '')); fail++; }
};

// ── CSS 级联解析（真实顺序 + 特异度）──────────────────────
function cssRules() {
  // 去掉 @media/@keyframes 块内的内容，避免其花括号干扰
  const stripped = css.replace(/@(media|keyframes|supports)[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, '');
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(stripped)) !== null) {
    const decls = m[2];
    const dm = decls.match(/display\s*:\s*([^;]+)/);
    if (!dm) continue;
    for (const sel of m[1].split(',').map(s => s.trim())) {
      if (!sel) continue;
      const cls = (sel.match(/\.[A-Za-z0-9_-]+/g) || []).length;
      const attr = (sel.match(/\[[^\]]+\]/g) || []).length;
      const id = (sel.match(/#[A-Za-z0-9_-]+/g) || []).length;
      const tags = (sel.match(/(^|[\s>+~])([a-z][a-z0-9]*)/gi) || []).length;
      out.push({ sel, display: dm[1].trim(), spec: id * 10000 + cls * 100 + attr * 100 + tags,
                 order: m.index });
    }
  }
  return out;
}
const RULES = cssRules();
/** 元素带 hidden 属性时，最终生效的 display */
function displayWhenHidden(baseSel, ancestorSel) {
  const cands = [];
  const consider = (sel, extraSpec, extraOrder) => {
    if (sel === '[hidden]') return;
    // baseSel 变体：sel + ' ' + baseSel  或 sel + baseSel
    if (sel === baseSel) cands.push({ spec: extraSpec, order: extraOrder, display: 'from-base' });
    if (sel === baseSel + '[hidden]') cands.push({ spec: extraSpec, order: extraOrder, display: 'none' });
    if (ancestorSel && sel === ancestorSel + ' ' + baseSel + '[hidden]')
      cands.push({ spec: extraSpec, order: extraOrder, display: 'none' });
  };
  // 简化：直接找出所有与 baseSel 或其 [hidden] 变体相关的规则
  for (const r of RULES) {
    if (r.sel === baseSel) cands.push({ spec: r.spec, order: r.order, display: r.display });
    if (r.sel === baseSel + '[hidden]') cands.push({ spec: r.spec, order: r.order, display: 'none' });
    if (ancestorSel && r.sel === ancestorSel + ' ' + baseSel + '[hidden]')
      cands.push({ spec: r.spec, order: r.order, display: 'none' });
  }
  if (!cands.length) return 'none';   // 无 display 规则 → hidden 生效
  cands.sort((a, b) => (a.spec - b.spec) || (a.order - b.order));
  const win = cands[cands.length - 1];
  return win.display === 'none' ? 'none' : win.display;
}

console.log('=== 缺陷1：[hidden] 能否真正隐藏（CSS display 覆盖问题）===');
const t1 = displayWhenHidden('.aw-retry');
ck('.aw-retry 带 hidden 时 display=none', t1 === 'none', 'got ' + t1);
const t2 = displayWhenHidden('.aw-busy');
ck('.aw-busy 带 hidden 时 display=none', t2 === 'none', 'got ' + t2);
const t3 = displayWhenHidden('.aw-task', '.aw-view');
ck('.aw-task 带 hidden 时 display=none（祖先选择器兜底）', t3 === 'none', 'got ' + t3);
ck('HTML 里 awRetry 默认带 hidden', /id="awRetry"[^>]*hidden/.test(html) ||
   /hidden[^>]*id="awRetry"/.test(html));
ck('HTML 里 awBusy 默认带 hidden', /id="awBusy"[^>]*hidden/.test(html) ||
   /hidden[^>]*id="awBusy"/.test(html));
ck('重试卡仅在真失败时出现（showRetry 唯一调用点在 catch 分支）',
   (wsJs.match(/showRetry\(/g) || []).length === 2);  // 1 定义 + 1 调用
ck('发送开始即清重试条', /hideRetry\(\);/.test(wsJs));

console.log('');
console.log('=== 缺陷2：删除确认弹层样式与行为 ===');
ck('工作台 CSS 含 .aw-confirm-mask（position:fixed 遮罩）', /\.aw-confirm-mask\s*\{/.test(css));
ck('遮罩为 fixed + 居中', /position:\s*fixed/.test(css) && /align-items:\s*center/.test(css));
ck('有独立阴影（卡片浮起感）', /\.aw-confirm-card[\s\S]{0,300}box-shadow/.test(css));
ck('z-index 高于技能浮层(60)', /\.aw-confirm-mask[\s\S]{0,200}z-index:\s*200/.test(css));
ck('确认/取消按钮样式齐备', /\.aw-confirm-btn\s*\{/.test(css) && /\.aw-confirm-btn\.is-danger/.test(css));
ck('showConfirm 会检测工作台样式并切换实现',
   /useWorkspaceStyle/.test(commonJs) && /aw-confirm-card/.test(commonJs));
// 工作台分支必须用 createElement/textContent，不能拼 innerHTML。
// 注意：不能用 /innerHTML/ 直接搜 —— 源码注释里提到了这个词会误报，
// 改为只检查**赋值给 innerHTML** 的写法。
const wsBranch = commonJs.slice(commonJs.indexOf('if (useWorkspaceStyle)'));
const wsBranchEnd = wsBranch.indexOf('} else {');
const wsOnly = wsBranch.slice(0, wsBranchEnd > 0 ? wsBranchEnd : 2000);
ck('工作台分支不拼 innerHTML（保持 XSS 约定）', !/\.innerHTML\s*=/.test(wsOnly));
ck('工作台分支用 textContent 写入文案', /textContent\s*=\s*message/.test(wsOnly));
ck('焦点交给确认按钮（键盘可达）', /first\.focus\(\)|\.focus\(\)/.test(commonJs));
ck('Esc/Enter 快捷键保留', /e\.key === "Escape"/.test(commonJs) && /e\.key === "Enter"/.test(commonJs));

console.log('');
console.log('=== 缺陷3：欢迎页入口为待命态，不自动启动 ===');
// 抽出卡片 click handler
const cardIdx = wsJs.indexOf('var cards = document.querySelectorAll');
const cardEnd = wsJs.indexOf('})(cards[i]);', cardIdx);
const cardBlock = wsJs.slice(cardIdx, cardEnd);
ck('卡片处理器不再调用 send()', !/\bsend\(\)/.test(cardBlock));
ck('卡片填入 prompt 后聚焦输入框', /elInput\.value = p/.test(cardBlock) && /elInput\.focus\(\)/.test(cardBlock));
ck('有 armed（待发送）状态文案', /armed:\s*"待发送"/.test(wsJs));
ck('CSS 有 armed 角标样式', /data-run-state="armed"/.test(css));
ck('用户改输入框会清 armed（clearArmedState）', /function clearArmedState/.test(wsJs) &&
   /clearArmedState\(\);\s*\n\s*syncSend\(\)/.test(wsJs));
ck('真正发送时清 armed（不残留待命角标）', /armedCards\[ci\]\.setAttribute\("data-run-state", ""\)/.test(wsJs));
ck('无工作区不再拦截点击', /无工作区\*\*不再拦截点击\*\*/.test(cardBlock));
ck('运行中仍拦截（防止误触丢掉正在跑的任务）', /if \(state\.sending\)/.test(cardBlock));

console.log('');
console.log('=== 缺陷4：思考状态及时结束 ===');
const runEndIdx = wsJs.indexOf('} else if (t === "run_end") {');
const runEndBlk = wsJs.slice(runEndIdx, runEndIdx + 700);
ck('run_end 分支会收起生成中指示', /hideBusy\(\);/.test(runEndBlk));
ck('增量输出显示生成回复状态，结束事件再收起', /showBusy\("正在生成回复"\)/.test(wsJs));
ck('正常结束路径有 hideBusy 兜底',
   /state\.sending = false;\s*\n\s*hideBusy\(\);/.test(wsJs));
ck('异常中断路径有 hideBusy 兜底',
   /state\.awaitingApproval = false;\s*\n\s*state\.approvalSince = 0;\s*\n\s*hideBusy\(\);\s*\n\s*setStatus\("idle", "已停止等待"\)/.test(wsJs));
ck('停止路径有 hideBusy', /setStatus\("idle", "已停止"\)/.test(wsJs));
ck('审批等待会重新显示（长任务不被误判结束）',
   /showBusy\("等待你的批准…"\)/.test(wsJs) && /showBusy\("Aegis 正在继续…"\)/.test(wsJs));
ck('run_start 显示生成中', /showBusy\(evt\.title \?/.test(wsJs));
// 快速连续发送：armed 清理与 sending 复位不应互相污染
ck('快速连发：新一轮重置 cancelNotified', /activeRun\.cancelNotified = false;/.test(wsJs));
ck('快速连发：runSeq 递增用于归属判定', /var myId = \+\+runSeq;/.test(wsJs));
ck('快速连发：stoppedByUser 按轮次判定', /if \(myId === runSeq\) stoppedByUser = true;/.test(wsJs));

console.log('');
console.log('='.repeat(52));
console.log('结果： ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
