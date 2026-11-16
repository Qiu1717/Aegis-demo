const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function harness(options={}) {
    const nodes={}, calls=[], streams=[], timers=[];
    let sessionsResult=[];
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
        get:async url=>{calls.push({url,method:'GET'});if(url.endsWith('/sessions'))return sessionsResult;
            if(url.endsWith('/workspace'))return {path:'/test',valid:true};
            if(url.endsWith('/permission'))return options.permissionResult||{mode:defaults.permission};
            if(url.endsWith('/thinking'))return {level:defaults.thinking};return {};},
        post:async(url,data)=>{calls.push({url,method:'POST',data});return {};},
        put:async(url,data)=>{calls.push({url,method:'PUT',data});return {};},getToken:()=> 'test-token'
    };
    const window={AegisAuth:AUTH,innerWidth:1280,events:{},addEventListener(k,f){this.events[k]=f;},dispatchEvent(){}};
    const fetch=async(url,options)=>{
        const number=streams.length+1;
        const stream={number,options,read:null};streams.push(stream);
        let first=true;
        return {ok:true,body:{getReader:()=>({
            read(){if(first){first=false;return Promise.resolve({done:false,value:new TextEncoder().encode('data: '+JSON.stringify({type:'run_start',task_id:'task-'+number,session_id:7,session_title:number===1?'First task title':'Second task title'})+'\n\n')});}
                return new Promise((resolve,reject)=>stream.read={resolve,reject});},cancel(){stream.cancelled=true;return stream.cancelGate||Promise.resolve();}
        })}};
    };
    const context={window,document,fetch,TextDecoder,TextEncoder,AbortController,CustomEvent:class{},
        setTimeout(fn){return timers.push(fn);},clearTimeout(){},setInterval(fn){return timers.push(fn);},clearInterval(){}};
    nodes.awPermSelect=element('select');nodes.awPermSelect.value='approve';
    nodes.awThinkSelect=element('select');nodes.awThinkSelect.value='balanced';
    nodes.awApprovals=element();
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/js/workspace.js'),'utf8'),context);
    function send(text){nodes.awInput.value=text;nodes.awComposer.events.submit({preventDefault(){}});}
    return {nodes,calls,streams,send,document,window,timers,setSessionsResult(value){sessionsResult=value;}};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('a late settings response cannot overwrite the permission the user just selected',async()=>{
    let resolvePermission;
    const h=harness({permissionResult:new Promise(resolve=>{resolvePermission=resolve;})});
    h.nodes.awPermSelect.value='full';h.nodes.awPermSelect.events.change();
    resolvePermission({mode:'approve'});await flush();
    assert.equal(h.nodes.awPermSelect.value,'full');
    h.send('do the task');await flush();
    assert.equal(JSON.parse(h.streams[0].options.body).permission_mode,'full');
    assert.equal(h.calls.find(c=>c.url==='/api/settings/permission'&&c.method==='PUT').data.mode,'full');
    h.streams[0].read.resolve({done:true});await flush();
});

async function emit(stream,event,ending='\n\n') {
    stream.read.resolve({done:false,value:new TextEncoder().encode('data: '+JSON.stringify(event)+ending)});
    await flush();
}

test('live activity follows thinking, actual tool start, command execution and reply output',async()=>{
    const h=harness();await flush();h.send('work');await flush();
    await emit(h.streams[0],{type:'agent_step',step:1,status:'thinking',title:'思考中…'});
    assert.equal(h.nodes.awBusy.hidden,false);assert.match(h.nodes.awBusyText.textContent,/正在思考/);
    await emit(h.streams[0],{type:'tool_start',tool:'read_file',call_id:'read-1'});
    assert.equal(h.nodes.awBusyText.textContent,'正在调用工具：读取文件');
    await emit(h.streams[0],{type:'tool_start',tool:'run_command',call_id:'cmd-1'});
    assert.equal(h.nodes.awBusyText.textContent,'正在运行命令');
    await emit(h.streams[0],{type:'terminal',content:'ok'});
    assert.equal(h.nodes.awBusy.hidden,false);assert.equal(h.nodes.awBusyText.textContent,'正在运行命令');
    await emit(h.streams[0],{type:'chunk',content:'done'});
    assert.equal(h.nodes.awBusy.hidden,false);assert.equal(h.nodes.awBusyText.textContent,'正在生成回复');
    await emit(h.streams[0],{type:'run_end',stats:{stopped_by:'completed'}});
    assert.equal(h.nodes.awBusy.hidden,true);
});

