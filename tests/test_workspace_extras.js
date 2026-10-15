const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness(options = {}) {
    const nodes = {}, handlers = {}, calls = [];
    function node() {
        const classes = new Set();
        return {textContent:'', value:'', checked:false, disabled:false, hidden:true, children:[], listeners:{},
            classList:{add:x=>classes.add(x), remove:x=>classes.delete(x), toggle:(x,on)=>on ? classes.add(x):classes.delete(x), contains:x=>classes.has(x)},
            style:{setProperty(k,v){this[k]=v;}},
            setAttribute(k,v){this[k]=v;}, appendChild(x){this.children.push(x);},
            addEventListener(k,f){this.listeners[k]=f;}, click(){if(this.listeners.click)this.listeners.click.call(this);}};
    }
    ['awToast','awAttachments','awBtnAttach','awAttachInput','awBtnAppearance','awAppearanceModal',
     'awTheme','awFontSize','awSidebarCollapsed','awTaskDetails','awAppearanceClose','awAppearanceMask',
     'awAppearanceSave','awMenuBtn','awSkillImportStatus','awSkillImportFile','awSkillFile',
     'awSkillImportGithub','awSkillGithub','awSkillCreate','awSkillName','awSkillInstructions'].forEach(id=>nodes[id]=node());
    const document = {getElementById:id=>nodes[id], createElement:node, body:node(), documentElement:node(), addEventListener(){}};
    const initial = {theme:'system',font_size:14,sidebar_collapsed:false,task_details:true};
    const auth = {
        get:async()=>options.initialLoad || initial,
        put:async(url, data)=>{calls.push({url,data});if(options.saveError)throw new Error('保存失败');return {...initial,...data};},
        post:async(url,data)=>{calls.push({url,data});if(options.importError)throw new Error('GitHub 下载失败');return {total:1};},
        del:async(url)=>{calls.push({url});return {};}
    };
    class FormData { constructor(){this.fields={};} append(k,v){this.fields[k]=v;} }
    const window = {AegisAuth:auth,addEventListener:(k,f)=>handlers[k]=f,dispatchEvent:e=>handlers[e.type]?.(e),
        aegisFetchJSON:async(url, data)=>{calls.push({url,data});return {id:'a'.repeat(32),name:data.body.fields.file.name,size:5};}};
    const source = fs.readFileSync(path.join(__dirname,'../static/js/workspace_extras.js'),'utf8');
    vm.runInNewContext(source, {window,document,FormData,CustomEvent:class{constructor(type){this.type=type;}},setTimeout(){}});
    return {window, document, nodes, calls};
}
const flush = ()=>new Promise(resolve=>setImmediate(resolve));

function historyHarness() {
    const source=fs.readFileSync(path.join(__dirname,'../static/js/workspace.js'),'utf8');
    const start=source.indexOf('function openSession(sid)');
    const end=source.indexOf('function renderMessages(',start);
    const requests={}, visible=[], notices=[];
    const state={sending:false,sessionId:null,lastAttachments:[]};
    const elMsgs={textContent:'old visible history'};
    const context={state,elMsgs,extras:null,newIntentSeq:0,
        AUTH:{get:url=>new Promise((resolve,reject)=>requests[url.split('/').at(-2)]={resolve,reject})},
        resetTask(){},renderMessages:messages=>visible.push(messages[0].message_content),
        setView(){},setStatus(){},loadSessions:()=>Promise.resolve(),toast:message=>notices.push(message)};
    vm.runInNewContext(source.slice(start,end), context);
    return {context,requests,visible,notices,state,elMsgs};
}

test('late history response never replaces the currently selected chat',async()=>{
    const h=historyHarness();
    h.context.openSession('A');h.context.openSession('B');
    assert.equal(h.elMsgs.textContent,'');
    h.requests.B.resolve([{message_content:'B history'}]);await flush();
    h.requests.A.resolve([{message_content:'A history'}]);await flush();
    assert.equal(h.state.sessionId,'B');
    assert.deepEqual(h.visible,['B history']);
});

test('failed stale history request does not report an error on the new chat',async()=>{
    const h=historyHarness();
    h.context.openSession('A');h.context.openSession('B');
    h.requests.B.resolve([{message_content:'B history'}]);await flush();
    h.requests.A.reject(new Error('old request failed'));await flush();
    assert.deepEqual(h.notices,[]);
});

function resetDeletedChat(h) {
    const source=fs.readFileSync(path.join(__dirname,'../static/js/workspace.js'),'utf8');
    const start=source.indexOf('function resetNewKeepList()');
    const end=source.indexOf('function addMsg(', start);
    const state={sessionId:42,lastAttachments:[{id:'old-retry'}]};
    vm.runInNewContext(source.slice(start,end)+'\nresetNewKeepList();', {
        state, newIntentSeq:0, extras:h.window.AegisWorkspaceExtras, elMsgs:null, elCards:null,
        resetTask(){}, hideBusy(){}, hideRetry(){}, setView(){}, setStatus(){}
    });
    return state;
}

