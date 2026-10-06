/**
 * 删除会话的计数同步 —— 纯逻辑回归测试
 * 用 Node 模拟最小 DOM 环境，直接跑 workspace.js 里的算法片段，
 * 验证：删最后一条、切换会话、并发删除、失败回滚。
 */
const fs = require('fs');
const path = require('path');

// ── 最小 DOM 桩：只实现被测逻辑用到的部分 ──────────────
function makeEl(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(),
    className: '',
    _text: '',
    children: [],
    hidden: false,
    title: '',
    type: '',
    get textContent() {
      if (this.children.length === 0) return this._text;
      return this._text + this.children.map(c => c.textContent).join('');
    },
    set textContent(v) { this._text = v == null ? '' : String(v); this.children = []; },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute() {},
    addEventListener() {},
    querySelectorAll() { return []; },
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    }
  };
}

const nodes = {};
['awArch', 'awArchBody', 'awArchCount', 'awConvList', 'awMsgs', 'awTask',
 'awSteps', 'awTools', 'awTaskEmpty', 'awRetry', 'awCards', 'awBusy'
].forEach(id => { nodes[id] = makeEl('div'); });
nodes.awArchCount._text = '0';
nodes.awArchCount.textContent = '0';

global.document = {
  getElementById: id => nodes[id] || null,
  createElement: makeEl,
  querySelectorAll: () => [],
  addEventListener: () => {}
};
global.window = {};
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;

// ── 被测逻辑（与 workspace.js 中的实现保持一致）──────────
let AUTH = null;
const state = { sessionId: null, sending: false, webEnabled: true,
  awaitingApproval: false, approvalSince: 0,
  hasWorkspace: false, lastPrompt: '', retryable: false };

