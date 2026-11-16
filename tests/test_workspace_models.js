const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function load() {
    function element(){return {value:'',disabled:false,children:[],events:{},setAttribute(){},
        appendChild(child){this.children.push(child);},addEventListener(k,f){this.events[k]=f;},
        set textContent(v){this.text=v;this.children=[];},get textContent(){return this.text||'';}};}
    const nodes={awModelSelect:element(),awModelContext:element()},requests=[],events={};
    const provider={key:'deepseek',model:'small',candidate_models:['large','small'],context_length_effective:300000,context_source:'remote'};
    let view={active:'deepseek',providers:[provider]}, fail=false, pending=null;
    const auth={get:async()=>view,put:async(url,payload)=>{
        requests.push({url,payload});
        if(fail)throw new Error('save failed');
        if(pending)await pending;
        view={active:'deepseek',providers:[{...provider,model:'large',context_length_effective:1048576}]};return view;
    }};
    const window={AegisAuth:auth,addEventListener(k,f){events[k]=f;},dispatchEvent(){}};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/js/workspace_models.js'),'utf8'),{
        window,document:{getElementById:id=>nodes[id],createElement:element},CustomEvent:class{}});
    function select(model){nodes.awModelSelect.value=model;return nodes.awModelSelect.events.change();}
    return {nodes,requests,window,events,select,setFail(){fail=true;},setPending(value){pending=value;}};
}
test('composer exposes the real model list and current context window',async()=>{
    const h=load();await flush();
    assert.deepEqual(h.nodes.awModelSelect.children.map(n=>n.textContent),['large','small']);
    assert.equal(h.nodes.awModelSelect.value,'small');
    assert.match(h.nodes.awModelContext.textContent,/300,?000/);
});
test('model switch persists model and automatic context before showing the returned window',async()=>{
    const h=load();await flush();await h.select('large');
    const payload=JSON.parse(JSON.stringify(h.requests[0].payload));
    assert.deepEqual(payload,{active:'deepseek',providers:[{key:'deepseek',model:'large',context_length:0}]});
    assert.equal(h.nodes.awModelSelect.value,'large');
    assert.match(h.nodes.awModelContext.textContent,/1,?048,?576/);
});
test('failed model switch restores the previous selection and avoids a false context update',async()=>{
    const h=load();await flush();h.setFail();await h.select('large');
    assert.equal(h.nodes.awModelSelect.value,'small');
    assert.match(h.nodes.awModelContext.textContent,/切换失败/);
});
test('running task blocks model switching',async()=>{
    const h=load();await flush();h.window.AegisModelPicker.setBusy(true);
    assert.equal(h.nodes.awModelSelect.disabled,true);
    await h.select('large');assert.equal(h.requests.length,0);
});
test('pending save remains busy until the server acknowledges the model change',async()=>{
    const h=load();await flush();let resolve;
    h.setPending(new Promise(done=>resolve=done));
    const saving=h.select('large');
    assert.equal(h.window.AegisModelPicker.saving(),true);
    assert.equal(h.nodes.awModelSelect.disabled,true);
    resolve();await saving;
    assert.equal(h.window.AegisModelPicker.saving(),false);
});
