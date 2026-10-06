/**
 * 新建对话后欢迎页 —— 竞态与视图回归测试
 *
 * 用户反馈「新建的对话没有欢迎页」。排查结论：resetNew 的 setView("home") 本身是对的，
 * 真正风险是 createSessionNow 的**异步回调**与用户后续操作竞态：
 * 用户点「新建任务」后、建会话返回前，若点了侧边栏某个会话，
 * 回调会把 state.sessionId 改写成新会话并可能把视图拉回欢迎页，
 * 表现为「刚点开的对话/欢迎页莫名消失」。
 *
 * 修复：引入 newIntentSeq 意图序号，openSession 作废未完成的意图，
 *      createSessionNow 回调发现过期时只补列表、不改状态与视图。
 */
function makeEl(tag) {
  const n = {
    tagName:(tag||'div').toUpperCase(), className:'', _text:'', children:[],
    hidden:false, title:'', type:'', attrs:{},
    get textContent(){ return this.children.length?this._text+this.children.map(c=>c.textContent).join(''):this._text; },
    set textContent(v){ this._text=v==null?'':String(v); this.children=[]; },
    appendChild(c){ this.children.push(c); return c; },
    setAttribute(k,v){ this.attrs[k]=String(v); },
    getAttribute(k){ return k in this.attrs?this.attrs[k]:null; },
    addEventListener(){}, querySelector(){return null;}, querySelectorAll(){return [];},
    focus(){},
    classList:{ _s:new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);},
      toggle(c,on){on?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} }
  };
  return n;
}
const nodes={};
['awViewHome','awViewChat','awScroll','awConvList','awCards','awInput','awSend','awMsgs',
 'awTask','awSteps','awTools','awTaskEmpty','awRetry','awBusy','awArch','awArchBody','awArchCount',
 'awStatus','awStatusText','awToast','awOverlay','awSide','awMenuBtn','awWebToggle',
 'awPermSelect','awThinkSelect','awWsChip','awModelChip','awForm','awComposer',
 'awNavNew','awNavSkills','awNavSettings','awBtnSettings','awBtnSkills','awBtnWs',
 'awRetryBtn','awRetryText','awBusyText','awBusyStop'].forEach(id=>{nodes[id]=makeEl('div');});
const cardEls=[];
['explore','build','review','fix'].forEach((k,i)=>{
  const c=makeEl('button');
  c.setAttribute('data-kind',k); c.setAttribute('data-needs-ws', i===0?'0':'1');
  c._badge=makeEl('span');
  c.querySelector=s=>s==='.aw-card-state'?c._badge:null;
  cardEls.push(c);
});
nodes.awCards.querySelectorAll=s=>s==='.aw-card'?cardEls:[];
global.document={getElementById:id=>nodes[id]||null,createElement:makeEl,
  querySelectorAll:()=>[],addEventListener:()=>{}};
global.window={};
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const n=makeEl(t); if(c)n.className=c; if(x!=null)n.textContent=x; return n;};

const elViewHome=$('awViewHome'), elViewChat=$('awViewChat');
function setView(name){
  const isChat=name==='chat';
  elViewHome.classList.toggle('is-active',!isChat);
  elViewChat.classList.toggle('is-active',isChat);
}
const currentView=()=>elViewHome.classList.contains('is-active')?'home':'chat';

let SERVER=[],SESSION=[],nextId=800;
// POST 可控延迟，用来制造竞态
let createDelay=0;
const AUTH={
  get(u){
    if(u.indexOf('/messages')>=0){
      // 注意：路径以 /messages 结尾，split('/').pop() 得到的是 "messages"，
      // 必须用正则从中间提取数字 sid（此前写错导致所有会话都被判为空会话）。
      const m = /\/sessions\/(\d+)\/messages/.exec(u);
      const sid = m ? Number(m[1]) : NaN;
      return Promise.resolve(SESSION[sid] || []);
    }
    return Promise.resolve(JSON.parse(JSON.stringify(SERVER)));
  },
  post(u,b){
    if(u==='/api/aichat/sessions'){
      const d=createDelay;
      return new Promise(res=>setTimeout(()=>{
        const s={session_id:nextId++,session_title:'新对话',is_pinned:0};
        SERVER.unshift(s); SESSION[s.session_id]=[];
        res(Object.assign({},s));
      },d));
    }
    return Promise.resolve({});
  },
  put(){return Promise.resolve({});},
  del(u){const m=/\/sessions\/(\d+)/.exec(u); const sid=m?Number(m[1]):NaN;
    SERVER=SERVER.filter(s=>s.session_id!==sid);return Promise.resolve({});}
};

const state={sessionId:null,sending:false,webEnabled:true,awaitingApproval:false,
  approvalSince:0,hasWorkspace:true,lastPrompt:'',retryable:false,markStopped:null,newIntent:0};