test('deleting the current chat clears pending and retry attachments', async()=>{
    const h=harness();await flush();
    h.window.AegisWorkspaceExtras.restore([{id:'c'.repeat(32),name:'private-A.txt',size:5}]);
    const state=resetDeletedChat(h);await flush();
    assert.equal(h.window.AegisWorkspaceExtras.files().length,0);
    assert.equal(state.lastAttachments.length,0);
    assert(h.calls.some(c=>c.url==='/api/agent/attachments/'+'c'.repeat(32)));
});

test('deleting the current chat during upload rejects the late attachment', async()=>{
    const h=harness();await flush();
    let resolveUpload;
    h.window.aegisFetchJSON=()=>new Promise(resolve=>resolveUpload=resolve);
    h.nodes.awAttachInput.files=[{name:'late-A.txt',size:5}];
    h.nodes.awAttachInput.listeners.change.call(h.nodes.awAttachInput);
    resetDeletedChat(h);
    resolveUpload({id:'d'.repeat(32),name:'late-A.txt',size:5});await flush();
    assert.equal(h.window.AegisWorkspaceExtras.files().length,0);
    assert(h.calls.some(c=>c.url==='/api/agent/attachments/'+'d'.repeat(32)));
});

test('late appearance load cannot undo newly saved preferences', async()=>{
    let resolveInitial;
    const initialLoad = new Promise(resolve=>resolveInitial=resolve);
    const h=harness({initialLoad});
    h.nodes.awTheme.value='dark';h.nodes.awFontSize.value='16';h.nodes.awAppearanceSave.click();await flush();
    resolveInitial({theme:'system',font_size:14,sidebar_collapsed:false,task_details:true});await flush();
    assert.equal(h.document.documentElement['data-theme'],'dark');
    assert.equal(h.document.documentElement.style['--aw-user-font-size'],'16px');
});

test('appearance save changes rendered theme and font only after persistence succeeds', async()=>{
    const h = harness(); await flush();
    h.nodes.awTheme.value='dark';h.nodes.awFontSize.value='16';h.nodes.awSidebarCollapsed.checked=true;
    h.nodes.awAppearanceSave.click(); await flush();
    assert.equal(h.document.documentElement['data-theme'],'dark');
    assert.equal(h.document.documentElement.style['--aw-user-font-size'],'16px');
    assert(h.document.body.classList.contains('aw-sidebar-collapsed'));
    assert.equal(h.calls[0].url,'/api/agent/appearance');
    const bad=harness({saveError:true});await flush();
    bad.nodes.awTheme.value='dark';bad.nodes.awFontSize.value='16';bad.nodes.awAppearanceSave.click();await flush();
    assert.equal(bad.document.documentElement['data-theme'],'system');
    assert.match(bad.nodes.awToast.textContent,/保存失败/);
});

test('uploads provide real attachment IDs to the caller and removal reaches the backend',async()=>{
    const h=harness();await flush();
    h.nodes.awAttachInput.files=[{name:'code.py',size:5}];
    h.nodes.awAttachInput.listeners.change.call(h.nodes.awAttachInput);await flush();
    assert.equal(h.calls[0].url,'/api/agent/attachments');
    assert.equal(h.window.AegisWorkspaceExtras.files()[0].name,'code.py');
    h.nodes.awAttachments.children.at(-1).children[1].click();await flush();
    assert(h.calls.some(c=>c.url==='/api/agent/attachments/'+'a'.repeat(32)));
    assert.equal(h.window.AegisWorkspaceExtras.files().length,0);
});

test('switching tasks during upload discards its result instead of attaching to another chat',async()=>{
    const h=harness();await flush();
    let resolveUpload;
    h.window.aegisFetchJSON=()=>new Promise(resolve=>resolveUpload=resolve);
    h.nodes.awAttachInput.files=[{name:'old.py',size:5}];
    h.nodes.awAttachInput.listeners.change.call(h.nodes.awAttachInput);
    h.window.AegisWorkspaceExtras.clear();
    resolveUpload({id:'b'.repeat(32),name:'old.py',size:5});await flush();
    assert.equal(h.window.AegisWorkspaceExtras.files().length,0);
    assert(h.calls.some(c=>c.url==='/api/agent/attachments/'+'b'.repeat(32)));
});

test('GitHub import passes entered URL and failed download does not display success',async()=>{
    const h=harness({importError:true});await flush();
    h.nodes.awSkillGithub.value='https://github.com/owner/repo/tree/main/skill';
    h.nodes.awSkillImportGithub.click();await flush();
    assert.equal(h.calls[0].url,'/api/skills/import/github');
    assert.equal(h.calls[0].data.url,'https://github.com/owner/repo/tree/main/skill');
    assert.match(h.nodes.awSkillImportStatus.textContent,/导入失败：GitHub 下载失败/);
    assert.equal(h.nodes.awSkillImportGithub.disabled,false);
});
