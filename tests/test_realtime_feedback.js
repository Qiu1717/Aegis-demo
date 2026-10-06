/**
 * 实时反馈与请求控制 —— 状态机回归测试
 *
 * 覆盖用户报告的四个缺陷对应的前端逻辑：
 *   1. 删除会话未及时生效（含失败提示）
 *   2. 停止按钮真正取消后端（必须发 /api/agent/cancel）
 *   3. 停止后卡片状态从「生成中」切「已停止」
 *   4. 停止后能继续对话（输入框可用、取消标记清理）
 *
 * 零依赖，直接跑 node。
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
['awCards','awSend','awInput','awBusy','awBusyText','awBusyStop','awStatus',
 'awStatusText','awConvList','awRetry','awToast','awMsgs','awTask',
 'awSteps','awTools','awTaskEmpty','awWebToggle','awPermSelect','awThinkSelect',
 'awBtnWs','awWsChip','awModelChip','awMenuBtn','awOverlay','awSide','awViewHome',
 'awViewChat','awScroll','awForm','awComposer','awNavNew','awNavSkills','awNavSettings',
 'awBtnSettings','awBtnSkills','awArch','awArchBody','awArchCount','awRetryBtn','awRetryText'
].forEach(id => { nodes[id] = makeEl('div'); });

// 卡片：4 张，带 badge 子节点
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

// ── 被测逻辑（与 workspace.js 保持一致）────────────────────
const AUTH = {
  _calls: [],
  _cancelCalls: 0,
  _delFail: false,
  /* 服务端会话表（模拟后端真实状态）：删除成功后 GET 必须返回剩余项，
     否则会误判成「删除把数据都清空了」。 */
  _server: [],
  get(u) {
    this._calls.push(['GET', u]);
    return Promise.resolve(JSON.parse(JSON.stringify(this._server)));
  },
  post(u, b) {
    this._calls.push(['POST', u, b]);
    if (u === '/api/agent/cancel') this._cancelCalls++;
    return Promise.resolve({ code: 0 });
  },
  put() { return Promise.resolve({}); },
  del(u) {
    this._calls.push(['DELETE', u]);
    if (this._delFail) return Promise.reject(new Error('服务端错误'));
    const sid = Number(u.split('/').pop());
    this._server = this._server.filter(s => Number(s.id) !== sid);
    return Promise.resolve({ code: 0 });
  }
};

const state = { sessionId: null, sending: false, webEnabled: true,
  awaitingApproval: false, approvalSince: 0,
  hasWorkspace: true, lastPrompt: '', retryable: false, markStopped: null };

let lastRunState = "";
const RUN_STATE_TEXT = { running:'生成中', stopped:'已停止', done:'已完成', failed:'失败' };
const elCards = $('awCards'), elSend = $('awSend'), elInput = $('awInput');
const elBusy = $('awBusy'), elStatusText = $('awStatusText'), elToast = $('awToast');

let sessionsCache = [], sessionsReqSeq = 0, deletingSessions = {};
const runSeq = { v: 0 };
const activeRun = { id:0, controller:null, watchdog:null, stallTimer:null, taskId:"", cancelNotified:false };
let toastMsg = "";

function toast(m) { toastMsg = m; }
function setStatus(s, t) { if (elStatusText) elStatusText.textContent = t; }
function hideBusy() { if (elBusy) elBusy.hidden = true; }
function showRetry(p, r) { state.lastPrompt = p||""; state.retryable = !!p; }
function hideRetry() { state.retryable = false; }
function setRunState(s) { lastRunState = s || ""; }

function syncCards() {
  if (!elCards) return;
  const cards = elCards.querySelectorAll('.aw-card');
  for (let i=0;i<cards.length;i++) {
    const c = cards[i];
    const needsWs = c.getAttribute('data-needs-ws') === '1';
    const blocked = needsWs && !state.hasWorkspace;
    const busy = state.sending;
    c.classList.toggle('is-busy', busy);
    c.classList.toggle('is-loading', busy);
    if (busy) c.setAttribute('data-run-state','running');
    else if (c.getAttribute('data-run-state') === 'running')
      c.setAttribute('data-run-state', lastRunState || '');
    const badge = c.querySelector('.aw-card-state');
    if (badge) {
      const st = c.getAttribute('data-run-state') || '';
      badge.textContent = RUN_STATE_TEXT[st] || '';
    }
    c.setAttribute('aria-disabled', blocked ? 'true':'false');
  }
}
function syncSend() {
  if (!elSend) return;
  const has = elInput && elInput.value && elInput.value.trim().length>0;
  elSend.disabled = !has;
  elSend.classList.toggle('is-stop', !!state.sending);
  syncCards();
}
function markCardUsed(kind) {
  cardEls.forEach(c => { if (c.getAttribute('data-kind')===kind) c.classList.add('is-selected'); });
}