for(const decision of ['allow_once','deny']) test('approval remains actionable outside collapsed task details: '+decision,async()=>{
    const h=harness();await flush();h.send('work');await flush();
    h.document.body.classList.add('aw-task-compact');
    await emit(h.streams[0],{type:'approval_request',request_id:'request-1',title:'修改文件',options:['allow_once','deny']});
    assert.equal(h.nodes.awApprovals.children.length,1);
    const card=h.nodes.awApprovals.children[0];
    const buttons=card.children[card.children.length-1].children;
    assert.deepEqual(buttons.map(b=>b.textContent),['允许一次','拒绝']);
    buttons[decision==='deny'?1:0].events.click();await flush();
    assert.equal(h.calls.find(c=>c.url==='/api/agent/approve').data.decision,decision);
    await emit(h.streams[0],{type:'run_end',stats:{stopped_by:'completed'}});
});

test('run_end releases sending without EOF, a resolved reader cancellation or sidebar refresh',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    h.setSessionsResult(new Promise(()=>{}));
    h.streams[0].cancelGate=new Promise(()=>{});
    await emit(h.streams[0],{type:'run_end',task_id:'task-1',final:'完整回答',stats:{stopped_by:'completed'}});
    assert.equal(h.nodes.awInput.getAttribute('aria-busy'),'false');
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
    h.send('second');await flush();
    assert.equal(h.streams.length,2);
    assert.equal(h.calls.filter(c=>c.url==='/api/agent/cancel').length,0);
    h.streams[1].read.resolve({done:true});await flush();
});

test('EOF completion does not keep sending locked while sidebar request is pending',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    h.setSessionsResult(new Promise(()=>{}));
    h.streams[0].read.resolve({done:true});await flush();
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
    h.send('second');await flush();assert.equal(h.streams.length,2);
    h.streams[1].read.resolve({done:true});await flush();
});

test('done without EOF restores sending and preserves its session ID',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    await emit(h.streams[0],{type:'done',session_id:12});
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
    h.send('second');await flush();
    assert.equal(h.streams.length,2);
    assert.equal(JSON.parse(h.streams[1].options.body).session_id,12);
    h.streams[1].read.resolve({done:true});await flush();
});

test('a terminal error unlocks sending without being marked completed',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    await emit(h.streams[0],{type:'error',code:'SERVER_ERROR',reason:'测试失败'});
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
    assert.notEqual(h.nodes.awTaskStatusText.textContent,'已完成');
    h.send('second');await flush();assert.equal(h.streams.length,2);
    h.streams[1].read.resolve({done:true});await flush();
});

test('CRLF terminal events split across reads release sending',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    await emit(h.streams[0],{type:'run_end',stats:{stopped_by:'completed'}},'\r');
    h.streams[0].read.resolve({done:false,value:new TextEncoder().encode('\n\r\n')});await flush();
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
});

test('three completed turns send directly without an intervening stop action',async()=>{
    const h=harness();await flush();
    for(let i=0;i<3;i++) {
        h.send('question '+i);await flush();assert.equal(h.streams.length,i+1);
        await emit(h.streams[i],{type:'run_end',stats:{stopped_by:'completed'},final:'answer '+i});
        assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
    }
    assert.equal(h.calls.filter(c=>c.url==='/api/agent/cancel').length,0);
});

test('events after run_end in the same network chunk cannot restore a paused state',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    const events=[{type:'run_end',stats:{stopped_by:'completed'}},{type:'paused',task_id:'task-1'}];
    h.streams[0].read.resolve({done:false,value:new TextEncoder().encode(events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join(''))});
    await flush();
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
    assert.notEqual(h.nodes.awStatusText.textContent,'已暂停');
});

test('a rejected stream cancellation cannot keep a completed turn locked',async()=>{
    const h=harness();await flush();h.send('first');await flush();
    h.streams[0].cancelGate=Promise.reject(new Error('reader cancellation failed'));
    await emit(h.streams[0],{type:'run_end',stats:{stopped_by:'completed'}});
    assert.equal(h.nodes.awSend.getAttribute('aria-label'),'发送');
});

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