let sessionsCache=[],sessionsReqSeq=0,deletingSessions={},newIntentSeq=0;
const elConvList=$('awConvList');
function sidOf(s){return s.id!=null?s.id:s.session_id;}
function isPinnedOf(s){if(!s)return false;const v=(s.is_pinned!=null)?s.is_pinned:s.isPinned;
  if(v===0||v==='0'||v===false||v==='false')return false;return !!v;}
function toast(){} function setStatus(){} function hideBusy(){} function hideRetry(){}
function resetTask(){} function syncCards(){} function syncSend(){} function autosize(){}
function markCardUsed(){} function addMsg(){}
function buildConvRow(s){
  const sid=sidOf(s);
  const row=el('div','aw-conv'+(String(sid)===String(state.sessionId)?' is-active':''));
  row._sid=sid; return row;
}
function renderSessions(list){
  elConvList.textContent='';
  list=list||[];
  const main=[],arch=[];
  list.forEach(s=>{const sid=sidOf(s);if(sid==null)return;
    if(s.archived)arch.push(s);else main.push(s);});
  main.sort((a,b)=>(isPinnedOf(b)?1:0)-(isPinnedOf(a)?1:0));
  if(!main.length)elConvList.appendChild(el('div','aw-side-empty','还没有会话'));
  else main.forEach(s=>elConvList.appendChild(buildConvRow(s)));
  $('awArchCount').textContent=String(arch.length);
}
function loadSessions(){
  const mySeq=++sessionsReqSeq;
  return AUTH.get('/api/aichat/sessions').then(res=>{
    if(mySeq<sessionsReqSeq)return;
    sessionsCache=res||[];renderSessions(sessionsCache);
  }).catch(()=>{});
}
function renderMessages(l){ if(!$('awMsgs'))return; $('awMsgs').textContent='';
  (l||[]).forEach(m=>{if(m)addMsg(m.message_role==='user'?'user':'assistant',m.message_content||'');}); }
function openSession(sid){
  newIntentSeq++; state.newIntent=newIntentSeq;   // 作废未完成的「新建」意图
  state.sessionId=sid; resetTask();
  return AUTH.get('/api/aichat/sessions/'+encodeURIComponent(sid)+'/messages')
    .then(res=>{
      const msgs=res||[];
      renderMessages(msgs);
      // 空会话显示欢迎页，有消息才进聊天视图
      setView(msgs.length?'chat':'home');
      setStatus('idle','空闲');
      return loadSessions();
    }).catch(()=>{});
}
function createSessionNow(intent){
  if(!AUTH)return Promise.resolve(null);
  return AUTH.post('/api/aichat/sessions',{title:'新对话'}).then(res=>{
    const sid=sidOf(res||{});
    if(sid==null)throw new Error('创建会话失败');
    const stale=(intent!=null && intent!==state.newIntent);
    if(!stale) state.sessionId=sid;
    sessionsCache=[{session_id:sid,session_title:'新对话',is_pinned:0,archived:false}].concat(sessionsCache);
    renderSessions(sessionsCache);
    return loadSessions().then(()=>sid);
  }).catch(()=>loadSessions().then(()=>null));
}
function resetNew(){
  state.newIntent=++newIntentSeq;
  state.sessionId=null;
  if($('awMsgs'))$('awMsgs').textContent='';
  resetTask();hideBusy();hideRetry();
  if($('awCards')){const u=$('awCards').querySelectorAll('.aw-card.is-selected');
    for(let i=0;i<u.length;i++)u[i].classList.remove('is-selected');}
  setView('home'); setStatus('idle','空闲');
  return createSessionNow(state.newIntent);   // 返回 Promise 供测试 await
}

let pass=0,fail=0;
const ck=(n,c,x)=>{if(c){console.log('  PASS  '+n);pass++;}
  else{console.log('  FAIL  '+n+(x?'  → '+x:''));fail++;}};
const rows=()=>elConvList.children.filter(c=>c.className!=='aw-side-empty');
const activeSid=()=>{const r=rows().find(c=>c.className.includes('is-active'));return r?r._sid:null;};

