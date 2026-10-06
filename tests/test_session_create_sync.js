/**
 * 侧边栏「新建对话」状态同步 —— 回归测试
 *
 * 问题：删除对话会实时刷新侧边栏，但新建对话后侧边栏不出现新项，
 *      导致连续新建多个对话处理不同工作时无法点击切换。
 *
 * 根因：resetNew() 只调 loadSessions()，而新会话是后端在 /api/agent/run 的
 *      _ensure_session 里「惰性」创建的 —— 点新建时后端根本没有会话存在。
 *
 * 修复：点「新建任务」即 POST /api/aichat/sessions 建会话，成功后乐观插入
 *      sessionsCache 顶部并重渲染（与删除的乐观更新对称）。
 */

// ── 最小 DOM 桩 ──────────────────────────────────────────
function makeEl(tag) {
  const n = {
    tagName: (tag || 'div').toUpperCase(),
    className: '', _text: '', children: [], hidden: false,
    title: '', type: '', attrs: {},
    get textContent() {
      return this.children.length
        ? this._text + this.children.map(c => c.textContent).join('')
        : this._text;
    },
    set textContent(v) { this._text = v == null ? '' : String(v); this.children = []; },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    }
  };
  return n;
}

const nodes = {};
['awCards','awSend','awInput','awBusy','awBusyText','awBusyStop','awStatus','awStatusText',
 'awConvList','awRetry','awToast','awMsgs','awTask','awSteps','awTools','awTaskEmpty','awWebToggle',
 'awPermSelect','awThinkSelect','awBtnWs','awWsChip','awModelChip','awMenuBtn','awOverlay','awSide',
 'awViewHome','awViewChat','awScroll','awComposer','awNavNew','awNavSkills','awNavSettings',
 'awBtnSettings','awBtnSkills','awArch','awArchBody','awArchCount','awRetryBtn','awRetryText'
].forEach(id => { nodes[id] = makeEl('div'); });

const cardEls = [];
['explore','build','review','fix'].forEach((kind, i) => {
  const c = makeEl('button');
  c.setAttribute('data-kind', kind);
  c.setAttribute('data-needs-ws', i === 0 ? '0' : '1');
  c._badge = makeEl('span');
  c.querySelector = sel => (sel === '.aw-card-state' ? c._badge : null);
  cardEls.push(c);
});
nodes.awCards.querySelectorAll = sel => (sel === '.aw-card' ? cardEls : []);