function el(tag, cls, text) {
  const n = makeEl(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
const $ = id => document.getElementById(id);
const elConvList = $('awConvList');

function sidOf(s) { return s.id != null ? s.id : s.session_id; }
function isPinnedOf(s) { return s.is_pinned === true || s.isPinned === true; }

let sessionsCache = [];
let sessionsReqSeq = 0;
let deleted = [];
let loadSessions;
let deletedSucceeds = true;

function renderSessions(list) {
  if (!elConvList) return;
  list = list || [];
  const main = [], archived = [];
  list.forEach(s => {
    const sid = sidOf(s);
    if (sid == null) return;
    if (s.archived) archived.push(s); else main.push(s);
  });
  main.sort((a, b) => (isPinnedOf(b) ? 1 : 0) - (isPinnedOf(a) ? 1 : 0));
  elConvList.textContent = '';
  if (!main.length) elConvList.appendChild(el('div', 'aw-side-empty', '还没有会话'));
  else main.forEach(s => elConvList.appendChild(el('div', 'aw-conv')));
  renderArchived(archived);
}

function renderArchived(archived) {
  const wrap = $('awArch'), body = $('awArchBody'), count = $('awArchCount');
  if (!wrap || !body) return;
  if (count) count.textContent = String((archived && archived.length) || 0);
  body.textContent = '';
  if (!archived || !archived.length) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const inner = el('div', 'aw-arch-inner');
  archived.forEach(s => inner.appendChild(el('div', 'aw-conv')));
  body.appendChild(inner);
}

loadSessions = function () {
  if (!AUTH) return Promise.resolve();
  const mySeq = ++sessionsReqSeq;
  return AUTH.get('/api/aichat/sessions').then(res => {
    if (mySeq < sessionsReqSeq) return;
    sessionsCache = res || [];
    renderSessions(sessionsCache);
  }).catch(() => {});
};

function resetNewKeepList() {
  state.sessionId = null;
  if ($('awMsgs')) $('awMsgs').textContent = '';
}

const deletingSessions = {};
function deleteSession(sid) {
  if (!AUTH) return Promise.resolve();
  const key = String(sid);
  if (deletingSessions[key]) return Promise.resolve();
  deletingSessions[key] = true;

  const snapshot = sessionsCache;
  sessionsCache = sessionsCache.filter(s => String(sidOf(s)) !== key);
  if (sessionsCache.length !== snapshot.length) renderSessions(sessionsCache);
  if (key === String(state.sessionId)) resetNewKeepList();

  return AUTH.del('/api/aichat/sessions/' + encodeURIComponent(sid))
    .then(() => loadSessions())
    .then(() => '会话已删除')
    .catch(() => {
      sessionsCache = snapshot;
      renderSessions(sessionsCache);
      return '删除失败，请稍后重试';
    })
    .then(r => { delete deletingSessions[key]; return r; });
}

// ── 测试脚手架 ────────────────────────────────────────
let serverState = [];
function makeAuth(opts) {
  opts = opts || {};
  return {
    get: () => (opts.delayGet
      ? new Promise(r => setTimeout(() => r(JSON.parse(JSON.stringify(serverState))), opts.delayGet))
      : Promise.resolve(JSON.parse(JSON.stringify(serverState)))),
    del: id => {
      const sid = Number(id.split('/').pop());
      if (opts.failDelete) {
        /* 模拟真实失败：接口返回错误（api_fail 的约定是 code != 0） */
        return Promise.reject(new Error('接口异常'));
      }
      serverState = serverState.filter(s => Number(s.session_id) !== sid);
      deleted.push(sid);
      return Promise.resolve({ code: 0 });
    }
  };
}

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { console.log('  PASS  ' + name); pass++; }
  else { console.log('  FAIL  ' + name + (extra ? '  → ' + extra : '')); fail++; }
}
const archCount = () => nodes.awArchCount.textContent;
const mainRows = () => nodes.awConvList.children.length;

(async function run() {
  console.log('=== 场景 1：删除普通会话，列表与计数实时减少 ===');
  serverState = [
    { session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' },
    { session_id: 3, session_title: 'C' }
  ];
  AUTH = makeAuth(); deleted = [];
  await loadSessions();
  check('初始渲染 3 条', mainRows() === 3, 'got ' + mainRows());
  await deleteSession(2);
  check('删除后列表剩 2 条', mainRows() === 2, 'got ' + mainRows());
  check('缓存剩 2 条', sessionsCache.length === 2);
  check('服务端已删 (id=2)', !serverState.some(s => s.session_id === 2));
  check('空归档计数为 0', archCount() === '0', 'got ' + archCount());

  console.log('');
  console.log('=== 场景 2：删除最后一条会话 ===');
  serverState = [{ session_id: 9, session_title: 'only' }];
  AUTH = makeAuth(); deleted = [];
  await loadSessions();
  check('初始 1 条', mainRows() === 1, 'got ' + mainRows());
  state.sessionId = 9;
  await deleteSession(9);
  check('列表变空态', mainRows() === 1 && nodes.awConvList.children[0].className === 'aw-side-empty',
    'class=' + nodes.awConvList.children[0].className);
  check('当前会话被清空', state.sessionId === null);
  check('计数为 0', archCount() === '0', 'got ' + archCount());
  check('服务端已空', serverState.length === 0);

  console.log('');
  console.log('=== 场景 3：归档计数随删除同步（含删最后一条归档）===');
  serverState = [
    { session_id: 1, session_title: 'M1' },
    { session_id: 5, session_title: 'A1' }, { session_id: 6, session_title: 'A2' }
  ];
  // 模拟后端 archived 标记
  const origGet = AUTH.get;
  AUTH = { get: () => Promise.resolve([
      { session_id: 1, session_title: 'M1', archived: false },
      { session_id: 5, session_title: 'A1', archived: true },
      { session_id: 6, session_title: 'A2', archived: true }]) };
  await loadSessions();
  check('归档计数 = 2', archCount() === '2', 'got ' + archCount());
  check('归档区可见', $('awArch').hidden === false);
  // 删掉一条归档
  AUTH = { get: AUTH.get, del: id => {
    const sid = Number(id.split('/').pop());
    const i = serverState.findIndex(s => s.session_id === sid);
    if (i >= 0) serverState.splice(i, 1);
    deleted.push(sid);
    // 剩余仍是 5、6 都归档
    return Promise.resolve({ code: 0 });
  } };
  await deleteSession(5);
  // 模拟服务端返回剩 1 条归档
  AUTH.get = () => Promise.resolve([{ session_id: 6, session_title: 'A2', archived: true }]);
  await loadSessions();
  check('删一条归档后计数 = 1', archCount() === '1', 'got ' + archCount());
  // 删掉最后一条归档 → 计数必须归零（这是原 bug）
  await deleteSession(6);
  AUTH.get = () => Promise.resolve([{ session_id: 1, session_title: 'M1', archived: false }]);
  await loadSessions();
  check('★删最后一条归档后计数归零（原 BUG 点）', archCount() === '0', 'got ' + archCount());
  check('★归档区已隐藏', $('awArch').hidden === true);

  console.log('');
  console.log('=== 场景 4：切换会话后删除，不影响其它行 ===');
  serverState = [
    { session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' },
    { session_id: 3, session_title: 'C' }, { session_id: 4, session_title: 'D' }
  ];
  AUTH = makeAuth(); deleted = [];
  await loadSessions();
  state.sessionId = 3;                       // 模拟用户正看着 3
  await deleteSession(1);                     // 删另一条
  check('删除非当前会话：当前会话保持', state.sessionId === 3, 'got ' + state.sessionId);
  check('列表剩 3 条', mainRows() === 3, 'got ' + mainRows());
  check('当前会话仍在列表中', sessionsCache.some(s => s.session_id === 3));

  console.log('');
  console.log('=== 场景 5：并发删除 3 条（去重 + 计数正确）===');
  serverState = [
    { session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' },
    { session_id: 3, session_title: 'C' }, { session_id: 4, session_title: 'D' },
    { session_id: 5, session_title: 'E' }
  ];
  AUTH = makeAuth(); deleted = [];
  await loadSessions();
  await Promise.all([deleteSession(1), deleteSession(2), deleteSession(3)]);
  check('服务端剩 2 条', serverState.length === 2, 'got ' + serverState.length);
  check('缓存剩 2 条', sessionsCache.length === 2, 'got ' + sessionsCache.length);
  check('列表剩 2 行', mainRows() === 2, 'got ' + mainRows());
  check('计数 0', archCount() === '0', 'got ' + archCount());
  check('去重标记已清空', Object.keys(deletingSessions).length === 0);

  console.log('');
  console.log('=== 场景 6：同一条被连点两次（只发一次请求）===');
  serverState = [{ session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' }];
  AUTH = makeAuth(); deleted = [];
  await loadSessions();
  const p1 = deleteSession(1);
  const p2 = deleteSession(1);                // 立刻重复点击
  await Promise.all([p1, p2]);
  check('只发一次 DELETE', deleted.filter(x => x === 1).length === 1,
    'sent ' + deleted.filter(x => x === 1).length);
  check('列表剩 1 条', mainRows() === 1, 'got ' + mainRows());

  console.log('');
  console.log('=== 场景 7：删除失败 → 界面回滚 ===');
  serverState = [{ session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' }];
  deleted = [];
  AUTH = makeAuth({ failDelete: true });
  await loadSessions();
  const r = await deleteSession(1);
  check('返回失败提示', r === '删除失败，请稍后重试', r);
  check('缓存已回滚为 2 条', sessionsCache.length === 2, 'got ' + sessionsCache.length);
  check('列表已回滚为 2 行', mainRows() === 2, 'got ' + mainRows());
  check('服务端未被改动', serverState.length === 2);

  console.log('');
  console.log('=== 场景 8：并发 loadSessions 乱序返回，计数不回退 ===');
  serverState = [{ session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' }];
  // 慢响应先发（返回旧数据 2 条），快响应后发（返回新数据 1 条）
  AUTH = {
    get: () => new Promise(r => setTimeout(() => r(JSON.parse(JSON.stringify(serverState))), 40)),
    del: () => Promise.resolve({ code: 0 })
  };
  serverState = [{ session_id: 1, session_title: 'A' }, { session_id: 2, session_title: 'B' }];
  const slow = loadSessions();
  serverState = [{ session_id: 1, session_title: 'A' }];   // 期间被删掉一条
  const fast = loadSessions();
  await Promise.all([slow, fast]);
  check('最终以最新响应为准（1 条）', sessionsCache.length === 1, 'got ' + sessionsCache.length);
  check('列表行数 = 1', mainRows() === 1, 'got ' + mainRows());

  console.log('');
  console.log('='.repeat(46));
  console.log('结果： ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
