const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

// Load the complete production script; only DOM and transport are replaced.
function load() {
    const nodes = {};
    function element() {
        return {value: '', textContent: '', innerHTML: '', hidden: false, style: {}, children: [],
            handlers: {}, classList: {add() {}, remove() {}, toggle() {}, contains() {return false;}},
            addEventListener(name, fn) {this.handlers[name] = fn;},
            appendChild(child) {this.children.push(child);},
            setAttribute() {}, removeAttribute() {}, getAttribute() {return null;}};
    }
    const template = fs.readFileSync(path.join(__dirname, '../templates/agent/_settings_modal.html'), 'utf8');
    for (const match of template.matchAll(/\bid="([^"]+)"/g)) nodes[match[1]] = element();
    const requests = [], toasts = [];
    const view = {active: 'deepseek', chain: ['deepseek', 'backup', 'offline'], temperature: 0, max_tokens: 8192,
        providers: [{key: 'deepseek', label: 'DeepSeek', enabled: true, base_url: 'https://api.deepseek.com',
            model: 'test-model', timeout: 137, context_length: 300000, has_key: true, masked_key: 'test...saved'},
            {key: 'backup', label: 'Backup', enabled: true, base_url: 'https://backup.invalid', model: 'backup'}]};
    let modelsResult = {ok: true, models: ['remote-model'], source: 'remote'};
    let saveResult = view;
    const window = {location: {origin: 'https://ui.invalid'},
        escapeText: s => String(s || ''), escapeAttrText: s => String(s || ''),
        showToast: m => toasts.push(m), addEventListener() {}, dispatchEvent() {},
        AegisAuth: {fetchJSON(url, options) {
            requests.push({url, options});
            // fetchJSON already unwraps api_ok().data in the actual auth module.
            return Promise.resolve(url.endsWith('/models') ? modelsResult : options.method === 'PUT' ? saveResult : view);
        }}};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/settings.js'), 'utf8'), {
        window, document: {readyState: 'complete', getElementById: id => nodes[id] || null,
            createElement: element, addEventListener() {}, body: element(), querySelectorAll() {return [];}},
        URL, setTimeout, clearTimeout, CustomEvent: function() {}, console,
    });
    const flush = () => new Promise(resolve => setImmediate(resolve));
    return {nodes, requests, toasts, settings: window.AegisSettings, flush,
        setModelsResult: value => {modelsResult = value;}, setSaveResult: value => {saveResult = value;}};
}

test('model fetch consumes unwrapped response and sends saved-provider key with blank credential omitted', async () => {
    const app = load();
    await app.settings.open();
    app.nodes.stFetchModelsBtn2.handlers.click();
    await app.flush();
    assert.deepEqual(app.nodes.stModelList.children.map(n => n.value), ['remote-model']);
    const payload = JSON.parse(app.requests.find(r => r.url.endsWith('/models')).options.body);
    assert.equal(payload.provider_key, 'deepseek');
    assert.equal('api_key' in payload, false);
    assert(app.toasts.includes('已获取 1 个模型'));
});

test('HTTP authentication failure keeps its reason and action in settings', async () => {
    const app = load();
    await app.settings.open();
    app.setModelsResult({ok: false, models: [], error: {code: 'INVALID_KEY', reason: '密钥已失效', action: '重新生成密钥'}});
    app.nodes.stFetchModelsBtn2.handlers.click();
    await app.flush();
    assert.match(app.nodes.stContextHint.textContent, /密钥已失效/);
    assert.match(app.nodes.stContextHint.textContent, /重新生成密钥/);
});

test('advanced settings hydrate then save zero temperature, max_tokens and context reset without chain edits', async () => {
    const app = load();
    await app.settings.open();
    assert.equal(Number(app.nodes.stTemp.value), 0);
    assert.equal(Number(app.nodes.stMaxTokens.value), 8192);
    app.nodes.stContextLen.value = '0';
    await app.settings.save(true);
    const payload = JSON.parse(app.requests.find(r => r.options.method === 'PUT').options.body);
    assert.equal(payload.temperature, 0);
    assert.equal(payload.max_tokens, 8192);
    assert.equal(payload.providers[0].context_length, 0);
    assert.equal('api_key' in payload.providers[0], false);
    assert.equal('chain' in payload, false);
    assert.equal(app.nodes.stChain, undefined);
});

test('context draft survives switching providers and returns to its own field', async () => {
    const app = load();
    await app.settings.open();
    app.nodes.stContextLen.value = '250000';
    app.nodes.stProviderSel.value = 'backup';
    app.nodes.stProviderSel.handlers.change();
    app.nodes.stProviderSel.value = 'deepseek';
    app.nodes.stProviderSel.handlers.change();
    assert.equal(Number(app.nodes.stContextLen.value), 250000);
});

test('key-store failure prevents false successful save and preserves draft', async () => {
    const app = load();
    await app.settings.open();
    app.nodes.stApiKey.value = 'test-new-key';
    app.setSaveResult({key_failed: ['deepseek'], providers: []});
    await assert.rejects(app.settings.save(true), /密钥/);
    assert.equal(app.settings.state.draft.deepseek.api_key, 'test-new-key');
});