function abortActiveRun(notifyBackend) {
  const tid = activeRun.taskId;
  if (notifyBackend !== false && tid && AUTH && !activeRun.cancelNotified) {
    activeRun.cancelNotified = true;
    try { AUTH.post('/api/agent/cancel', { task_id: tid }).catch(()=>{}); } catch(e){}
  }
  try { if (activeRun.controller) activeRun.controller.abort(); } catch(e){}
  activeRun.controller=null; activeRun.watchdog=null; activeRun.stallTimer=null;
  activeRun.taskId=""; activeRun.cancelNotified=false;
  state.awaitingApproval=false; state.approvalSince=0;
}

function stopRun() {
  if (!state.sending) return;
  try { if (typeof state.markStopped === 'function') state.markStopped(); } catch(e){}
  abortActiveRun(true);
  state.sending=false; state.awaitingApproval=false; state.approvalSince=0;
  hideBusy();
  setStatus('idle','已停止');
  setRunState('stopped');
  syncCards(); syncSend();
  if (elInput) { try { elInput.focus(); } catch(e){} }
  toast('已停止生成');
}

function handle(evt, answerEl, onText) {
  const t = evt.type;
  if (t === 'run_start') {
    if (evt.task_id) { activeRun.taskId = String(evt.task_id); activeRun.cancelNotified = false; }
  } else if (t === 'chunk') {
    answerEl.textContent = answerEl.textContent + (evt.content||'');
  }
}

// ── 精简版 send（只保留与四个缺陷相关的状态流转）─────────────
function send() {
  if (!elInput) return Promise.resolve();
  if (state.sending) { stopRun(); return Promise.resolve(); }
  const text = elInput.value.trim();
  if (!text) return Promise.resolve();
  const myId = ++runSeq.v;
  state.sending = true;
  state.lastPrompt = text;
  let stoppedByUser = false;
  const markStopped = () => { if (myId === runSeq.v) stoppedByUser = true; };
  state.markStopped = markStopped;
  activeRun.cancelNotified = false;
  hideRetry();
  elInput.value = '';
  const answerEl = makeEl('div');
  syncSend();
  setStatus('running','运行中');

  return new Promise(resolve => {
    // 模拟：立即派发 run_start（带 task_id），随后陆续 chunk
    handle({ type:'run_start', task_id:'t-test01' }, answerEl);
    handle({ type:'chunk', content:'你好' }, answerEl);
    // 由测试显式调用 finish()/abort() 决定结局
    state._answerEl = answerEl;
    state._finish = () => {
      handle({ type:'chunk', content:'，世界' }, answerEl);
      if (stoppedByUser) { stopRun(); resolve('stopped'); return; }
      hideBusy(); setStatus('idle','空闲'); setRunState('done'); syncCards();
      resolve('done');
    };
    state._fail = err => {
      if (stoppedByUser) { stopRun(); resolve('stopped'); return; }
      answerEl.textContent = '请求失败：' + err;
      hideBusy(); setStatus('idle','空闲'); setRunState('failed'); syncCards();
      showRetry(text, err); toast('发送失败：'+err);
      resolve('failed');
    };
    state._abort = () => {
      // 模拟 AbortController 因看门狗超时而中止（**非**用户主动停止）
      const e = new Error('aborted'); e.name = 'AbortError';
      if (stoppedByUser) { stopRun(); resolve('stopped'); return; }
      /* 超时路径：也要通知后端取消，并如实显示「已停止等待」 */
      abortActiveRun(true);
      state.sending = false;
      hideBusy(); setStatus('idle','已停止等待'); setRunState('stopped');
      syncCards(); syncSend();
      toast('后端响应超时，已停止等待并通知后端取消');
      resolve('stopped-by-timeout');
    };
  }).then(() => {
    if (state.sending) { state.sending=false; syncSend(); }
    if (elInput) { try { elInput.focus(); } catch(e){} }
  });
}

