const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function harness() {
    const nodes={}, calls=[], streams=[], timers=[];
    function element(tag='div') {
        const classes=new Set();
        return {tagName:tag.toUpperCase(),value:'',style:{},children:[],attributes:{},events:{},hidden:false,disabled:false,
            scrollHeight:200,clientHeight:500,scrollTop:0,
            classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle(x,on){if(on===undefined)on=!classes.has(x);on?classes.add(x):classes.delete(x);return on;}},
            set textContent(v){this.text=v;this.children=[];},get textContent(){return this.text||this.children.map(c=>c.textContent).join('');},
            setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k];},
            appendChild(child){this.children.push(child);return child;},querySelectorAll(){return [];},
            addEventListener(k,fn){this.events[k]=fn;},focus(){},contains(){return false;},
            getBoundingClientRect(){return {height:200,top:0,bottom:500};},scrollTo(){},scrollIntoView(){}};
    }
    const document={visibilityState:'visible',hidden:false,body:element(),documentElement:element(),events:{},
        getElementById(id){return nodes[id]||(nodes[id]=element());},createElement:element,
        querySelectorAll(){return [];},addEventListener(k,f){this.events[k]=f;}};
    const defaults={permission:'approve',thinking:'balanced'};
    const AUTH={
        get:async url=>{calls.push({url,method:'GET'});if(url.endsWith('/sessions'))return [];
            if(url.endsWith('/workspace'))return {path:'/test',valid:true};
            if(url.endsWith('/permission'))return {mode:defaults.permission};
            if(url.endsWith('/thinking'))return {level:defaults.thinking};return {};},
        post:async(url,data)=>{calls.push({url,method:'POST',data});return {};},getToken:()=> 'test-token'
    };
    const window={AegisAuth:AUTH,innerWidth:1280,events:{},addEventListener(k,f){this.events[k]=f;},dispatchEvent(){}};
    const fetch=async(url,options)=>{
        const number=streams.length+1;
        const stream={number,options,read:null};streams.push(stream);
        let first=true;
        return {ok:true,body:{getReader:()=>({
            read(){if(first){first=false;return Promise.resolve({done:false,value:new TextEncoder().encode('data: '+JSON.stringify({type:'run_start',task_id:'task-'+number,session_id:7,session_title:number===1?'First task title':'Second task title'})+'\n\n')});}
                return new Promise((resolve,reject)=>stream.read={resolve,reject});},cancel(){stream.cancelled=true;return Promise.resolve();}
        })}};
    };
    const context={window,document,fetch,TextDecoder,TextEncoder,AbortController,CustomEvent:class{},
        setTimeout(fn){return timers.push(fn);},clearTimeout(){},setInterval(fn){return timers.push(fn);},clearInterval(){}};
    nodes.awPermSelect=element('select');nodes.awPermSelect.value='approve';
    nodes.awThinkSelect=element('select');nodes.awThinkSelect.value='balanced';
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/js/workspace.js'),'utf8'),context);
    function send(text){nodes.awInput.value=text;nodes.awComposer.events.submit({preventDefault(){}});}
    return {nodes,calls,streams,send,document,window,timers};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('composer has no search switch or sidebar model label and sends with search enabled',async()=>{
    const template=fs.readFileSync(path.join(__dirname,'../templates/agent/aegis_workspace.html'),'utf8');
    assert.doesNotMatch(template,/id="awWebToggle"|id="awModelChip"/);
    const h=harness();await flush();h.send('搜索资料');await flush();
    assert.equal(JSON.parse(h.streams[0].options.body).use_web,true);
    h.streams[0].read.resolve({done:true});await flush();
});

test('stopped stream cleanup cannot cancel a newly submitted task',async()=>{
    const h=harness();await flush();
    h.send('first');await flush();
    assert(h.streams[0].read);
    h.nodes.awBusyStop.events.click();
    h.send('second');await flush();
    assert(h.streams[1].read);
    const error=new Error('aborted old stream');error.name='AbortError';
    h.streams[0].read.reject(error);await flush();
    assert.equal(h.streams[1].options.signal.aborted,false);
    assert.equal(h.nodes.awInput.getAttribute('aria-busy'),'true');
    assert.deepEqual(h.calls.filter(c=>c.url==='/api/agent/cancel').map(c=>c.data.task_id),['task-1']);
    h.streams[1].read.resolve({done:true});await flush();
});

test('late paused event from a stopped task cannot change the new task status',async()=>{
    const h=harness();await flush();
    h.send('first');await flush();h.nodes.awBusyStop.events.click();
    h.send('second');await flush();
    h.streams[0].read.resolve({done:false,value:new TextEncoder().encode('data: {"type":"paused","task_id":"task-1"}\n\n')});
    await flush();
    assert.notEqual(h.nodes.awStatusText.textContent,'已暂停');
    assert.equal(h.streams[1].options.signal.aborted,false);
    h.streams[1].read.resolve({done:true});await flush();
});

test('sidebar title updates as soon as the task starts instead of waiting for completion',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    assert(h.streams[0].read);
    assert.match(h.nodes.awConvList.textContent,/First task title/);
    h.streams[0].read.resolve({done:true});await flush();
});

test('focus refreshes sidebar metadata and unchanged data does not rebuild rows',async()=>{
    const h=harness();await flush();
    const before=h.calls.filter(c=>c.url==='/api/aichat/sessions').length;
    assert.equal(typeof h.window.events.focus,'function');
    const row=h.nodes.awConvList.children[0];
    h.window.events.focus();await flush();
    assert.equal(h.calls.filter(c=>c.url==='/api/aichat/sessions').length,before+1);
    assert.equal(h.nodes.awConvList.children[0],row);
});

test('successful late completion cannot unlock or replace the new task',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    h.nodes.awBusyStop.events.click();h.send('second');await flush();
    h.streams[0].read.resolve({done:true});await flush();
    assert.equal(h.nodes.awInput.getAttribute('aria-busy'),'true');
    assert.equal(h.nodes.awStatusText.textContent,'运行中');
    assert.equal(h.streams[1].options.signal.aborted,false);
    h.streams[1].read.resolve({done:true});await flush();
});

test('stopping preserves the session for the next message before the first answer completes',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    h.nodes.awBusyStop.events.click();h.send('second');await flush();
    assert.equal(JSON.parse(h.streams[1].options.body).session_id,7);
    h.streams[0].read.resolve({done:true});h.streams[1].read.resolve({done:true});await flush();
});

test('visible sidebar refreshes periodically and stops fetching while hidden',async()=>{
    const h=harness();await flush();
    const before=h.calls.filter(c=>c.url==='/api/aichat/sessions').length;
    for(const callback of h.timers) callback();await flush();
    assert.equal(h.calls.filter(c=>c.url==='/api/aichat/sessions').length,before+1);
    h.document.hidden=true;
    for(const callback of h.timers) callback();await flush();
    assert.equal(h.calls.filter(c=>c.url==='/api/aichat/sessions').length,before+1);
});