(async function(){
  console.log('=== 场景1：点「新建任务」后显示欢迎页 ===');
  SERVER=[];SESSION=[];nextId=800;sessionsCache=[];sessionsReqSeq=0;
  state.sessionId=null;newIntentSeq=0;state.newIntent=0;createDelay=0;
  elViewHome.className='aw-view is-active'; elViewChat.className='aw-view';
  await loadSessions();
  await resetNew();
  ck('★新建后停在欢迎页', currentView()==='home', currentView());
  ck('★侧边栏出现新会话', rows().length===1, 'got '+rows().length);
  ck('★新会话被高亮', activeSid()===state.sessionId);
  ck('state.sessionId 指向新会话', state.sessionId===SERVER[0].session_id);

  console.log('');
  console.log('=== 场景2：从聊天视图点「新建任务」应回欢迎页 ===');
  SERVER=[{session_id:1,session_title:'旧对话',is_pinned:0}];
  SESSION[1]=[{message_role:'user',message_content:'之前的问题'},
              {message_role:'assistant',message_content:'之前的回答'}];
  nextId=810;
  await loadSessions();
  await openSession(1);
  ck('先进入旧会话的聊天视图', currentView()==='chat');
  await resetNew();
  ck('★点新建后回欢迎页', currentView()==='home', currentView());
  ck('★侧边栏 2 项', rows().length===2, 'got '+rows().length);

  console.log('');
  console.log('=== 场景3：★竞态 —— 新建途中用户点了别的会话 ===');
  SERVER=[{session_id:1,session_title:'旧对话',is_pinned:0}];
  // 旧会话有消息（点开应进聊天视图）
  SESSION[1]=[{message_role:'user',message_content:'之前的问题'},
              {message_role:'assistant',message_content:'之前的回答'}];
  nextId=820; createDelay=200;            // 建会话慢，制造竞态
  await loadSessions();
  await openSession(1);
  ck('起始在旧会话聊天视图', currentView()==='chat', currentView());

  resetNew();                              // 点新建（不 await，故意让建会话在飞）
  await openSession(1);                    // 用户改主意，又点回旧会话
  ck('用户已切到旧会话', currentView()==='chat' && state.sessionId===1);
  await new Promise(r=>setTimeout(r,400)); // 等建会话返回
  ck('★竞态后 state.sessionId 未被覆盖', state.sessionId===1,
     'got '+state.sessionId+'（期望 1）');
  ck('★竞态后视图未被拉回欢迎页', currentView()==='chat', currentView());
  ck('★竞态后新会话仍出现在侧边栏', rows().length===2, 'got '+rows().length);
  ck('★高亮仍跟随当前会话', activeSid()===1, 'got '+activeSid());

  console.log('');
  console.log('=== 场景4：无竞态时正常新建 ===');
  SERVER=[];SESSION=[];nextId=830;sessionsCache=[];sessionsReqSeq=0;
  state.sessionId=null;createDelay=0;
  elViewHome.className='aw-view is-active'; elViewChat.className='aw-view';
  await loadSessions();
  await resetNew();
  ck('无竞态：停在欢迎页', currentView()==='home', currentView());
  ck('无竞态：sessionId 指向新会话', state.sessionId===SERVER[0].session_id);
  ck('无竞态：侧边栏 1 项且高亮', rows().length===1 && activeSid()===state.sessionId);

  console.log('');
  console.log('=== 场景5：连续新建两次，都要有欢迎页与独立项 ===');
  await resetNew();
  ck('第2次新建后仍在欢迎页', currentView()==='home', currentView());
  ck('第2次新建后 2 项', rows().length===2, 'got '+rows().length);
  const ids=SERVER.map(s=>s.session_id);
  ck('两个会话 id 不同', new Set(ids).size===2, ids.join(','));
  console.log('');
  console.log('=== 场景6：★点侧边栏「空会话」也应显示欢迎页（用户反馈的原路径）===');
  SERVER=[];SESSION=[];nextId=900;sessionsCache=[];sessionsReqSeq=0;
  state.sessionId=null;createDelay=0;
  elViewHome.className='aw-view is-active'; elViewChat.className='aw-view';
  await loadSessions();
  // 建一个空会话（刚新建、没发过消息）
  const r=await AUTH.post('/api/aichat/sessions',{title:'新对话'});
  const emptySid=r.session_id;
  SESSION[emptySid]=[];                       // 0 条消息
  await loadSessions();
  ck('侧边栏有该空会话', rows().length===1);
  // 先进入某会话把视图切到 chat
  elViewChat.classList.add('is-active'); elViewHome.classList.remove('is-active');
  ck('预设：当前在聊天视图', currentView()==='chat');
  // 点开这个空会话
  await openSession(emptySid);
  ck('★点开空会话 → 显示欢迎页（不是空白对话区）',
     currentView()==='home', currentView());
  ck('★空会话仍被高亮为当前', activeSid()===emptySid);
  // 有消息的会话仍应进聊天视图
  SESSION[emptySid]=[{message_role:'user',message_content:'hi'},
                     {message_role:'assistant',message_content:'你好'}];
  await loadSessions();
  await openSession(emptySid);
  ck('★有消息的会话仍进聊天视图', currentView()==='chat', currentView());

  console.log('');
  console.log('='.repeat(52));
  console.log('结果： '+pass+' passed, '+fail+' failed');
  process.exit(fail?1:0);
})();