// 删除会话（与实现一致）
function deleteSession(sid) {
  if (!AUTH) return Promise.resolve();
  const key = String(sid);
  if (deletingSessions[key]) return Promise.resolve();
  deletingSessions[key] = true;
  const snapshot = sessionsCache;
  sessionsCache = sessionsCache.filter(s => String(s.id) !== key);
  if (sessionsCache.length !== snapshot.length) renderSessions(sessionsCache);
  if (key === String(state.sessionId)) { state.sessionId = null; }
  return AUTH.del('/api/aichat/sessions/' + encodeURIComponent(sid))
    .then(() => loadSessions())
    .then(() => '会话已删除')
    .catch(() => { sessionsCache = snapshot; renderSessions(sessionsCache); toast('删除失败，请稍后重试'); return '失败'; })
    .then(r => { delete deletingSessions[key]; return r; });
}
function renderSessions(list) {
  const main = (list||[]).filter(s => !s.archived);
  $('awConvList').textContent = '';
  if (!main.length) $('awConvList').appendChild(el('div','aw-side-empty','还没有会话'));
  else main.forEach(() => $('awConvList').appendChild(el('div','aw-conv')));
  const arch = (list||[]).filter(s => s.archived);
  $('awArchCount').textContent = String(arch.length);
}
function loadSessions() {
  const mySeq = ++sessionsReqSeq;
  return AUTH.get('/api/aichat/sessions').then(res => {
    if (mySeq < sessionsReqSeq) return;
    sessionsCache = res || []; renderSessions(sessionsCache);
  }).catch(()=>{});
}

// ── 测试 ──────────────────────────────────────────────────
let pass=0, fail=0;
const ck=(n,c,x)=>{ if(c){console.log('  PASS  '+n);pass++;} else {console.log('  FAIL  '+n+(x?'  → '+x:''));fail++;} };
const cardState = i => cardEls[i].getAttribute('data-run-state')||'';
const badgeText = i => cardEls[i].querySelector('.aw-card-state').textContent;
const mainRows = () => $('awConvList').children.length;