global.document = {
  getElementById: id => nodes[id] || null,
  createElement: makeEl,
  querySelectorAll: () => [],
  addEventListener: () => {}
};
global.window = {};

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const n = makeEl(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

// ── 服务端模拟 ───────────────────────────────────────────
let SERVER = [];          // 权威数据源
let nextId = 100;
let failCreate = false;
const AUTH = {
  _calls: [],
  get(u) { this._calls.push(['GET', u]); return Promise.resolve(JSON.parse(JSON.stringify(SERVER))); },
  post(u, b) {
    this._calls.push(['POST', u, b]);
    if (u === '/api/aichat/sessions') {
      if (failCreate) return Promise.reject(new Error('创建失败'));
      // 与后端一致：is_pinned=0，标题「新对话」
      const s = { session_id: nextId++, session_title: '新对话', is_pinned: 0 };
      SERVER.unshift(s);   // created_time 倒序 → 新的在前
      return Promise.resolve(Object.assign({}, s));
    }
    return Promise.resolve({});
  },
  put() { return Promise.resolve({}); },
  del(u) {
    const sid = Number(u.split('/').pop());
    SERVER = SERVER.filter(s => s.session_id !== sid);
    return Promise.resolve({ code: 0 });
  }
};

const state = { sessionId: null, sending: false, webEnabled: true,
  awaitingApproval: false, approvalSince: 0,
  hasWorkspace: true, lastPrompt: '', retryable: false, markStopped: null };

let sessionsCache = [], sessionsReqSeq = 0, deletingSessions = {};
const elConvList = $('awConvList');
const elCards = $('awCards'), elInput = $('awInput');

function sidOf(s) { return s.id != null ? s.id : s.session_id; }
/* 置顶判断：与修复后的实现一致 —— 后端返回 int 0/1，不能用 === true。
   （这是本轮实测发现并修掉的真实缺陷：int 1 === true 为 false，置顶在前端全失效。） */
function isPinnedOf(s) {
  if (!s) return false;
  const v = (s.is_pinned != null) ? s.is_pinned : s.isPinned;
  if (v === 0 || v === '0' || v === false || v === 'false') return false;
  return !!v;
}
function toast() {}

function renderSessions(list) {
  elConvList.textContent = '';
  list = list || [];
  const main = [], archived = [];
  list.forEach(s => {
    const sid = sidOf(s);
    if (sid == null) return;
    if (s.archived) archived.push(s); else main.push(s);
  });
  main.sort((a, b) => (isPinnedOf(b) ? 1 : 0) - (isPinnedOf(a) ? 1 : 0));
  if (!main.length) elConvList.appendChild(el('div', 'aw-side-empty', '还没有会话'));
  else main.forEach(s => {
    const row = el('div', 'aw-conv' + (String(sidOf(s)) === String(state.sessionId) ? ' is-active' : ''));
    row._sid = sidOf(s);
    elConvList.appendChild(row);
  });
  $('awArchCount').textContent = String(archived.length);
}

function loadSessions() {
  const mySeq = ++sessionsReqSeq;
  return AUTH.get('/api/aichat/sessions').then(res => {
    if (mySeq < sessionsReqSeq) return;
    sessionsCache = res || [];
    renderSessions(sessionsCache);
  }).catch(() => {
    // 修复后：失败不再清空侧边栏
  });
}

// ── 被测逻辑（与 workspace.js 一致）────────────────────────
function createSessionNow() {
  if (!AUTH) return Promise.resolve(null);
  return AUTH.post('/api/aichat/sessions', { title: '新对话' }).then(function (res) {
    var sid = sidOf(res || {});
    if (sid == null) throw new Error('创建会话失败');
    state.sessionId = sid;
    // 与后端一致：不置顶，靠 created_time 倒序排到前面
    sessionsCache = [{
      session_id: sid, session_title: '新对话', is_pinned: 0, archived: false
    }].concat(sessionsCache);
    renderSessions(sessionsCache);
    return loadSessions().then(() => sid);
  }).catch(function () {
    return loadSessions().then(() => null);
  });
}

function resetNew() {
  state.sessionId = null;
  if ($('awMsgs')) $('awMsgs').textContent = '';
  createSessionNow();
}

function deleteSession(sid) {
  const key = String(sid);
  if (deletingSessions[key]) return Promise.resolve();
  deletingSessions[key] = true;
  const snapshot = sessionsCache;
  sessionsCache = sessionsCache.filter(s => String(sidOf(s)) !== key);
  if (sessionsCache.length !== snapshot.length) renderSessions(sessionsCache);
  if (key === String(state.sessionId)) state.sessionId = null;
  return AUTH.del('/api/aichat/sessions/' + encodeURIComponent(sid))
    .then(() => loadSessions())
    .catch(() => { sessionsCache = snapshot; renderSessions(sessionsCache); })
    .then(() => { delete deletingSessions[key]; });
}

// ── 测试 ────────────────────────────────────────────────
let pass = 0, fail = 0;
const ck = (n, c, x) => { if (c) { console.log('  PASS  ' + n); pass++; }
                            else { console.log('  FAIL  ' + n + (x ? '  → ' + x : '')); fail++; } };
const rows = () => elConvList.children.filter(c => c.className !== 'aw-side-empty');
const firstSid = () => rows()[0] ? rows()[0]._sid : null;
const isActiveTop = () => rows()[0] ? rows()[0].className.includes('is-active') : false;

(async function () {
  console.log('=== 场景1：首次进入（空列表）新建对话 ===');
  SERVER = []; nextId = 100; failCreate = false; deletingSessions = {}; sessionsReqSeq = 0;
  state.sessionId = null;
  renderSessions([]);
  ck('初始 0 条', rows().length === 0);

  await resetNew();
  ck('★新建后侧边栏立即出现 1 项', rows().length === 1, 'got ' + rows().length);
  ck('★新建项被高亮（is-active）', isActiveTop());
  ck('★state.sessionId 已指向新会话', state.sessionId === SERVER[0].session_id);
  ck('后端已落库', SERVER.length === 1);
  ck('乐观插入值与后端一致（is_pinned=0）', sessionsCache[0].is_pinned === 0,
     'got ' + sessionsCache[0].is_pinned);

  console.log('');
  console.log('=== 场景2：连续新建多个对话，每个都可点击切换 ===');
  await resetNew();   // 第2个
  ck('第2次新建后有 2 项', rows().length === 2, 'got ' + rows().length);
  ck('第2次新建：新项在顶部且高亮', isActiveTop());
  const sid2 = state.sessionId;

  await resetNew();   // 第3个
  ck('第3次新建后有 3 项', rows().length === 3, 'got ' + rows().length);
  ck('第3次新建：新项在顶部且高亮', isActiveTop());
  const sid3 = state.sessionId;
  ck('三个会话 id 互不相同', new Set(SERVER.map(s => s.session_id)).size === 3);

  // 逐个点击切换（模拟点不同行）
  const order = rows().map(r => r._sid);
  ck('列表顺序：最新在前', order[0] === sid3 && order[2] === sid2 - 1, order.join(','));

  // 手动切换选中：点第2项
  state.sessionId = order[1];
  renderSessions(sessionsCache);
  ck('可切换到中间那项并高亮', rows()[1].className.includes('is-active') && !isActiveTop());
  // 切回第一项
  state.sessionId = order[0];
  renderSessions(sessionsCache);
  ck('可切回最新那项', isActiveTop());

  console.log('');
  console.log('=== 场景3：新建与删除行为对称 ===');
  const before = rows().length;
  const victim = state.sessionId;
  await deleteSession(victim);
  ck('删除当前会话后列表立即减少', rows().length === before - 1, 'got ' + rows().length);
  ck('删除后 state.sessionId 已清空', state.sessionId === null);
  // 删除后立即新建
  await resetNew();
  ck('★删除后立即新建，新项立刻出现', rows().length === before, 'got ' + rows().length);
  ck('★删除后新建，新项高亮', isActiveTop());
  ck('前后端条数一致', rows().length === SERVER.length,
     'ui=' + rows().length + ' server=' + SERVER.length);

  console.log('');
  console.log('=== 场景4：已有会话时新建，顺序不乱 ===');
  // 用 int 0/1（与后端 SQLite 实际返回一致）
  SERVER = [
    { session_id: 1, session_title: '旧A', is_pinned: 0 },
    { session_id: 2, session_title: '旧B', is_pinned: 1 }   // 置顶
  ];
  nextId = 200;
  await loadSessions();
  ck('初始 2 项', rows().length === 2, 'got ' + rows().length);
  ck('★置顶项（int 1）排第一', firstSid() === 2, 'got ' + firstSid());
  await resetNew();
  ck('新建后 3 项', rows().length === 3, 'got ' + rows().length);
  const newId = state.sessionId;
  ck('★置顶项仍在第一位（排序规则未被破坏）', firstSid() === 2, 'got ' + firstSid());
  ck('★新项紧随置顶项之后', rows()[1]._sid === newId, 'got ' + rows()[1]._sid);
  ck('★新项不置顶（is_pinned=0）', sessionsCache.find(s => s.session_id === newId).is_pinned === 0);
  ck('前后端条数一致', rows().length === SERVER.length,
     'ui=' + rows().length + ' server=' + SERVER.length);

  console.log('');
  console.log('=== 场景4b：置顶判断的类型兼容（本轮修掉的真实缺陷）===');
  ck('int 1 判为置顶', isPinnedOf({ is_pinned: 1 }) === true);
  ck('int 0 判为未置顶', isPinnedOf({ is_pinned: 0 }) === false);
  ck('bool true 判为置顶', isPinnedOf({ is_pinned: true }) === true);
  ck('bool false 判为未置顶', isPinnedOf({ is_pinned: false }) === false);
  ck('字符串 "0" 判为未置顶', isPinnedOf({ is_pinned: '0' }) === false);
  ck('字符串 "false" 判为未置顶', isPinnedOf({ is_pinned: 'false' }) === false);
  ck('驼峰 isPinned 兼容', isPinnedOf({ isPinned: 1 }) === true);
  ck('缺失字段判为未置顶', isPinnedOf({}) === false);
  ck('★置顶项带 is-pinned 样式标记', (function () {
    // 复刻 buildConvRow 的类名拼接
    const s = { is_pinned: 1, session_id: 999 };
    return ('aw-conv' + (isPinnedOf(s) ? ' is-pinned' : '')).includes('is-pinned');
  })());

  console.log('');
  console.log('=== 场景5：创建失败应可回退，不阻断用户 ===');
  SERVER = [{ session_id: 1, session_title: '旧A', is_pinned: 0 }];
  nextId = 300; failCreate = true;
  state.sessionId = null;
  await loadSessions();
  const n0 = rows().length;
  await resetNew();
  ck('创建失败不抛异常', true);
  ck('★失败后侧边栏条目未减少（不清空）', rows().length === n0, 'got ' + rows().length);
  ck('★失败后 state.sessionId 为 null（发送时后端补建）', state.sessionId === null);

  console.log('');
  console.log('=== 场景6：快速连续新建（3 次点击）===');
  SERVER = []; nextId = 400; failCreate = false; deletingSessions = {};
  sessionsReqSeq = 0; state.sessionId = null;
  renderSessions([]);
  await Promise.all([resetNew(), resetNew(), resetNew()]);
  ck('三次并发新建后服务端 3 条', SERVER.length === 3, 'got ' + SERVER.length);
  ck('三次并发新建后侧边栏 3 条', rows().length === 3, 'got ' + rows().length);
  ck('前后端一致', rows().length === SERVER.length);
  ck('仅一项高亮', rows().filter(r => r.className.includes('is-active')).length === 1,
     'got ' + rows().filter(r => r.className.includes('is-active')).length);

  console.log('');
  console.log('='.repeat(52));
  console.log('结果： ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
