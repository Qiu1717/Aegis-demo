/**
 * 回归测试：设置弹窗「× 关不掉、取消能关」
 *
 * 根因：× 按钮内部包着 <svg>，真实点击时 e.target 是 svg/path，
 *       它们没有 data-act 属性 → 事件委托判断 act === "close" 恒为 null。
 *       「取消」是直接绑 id 的，不走委托，所以能关。
 *
 * 本测试**必须**用 svg/path 作为 e.target 才能复现；
 * 若用 button 自身作为 target，修复前后都会通过 —— 那正是我前几轮
 * 一直「测不出来」的原因。
 */
const fs = require('fs');
const js = fs.readFileSync(require('path').join(__dirname, '../static/js/settings.js'), 'utf8');

let pass = 0, fail = 0;
const ck = (n, c, x) => { if (c) { console.log('  PASS  ' + n); pass++; }
                          else { console.log('  FAIL  ' + n + (x ? '  → ' + x : '')); fail++; } };

// ── 带真实冒泡 + parentNode 的 DOM 桩 ────────────────────
function El(tag, cls) {
  const n = {
    tagName: (tag || 'div').toUpperCase(),
    _cls: cls || '', _text: '', children: [], parentNode: null,
    attrs: {}, hidden: false, style: {}, type: '', value: '',
    classList: null, dataset: {},
    get className() { return this._cls; },
    set className(v) { this._cls = v; },
    get textContent() {
      return this.children.length ? this._text + this.children.map(c => c.textContent).join('') : this._text;
    },
    set textContent(v) { this._text = v == null ? '' : String(v); this.children = []; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    remove() {}, focus() {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return k in this.attrs; },
    addEventListener(ev, fn) { (this._h = this._h || {})[ev] = (this._h[ev] || []).concat(fn); },
    removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    dispatchEvent(evt) {
      let node = this;
      while (node) {
        const hs = (node._h || {})[evt.type] || [];
        for (const fn of hs) if (!evt._stopped) fn.call(node, evt);
        if (evt.cancelBubble) break;
        node = node.parentNode;
      }
    },
  };
  n.classList = {
    _s: new Set((cls || '').split(/\s+/).filter(Boolean)),
    add(...c) { c.forEach(x => n.classList._s.add(x)); },
    remove(...c) { c.forEach(x => n.classList._s.delete(x)); },
    contains(c) { return n.classList._s.has(c); },
    toggle(c, on) { on ? n.classList._s.add(c) : n.classList._s.delete(c); },
  };
  return n;
}

const nodes = {};
['settingsModal','stCancelBtn','stSaveBtn','stTestBtn','stCustDelBtn','stFetchModelsBtn',
 'stFetchModelsBtn2','stCustAddBtn','stProviderSel','stBaseUrl','stModel','stApiKey','stTimeout',
 'stContextLen','stContextHint','stCustLabel','stCustKey','stCustUrl','stCustModel','stCustApiKey',
 'stToggleKey','stKeyUrl','stMaskHint','stFreeNote','stKeyStoreNote','stTestResult','stErrorAction',
 'stModelList','stCustModelList','stChain','stFetchModelsHint','aegisSetupBanner','chatInput','sendBtn',
 'gdBox','stCustBox','stReason','gdGlobal','gdRefusal','gdCatList','gdCatLabel','gdCatAddBtn',
 'gdKwList','gdKwInput','gdKwAddBtn','gdKwBatch','gdExportBtn','gdImportBtn','gdImportFile',
 'gdDebugBtn','gdDebugOut','gdKwCatSel','gdImportArea'].forEach(id => { nodes[id] = El('div'); });

// 按真实结构搭建：modal > mask, panel > header > (button > svg > path)
const modal = nodes.settingsModal;
const mask = El('div', 'aegis-modal__mask');
mask.setAttribute('data-act', 'close');
const panel = El('div', 'aegis-modal__panel');
const header = El('header', 'aegis-modal__head');
const closeBtn = El('button', 'aegis-icon-btn');
closeBtn.setAttribute('data-act', 'close');
closeBtn.setAttribute('type', 'button');
const svg = El('svg');
const path = El('path');
svg.appendChild(path);
closeBtn.appendChild(svg);
header.appendChild(closeBtn);
panel.appendChild(header);
panel.appendChild(nodes.stCancelBtn);
panel.appendChild(nodes.stSaveBtn);
modal.appendChild(mask);
modal.appendChild(panel);
modal.classList.remove('is-open');

let toasts = [];
let saveMode = 'ok';   // ok / fail
global.escapeText = global.esc = s => (s == null ? '' : String(s));
global.escapeAttrText = global.escAttr = global.escapeText;
global.document = {
  readyState: 'complete',
  getElementById: id => nodes[id] || null,
  createElement: t => El(t),
  addEventListener(ev, fn) { (this._h = this._h || {})[ev] = (this._h[ev] || []).concat(fn); },
  dispatchEvent(e) { const hs = (this._h || {})[e.type] || []; hs.forEach(f => f(e)); },
  querySelectorAll: () => [],
  body: El('body'),
};
global.window = {
  AegisAuth: {
    getToken: () => 'T',
    fetchJSON: (url, opts) => {
      if (opts && opts.method === 'PUT') {
        if (saveMode === 'fail') return Promise.reject('字符串形式的错误');
        if (saveMode === 'throwObj') return Promise.reject({ code: 500 });
        return Promise.resolve({ providers: [], chain: [], presets: [] });
      }
      return Promise.resolve({ providers: [], chain: [], presets: [], workspace: '' });
    },
  },
  addEventListener: () => {},
  showToast: (m) => { toasts.push(m); },
  showConfirm: () => Promise.resolve(true),
  dispatchEvent: () => {},
  CustomEvent: function (n) { this.type = n; },
  getComputedStyle: () => ({ display: 'flex' }),
  setTimeout, clearTimeout,
};
global.CustomEvent = window.CustomEvent;
global.setTimeout = setTimeout; global.clearTimeout = clearTimeout;
global.toast = m => toasts.push(m);
global.window.showToast = m => toasts.push(m);

console.log('=== 执行 settings.js ===');
try { new Function(js)(); console.log('  执行成功'); }
catch (e) { console.log('  ★抛错:', e.message); fail++; }

const S = window.AegisSettings;
const isOpen = () => modal.classList.contains('is-open');
const clickOn = (target) => modal.dispatchEvent({ type: 'click', target,
  cancelBubble: false, _stopped: false, preventDefault(){}, stopPropagation(){} });

const run = (name, fn) => { console.log(); console.log('=== ' + name + ' ==='); fn(); };

setTimeout(() => {
  run('★核心：点 × 内部的 <svg>（真实浏览器的 e.target）', () => {
    S.open();
    ck('先确认已打开', isOpen());
    clickOn(svg);
    ck('★点 svg 后关闭（修复前必然 FAIL）', !isOpen(), 'is-open 仍为 true');
  });

  run('点 × 内部的 <path>', () => {
    S.open();
    clickOn(path);
    ck('★点 path 后关闭', !isOpen());
  });

  run('点 × 按钮本身', () => {
    S.open();
    clickOn(closeBtn);
    ck('点 button 关闭', !isOpen());
  });

  run('点遮罩', () => {
    S.open();
    clickOn(mask);
    ck('点遮罩关闭', !isOpen());
  });

  run('点取消（直接绑 id，不走委托）', () => {
    S.open();
    nodes.stCancelBtn.dispatchEvent({ type: 'click', target: nodes.stCancelBtn,
      cancelBubble: false, preventDefault(){}, stopPropagation(){} });
    ck('取消关闭（本来就能关，作为对照）', !isOpen());
  });

  run('保存成功 → 关闭', () => {
    S.open(); saveMode = 'ok'; toasts = [];
    nodes.stSaveBtn.dispatchEvent({ type: 'click', target: nodes.stSaveBtn,
      cancelBubble: false, preventDefault(){}, stopPropagation(){} });
    setTimeout(() => {
      ck('★保存成功后关闭', !isOpen());
      ck('按钮文案恢复', nodes.stSaveBtn.textContent === '保存', nodes.stSaveBtn.textContent);
      run('保存失败（字符串错误）→ 保持打开 + 有提示', () => {
        S.open(); saveMode = 'fail'; toasts = [];
        nodes.stSaveBtn.dispatchEvent({ type: 'click', target: nodes.stSaveBtn,
          cancelBubble: false, preventDefault(){}, stopPropagation(){} });
        setTimeout(() => {
          ck('★失败时保持打开', isOpen());
          ck('★有明确错误提示', toasts.some(t => /保存失败/.test(t)), JSON.stringify(toasts));
          run('保存失败（非 Error 对象）也不崩', () => {
            saveMode = 'throwObj'; toasts = [];
            nodes.stSaveBtn.dispatchEvent({ type: 'click', target: nodes.stSaveBtn,
              cancelBubble: false, preventDefault(){}, stopPropagation(){} });
            setTimeout(() => {
              ck('非 Error 对象也能提示', toasts.some(t => /保存失败/.test(t)),
                 JSON.stringify(toasts));
              ck('按钮仍恢复可用', nodes.stSaveBtn.disabled === false);
              run('反复开关 5 次均正常', () => {
                let bad = 0;
                for (let k = 0; k < 5; k++) {
                  S.open();
                  if (!isOpen()) bad++;
                  clickOn(svg);
                  if (isOpen()) bad++;
                }
                ck('5 轮开关全部正常', bad === 0, '异常 ' + bad + ' 次');
                console.log();
                console.log('='.repeat(52));
                console.log('结果：%d passed, %d failed', pass, fail);
                process.exit(fail ? 1 : 0);
              });
            }, 40);
          }, 40);
        }, 40);
      });
    }, 40);
  });
}, 40);