(async function () {
  console.log('=== 缺陷 3+4：停止按钮真正取消后端 ===');
  AUTH._calls = []; AUTH._cancelCalls = 0;
  elInput.value = '写一篇长文';
  const p = send();
  await Promise.resolve();
  ck('生成中：sending=true', state.sending === true);
  ck('生成中：taskId 已捕获', activeRun.taskId === 't-test01', 'got '+activeRun.taskId);
  ck('生成中：卡片态=running', cardState(0) === 'running', cardState(0));
  ck('生成中：角标=生成中', badgeText(0) === '生成中', badgeText(0));
  ck('生成中：发送按钮为 stop 态', elSend.classList.contains('is-stop'));

  stopRun();
  /* stopRun 之后 promise 链需要一个终结点（真实实现里是 AbortError 触发 catch）。
     这里显式调用 _abort 模拟控制器中止，让链走完并复位。 */
  state._abort();
  await p;
  ck('★已发送 POST /api/agent/cancel', AUTH._calls.some(c => c[0]==='POST' && c[1]==='/api/agent/cancel'),
     JSON.stringify(AUTH._calls.map(c=>c[1])));
  ck('★cancel 携带了 task_id', (AUTH._calls.find(c=>c[1]==='/api/agent/cancel')||[])[2]?.task_id === 't-test01');
  ck('停止后 sending=false', state.sending === false);
  ck('停止后 taskId 已清空（无残留取消标记）', activeRun.taskId === '', 'got '+activeRun.taskId);
  ck('停止后 cancelNotified 已复位', activeRun.cancelNotified === false);
  ck('停止后状态文案=已停止', $('awStatusText').textContent === '已停止', $('awStatusText').textContent);
  ck('★卡片态=stopped', cardState(0) === 'stopped', cardState(0));
  ck('★角标=已停止', badgeText(0) === '已停止', badgeText(0));
  ck('★卡片不再转圈（is-loading 移除）', !cardEls[0].classList.contains('is-loading'));
  ck('★生成中指示条已隐藏', elBusy.hidden === true);
  /* 空输入框时按钮置灰是**设计如此**（有内容才能点），不是缺陷。
     真正的要求是「输入框恢复可编辑、随时能继续输入」—— 下面用有内容的情形验证。 */
  elInput.value = '继续提问'; syncSend();
  ck('★输入框恢复可用（有内容即可发送）', elSend.disabled === false);
  elInput.value = ''; syncSend();
  ck('停止提示文案', toastMsg === '已停止生成', toastMsg);

  console.log('');
  console.log('=== 缺陷 4：停止后能继续对话 ===');
  AUTH._calls = []; AUTH._cancelCalls = 0;
  elInput.value = '第二个问题';
  ck('可输入（按钮随内容亮起）', (elInput.value='', syncSend(), elSend.disabled===true));
  elInput.value = '第二个问题'; syncSend();
  ck('有内容时可点发送', elSend.disabled === false);
  const p2 = send();
  await Promise.resolve();
  ck('新一轮已启动', state.sending === true);
  ck('★新一轮能再次取消（标记已清）', (stopRun(), state._abort(), AUTH._cancelCalls === 1), 'cancelCalls='+AUTH._cancelCalls);
  await p2;

  console.log('');
  console.log('=== 停止 vs 超时：文案不得混淆 ===');
  AUTH._cancelCalls = 0;
  elInput.value = 'x'; const p3 = send(); await Promise.resolve();
  const r3 = state._abort();   // 模拟 AbortController 超时中止（**非**用户主动）
  await p3;
  /* 关键：超时中止与用户主动停止是两件事。
     超时后后端可能仍在跑，所以必须（a）也发 cancel，（b）如实显示「已停止等待」
     而不是谎称「已停止生成」。 */
  ck('超时中止不谎称「已停止生成」', toastMsg !== '已停止生成', 'toast='+toastMsg);
  ck('★超时也会通知后端取消', AUTH._cancelCalls === 1, 'cancelCalls='+AUTH._cancelCalls);
  ck('★超时时状态文案=已停止等待', $('awStatusText').textContent === '已停止等待', $('awStatusText').textContent);
  ck('★超时后卡片态=stopped（不再转圈）', cardState(0) === 'stopped', cardState(0));
  ck('超时中止仍完成了状态复位', state.sending === false);

  console.log('');
  console.log('=== 正常完成路径 ===');
  elInput.value = '正常问题'; const p4 = send(); await Promise.resolve();
  state._finish(); await p4;
  ck('完成：sending=false', state.sending === false);
  ck('完成：卡片态=done', cardState(0) === 'done', cardState(0));
  ck('完成：角标=已完成', badgeText(0) === '已完成', badgeText(0));

  console.log('');
  console.log('=== 失败路径 ===');
  elInput.value = '会失败'; const p5 = send(); await Promise.resolve();
  state._fail('网络错误'); await p5;
  ck('失败：卡片态=failed', cardState(0) === 'failed', cardState(0));
  ck('失败：角标=失败', badgeText(0) === '失败', badgeText(0));
  ck('失败：给出重试入口', state.retryable === true);
  ck('失败：sending 已复位', state.sending === false);

  console.log('');
  console.log('=== 缺陷 1：删除会话及时生效 + 失败提示 ===');
  AUTH._delFail = false;
  sessionsCache = [{id:1},{id:2},{id:3}];
  AUTH._server = JSON.parse(JSON.stringify(sessionsCache));  // 服务端同步初始状态
  renderSessions(sessionsCache);
  ck('初始 3 条', mainRows() === 3, 'got '+mainRows());
  await deleteSession(2);
  ck('删除后列表剩 2 条', mainRows() === 2, 'got '+mainRows());
  ck('删除后缓存剩 2 条', sessionsCache.length === 2, 'got '+sessionsCache.length);
  ck('服务端也已删除（前后端一致）', AUTH._server.length === 2 && !AUTH._server.some(s=>s.id===2),
     JSON.stringify(AUTH._server));
  ck('后端已调 DELETE', AUTH._calls.some(c => c[0]==='DELETE' && c[1].indexOf('/2')>0));
  ck('去重标记已清', Object.keys(deletingSessions).length === 0);

  // 失败回滚：服务端状态不变，界面必须还原
  AUTH._delFail = true;
  toastMsg = '';
  const before = sessionsCache.length;
  const r = await deleteSession(1);
  ck('★删除失败有明确提示', r === '失败' && toastMsg === '删除失败，请稍后重试', 'r='+r+' toast='+toastMsg);
  ck('★失败后界面回滚（不做视觉隐藏）', sessionsCache.length === before && mainRows() === before,
     'cache='+sessionsCache.length+' rows='+mainRows());
  ck('★失败后前端与后端仍一致', sessionsCache.length === AUTH._server.length,
     'front='+sessionsCache.length+' server='+AUTH._server.length);
  AUTH._delFail = false;

  console.log('');
  console.log('=== 无工作区：编码类卡片禁用 ===');
  state.hasWorkspace = false; syncCards();
  ck('探索卡(needs-ws=0)可用', cardEls[0].getAttribute('aria-disabled') === 'false');
  ck('构建卡(needs-ws=1)禁用', cardEls[1].getAttribute('aria-disabled') === 'true');
  state.hasWorkspace = true; syncCards();
  ck('设置工作区后全部解锁', cardEls.every(c => c.getAttribute('aria-disabled')==='false'));

  console.log('');
  console.log('='.repeat(46));
  console.log('结果： ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
