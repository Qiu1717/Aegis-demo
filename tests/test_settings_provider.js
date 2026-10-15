// 检查真实 renderProvider：厂商配置存在时不能引用未定义的 key。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '../static/js/settings.js'), 'utf8');
const start = source.indexOf('    function renderProvider() {');
const end = source.indexOf('    function renderKeystoreNote()', start);
assert(start >= 0 && end > start);
const render = source.slice(start, end);

for (const [provider, hidden, label] of [
    [{key:'deepseek'}, false, '删除该预设'],
    [{key:'custom', custom:true}, false, '删除该厂商'],
    [{key:'offline'}, true, '删除该预设'],
    [null, true, '删除该预设'],
]) {
    const button = {hidden:false, textContent:''};
    vm.runInNewContext(render + '\nrenderProvider();', {
        state: {current: provider ? provider.key : ''},
        findProvider: () => provider,
        findPreset: () => ({}),
        draftOf: () => ({}),
        $: id => id === 'stCustDelBtn' ? button : null,
        renderModels() {}, renderChain() {}, renderKeystoreNote() {}, clearStatus() {},
    });
    assert.equal(button.hidden, hidden);
    assert.equal(button.textContent, label);
}
console.log('Settings provider rendering: 4 cases passed');
