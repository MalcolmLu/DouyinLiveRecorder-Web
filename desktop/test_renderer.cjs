"use strict";
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const {JSDOM}=require("jsdom");
const root=path.resolve(__dirname,"..");
const html=fs.readFileSync(path.join(root,"gui/static/index.html"),"utf8")
  .replace("{% if authenticated %}hidden{% endif %}","hidden")
  .replace("{{ login_error | default('') }}","");
const script=fs.readFileSync(path.join(root,"gui/static/app.js"),"utf8");

async function waitFor(check){
  for(let i=0;i<100;i++){if(check())return;await new Promise(r=>setTimeout(r,10));}
  throw new Error("UI state did not settle");
}
function page(desktop,rooms=[],jobs=[]){
  const calls=[];
  const jobState={items:jobs,pageSize:25,failBulk:false};
  jobState.control={supported:true,online:true,mode:"running",state:"encoding",phase:"encoding",
    current_job_id:jobs.find(job=>job.status==="running")?.id || null,control_revision:0,order_revision:1};
  const values={downloads:"/example/recordings",ffmpeg:"",encoder:"auto",bitrate_kbps:4000,cpu_threads:2,
    auto_start:true,close_to_tray:true,start_minimized:false,prevent_sleep:true,recording_roots:["/example/recordings"]};
  const snapshot={rooms,monitoring_count:rooms.length,recording_count:0,queue:{queued:0,running:0,done:0,failed:0,saved_bytes:0},
    recorder:{connected:true},updated:Date.now()/1000,revision:"record-revision",default_quality:"原画",
    encoding:{encoder:"hevc_videotoolbox"},desktop:desktop?{running:false,ffmpeg_ready:true,node_ready:true}:undefined};
  const dom=new JSDOM(html,{url:"http://127.0.0.1:18888/",runScripts:"outside-only",pretendToBeVisual:true});
  const w=dom.window;
  w.confirm=()=>true;
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event("close"));};
  w.HTMLMediaElement.prototype.pause=function(){};
  w.HTMLMediaElement.prototype.load=function(){};
  w.HTMLMediaElement.prototype.play=async function(){this.dispatchEvent(new w.Event("playing"));};
  w.fetch=async (url,options={})=>{
    calls.push({url,options});
    let data={};
    if(url==="/api/session")data={authenticated:true,desktop,csrf:"isolated-ui-csrf",setup_required:false};
    else if(url==="/api/dashboard")data={...snapshot,queue_control:jobState.control};
    else if(url==="/api/desktop"){
      if(options.method==="PUT")Object.assign(values,JSON.parse(options.body).values);
      data=options.method==="PUT"?{ok:true,message:"已保存"}:
        {values,revision:"desktop-revision",status:snapshot.desktop,encoders:[{value:"libx265",label:"CPU"},{value:"hevc_videotoolbox",label:"Apple VideoToolbox"}],
          data_dir:"/example/data",ffmpeg_path:"/example/tools/ffmpeg"};
    }else if(url.endsWith("/sessions")){
      data={items:[{id:"e".repeat(64),name:"测试主播",started:1750000000,ended:1750003600,part_count:3,auto_merge:false,
        status:jobState.mergeRequested?"requested":"ready",can_merge:!jobState.mergeRequested,revision:"merge-revision",skipped:["deleted.ts"]}]};
    }else if(url.endsWith("/merge")){
      jobState.mergeRequested=true;data={ok:true,message:"合并已提交"};
    }else if(url.includes("/recordings?")){
      const folder=new URL(url,w.location.href).searchParams.get("folder") || "0/B站直播/测试主播";
      const child=folder.endsWith("/2026-10-10");
      data={room:{id:rooms[0]?.id,name:"测试主播"},folder,parent:child?"0/B站直播/测试主播":null,
        breadcrumbs:[{name:"测试主播",path:"0/B站直播/测试主播"}],total:child?1:2,page:1,page_size:25,
        items:[{kind:"file",name:"中文 & <测试>.mp4",path:folder+"/中文 & <测试>.mp4",size:1024,modified:1750000000,format:"MP4"},
          ...(!child?[{kind:"folder",name:"2026-10-10",path:folder+"/2026-10-10",modified:1750000000}]:[])]};
    }else if(url==="/api/queue/control"){
      const request=JSON.parse(options.body),control=jobState.control;
      control.mode={pause:"paused",resume:"running",stop:"stopped"}[request.action];
      control.state=control.phase=request.action==="resume"?"encoding":control.mode;
      control.control_revision++;
      if(request.action==="stop"){
        const job=jobState.items.find(job=>job.id===control.current_job_id);if(job)job.status="stopped";
        control.current_job_id=null;
      }
      data={ok:true,control,message:"转码指令已提交"};
    }else if(url==="/api/queue/concurrency"){
      const request=JSON.parse(options.body),control=jobState.control;
      control.max_workers=request.max_workers;control.control_revision++;
      data={ok:true,control,message:"并行任务数已更新"};
    }else if(url==="/api/queue/order"){
      if(jobState.failOrder)return {ok:false,status:409,json:async()=>({error:{code:"stale_queue",message:"队列状态已变化，请刷新后再操作。"}})};
      const request=JSON.parse(options.body),queued=jobState.items.filter(job=>job.status==="queued");
      if(request.sort){
        queued.sort((a,b)=>request.sort==="smallest"?a.size-b.size:request.sort==="largest"?b.size-a.size:
          request.sort==="name"?a.name.localeCompare(b.name):request.sort==="newest"?b.created-a.created:a.created-b.created);
      }else{
        const index=queued.findIndex(job=>job.id===request.job_id);
        const target={top:0,up:Math.max(0,index-1),down:Math.min(queued.length-1,index+1),bottom:queued.length-1}[request.move];
        queued.splice(target,0,queued.splice(index,1)[0]);
      }
      let index=0;jobState.items=jobState.items.map(job=>job.status==="queued"?queued[index++]:job);
      jobState.control.order_revision++;
      data={ok:true,control:jobState.control,message:"实际转码顺序已更新"};
    }else if(/^\/api\/jobs\/[0-9a-f]{64}\/start-now$/.test(url)){
      const id=url.split('/').at(-2),job=jobState.items.find(job=>job.id===id);
      jobState.items=[job,...jobState.items.filter(item=>item.id!==id)];
      jobState.control.priority_job_id=id;jobState.control.order_revision++;
      data={ok:true,control:jobState.control,message:'已请求优先开始'};
    }else if(/^\/api\/library\/jobs\/[0-9a-f]{64}\/start-now$/.test(url)){
      const id=url.split('/').at(-2),job=jobState.library.items.find(job=>job.id===id);
      jobState.library.items=[job,...jobState.library.items.filter(item=>item.id!==id)];
      jobState.library.control.priority_job_id=id;jobState.library.control.order_revision++;
      data={ok:true,control:jobState.library.control,message:'已请求优先开始'};
    }else if(url.startsWith('/api/library/jobs?')){
      data=jobState.library || {enabled:false};
      if(data.enabled){
        const query=new URL(url,w.location.href).searchParams, status=query.get('status');
        const items=data.items.filter(job=>(status==='all' || job.status===status) && job.name.includes(query.get('search') || ''));
        const sort=query.get('sort'),direction=query.get('direction'),page=Number(query.get('page') || 1);
        if(sort)items.sort((a,b)=>{
          const result=sort==='size'?(a.size||0)-(b.size||0):sort==='mtime'?(a.modified||0)-(b.modified||0):a.name.localeCompare(b.name,'zh-CN');
          return direction==='desc'?-result:result;
        });
        data={...data,items:items.slice((page-1)*data.page_size,page*data.page_size),total:items.length,page};
      }
    }else if(url==='/api/library/order'){
      const request=JSON.parse(options.body),control=jobState.library.control;
      jobState.libraryOrder=request.sort;
      control.order_revision=(control.order_revision||0)+1;
      data={ok:true,control,message:'历史后台转码顺序已更新'};
    }else if(url==='/api/library/jobs/delete-records'){
      if(jobState.failLibraryBulk)return {ok:false,status:409,json:async()=>({error:{message:'任务正在重试，请刷新。'}})};
      const ids=JSON.parse(options.body).ids;
      jobState.library.items=jobState.library.items.filter(job=>!ids.includes(job.id));
      data={ok:true,deleted:ids.length,message:'记录已删除，视频保留'};
    }else if(url==='/api/library/control'){
      const request=JSON.parse(options.body),control=jobState.library.control;
      control.mode={pause:'paused',resume:'running',stop:'stopped'}[request.action];
      control.state=control.phase=request.action==='resume'?'waiting_priority':control.mode;
      control.control_revision++;
      data={ok:true,control,message:'后台控制已提交'};
    }else if(url==='/api/library/concurrency'){
      const request=JSON.parse(options.body),control=jobState.library.control;
      control.max_workers=request.max_workers;control.control_revision++;
      data={ok:true,control,message:'后台并行任务数已更新'};
    }else if(url.endsWith('/retry') && url.startsWith('/api/library/jobs/')){
      jobState.library.items[0].retry_requested=true;data={ok:true,message:'已提交重试'};
    }else if(url.startsWith('/api/library/jobs/') && options.method==='DELETE'){
      jobState.library.items=[];jobState.library.total=0;data={ok:true,message:'记录已删除，视频保留'};
    }else if(url==="/api/jobs/delete-records"){
      if(jobState.failBulk)return {ok:false,status:409,json:async()=>({error:{code:"conflict",message:"任务已重新排队，请刷新后重试。"}})};
      const ids=JSON.parse(options.body).ids;
      jobState.items=jobState.items.filter(job=>!ids.includes(job.id));
      data={ok:true,deleted:ids.length,message:"已删除所选记录，录像文件已保留。"};
    }else if(/^\/api\/jobs\/[0-9a-f]{64}$/.test(url) && options.method==="DELETE"){
      jobState.items=jobState.items.filter(job=>job.id!==url.split("/").at(-1));
      data={ok:true,message:"转码记录已删除，录像文件已保留。"};
    }else if(url.startsWith("/api/jobs?")){
      const params=new URL(url,w.location.href).searchParams;
      const status=params.get("status"),search=params.get("search"),number=Number(params.get("page"));
      const filtered=jobState.items.filter(job=>(status==="all" || job.status===status) && (!search || job.name.includes(search)));
      const queued=jobState.items.filter(job=>job.status==="queued");
      data={items:filtered.slice((number-1)*jobState.pageSize,number*jobState.pageSize).map(job=>({...job,
        queue_position:job.status==="queued"?queued.findIndex(item=>item.id===job.id)+1:null,queued_total:queued.length})),
        total:filtered.length,page:number,page_size:jobState.pageSize,control:jobState.control,order_revision:jobState.control.order_revision};
    }
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(data))};
  };
  w.recorderDesktop={chooseFolder:async()=>"/example/new-recordings",chooseFFmpeg:async()=>"/example/bin/ffmpeg",
    preferencesChanged:async()=>true,openRecordings:async()=>true,openLogs:async()=>true};
  w.eval(script);
  return {dom,w,calls,jobState,snapshot};
}
test('queue combines storage savings in GB and shows actual task runtime with honest legacy fallbacks',async()=>{
  const jobs=[queueJob(1),queueJob(2,'running'),queueJob(3,'queued')];
  jobs[0].runtime_seconds=3723; jobs[1].runtime_seconds=null;
  const {dom,w,jobState,snapshot}=page(false,[],jobs);
  snapshot.queue.saved_bytes=1_500_000_000;
  const historical=[{...queueJob(4),runtime_seconds:65.9},{...queueJob(5),already_hevc:true}];
  jobState.library={enabled:true,label:'历史录像',items:historical,total:2,page:1,page_size:25,
    summary:{done:2,saved_bytes:250_000_000},already_hevc:1,sidecars_retained:0,log:'test.log',
    control:{supported:true,online:true,mode:'running',phase:'idle'}};
  try{
    await openQueue(w,3);
    await waitFor(()=>w.document.querySelector('#queue-total-saved').textContent==='1.75 GB');
    assert.match(w.document.querySelector('#queue-summary').textContent,/1.50 GB/);
    assert.match(w.document.querySelector('#library-summary').textContent,/0.25 GB/);
    assert.deepEqual([...w.document.querySelectorAll('#job-table .job-runtime')].map(c=>c.textContent),
      ['1 小时 2 分 3 秒','未记录','—']);
    assert.deepEqual([...w.document.querySelectorAll('#library-table .job-runtime')].map(c=>c.textContent),
      ['1 分 5 秒','无需转码']);
    const filter=w.document.querySelector('#library-filter');filter.value='failed';filter.dispatchEvent(new w.Event('change'));
    await waitFor(()=>w.document.querySelector('#library-table td')?.colSpan===8);
    assert.equal(w.document.querySelector('#queue-total-saved').textContent,'1.75 GB');
  }finally{dom.window.close();}
});
test('historical retention explains a discarded larger output and allows an authenticated retry',async()=>{
  const {dom,w,jobState,calls}=page(false,[],[]);
  const job={...queueJob(9),retained_original:true,candidate_size:4096,runtime_seconds:65};
  jobState.library={enabled:true,label:'历史录像',items:[job],total:1,page:1,page_size:25,
    summary:{done:1,saved_bytes:0},already_hevc:0,retained_original:1,sidecars_retained:0,log:'test.log',
    control:{supported:true,online:true,mode:'running',phase:'idle'}};
  try{
    await openQueue(w,0);
    await waitFor(()=>w.document.querySelector('#library-table').textContent.includes('原片保留'));
    const row=w.document.querySelector('#library-table tr');
    assert.match(row.textContent,/转码后未减小/);
    assert.match(row.textContent,/未采用/);
    assert.match(row.textContent,/转码结果 4 KB/);
    assert.match(w.document.querySelector('#library-detail').textContent,/1 个任务转码后未减小/);
    [...row.querySelectorAll('button')].find(b=>b.textContent==='重试').click();
    await waitFor(()=>calls.some(c=>c.url.endsWith('/library/jobs/'+job.id+'/retry')));
    assert.equal(calls.find(c=>c.url.endsWith('/library/jobs/'+job.id+'/retry')).options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    await waitFor(()=>w.document.querySelector('#library-table').textContent.includes('等待检查并重新排队'));
    assert.equal(w.document.querySelector('#library-table input'),null);
  }finally{dom.window.close();}
});
test("desktop navigation, native directory selection, and authenticated settings payload",async()=>{
  const {dom,w,calls}=page(true);
  try{
    await waitFor(()=>!w.document.querySelector("#app-view").hidden);
    assert.equal(w.document.querySelector("#desktop-nav").hidden,false);
    assert.equal(w.document.querySelector("#logout").hidden,true);
    w.document.querySelector("#desktop-nav").click();
    await waitFor(()=>w.document.querySelector("#desktop-form").elements.downloads.value==="/example/recordings");
    assert.equal(w.document.querySelector("#page-desktop").hidden,false);
    w.document.querySelector("#choose-downloads").click();
    await waitFor(()=>w.document.querySelector("#desktop-form").elements.downloads.value==="/example/new-recordings");
    const form=w.document.querySelector("#desktop-form");
    const submitter=form.querySelector("button[type=submit]");
    form.dispatchEvent(new w.SubmitEvent("submit",{bubbles:true,cancelable:true,submitter}));
    await waitFor(()=>calls.some(call=>call.url==="/api/desktop" && call.options.method==="PUT"));
    const saved=calls.find(call=>call.url==="/api/desktop" && call.options.method==="PUT");
    assert.equal(saved.options.headers["X-CSRF-Token"],"isolated-ui-csrf");
    const request=JSON.parse(saved.options.body);
    assert.equal(request.revision,"desktop-revision");
    assert.equal(request.values.downloads,"/example/new-recordings");
    assert.equal(request.values.close_to_tray,true);
    assert.equal(request.values.recording_roots,undefined);
    await waitFor(()=>!submitter.disabled);
  }finally{dom.window.close();}
});
test("NAS view hides desktop controls and preserves the existing pages",async()=>{
  const {dom,w}=page(false);
  try{
    await waitFor(()=>!w.document.querySelector("#app-view").hidden);
    assert.equal(w.document.querySelector("#desktop-nav").hidden,true);
    assert.equal(w.document.querySelector("#logout").hidden,false);
    assert.equal(w.document.querySelector("#workspace-name").textContent,"我的 NAS");
  }finally{dom.window.close();}
});
test("recordings button opens recordings, plays encoded file URLs, navigates folders, and stops on close",async()=>{
  const room={id:"example-room",display_name:"测试主播",name:"测试主播",platform:"哔哩哔哩",url:"https://live.bilibili.com/100001",
    state:"offline",quality:"原画",enabled:true,uses_proxy:false};
  const {dom,w,calls}=page(false,[room]);
  try{
    await waitFor(()=>w.document.querySelector(".room-recordings"));
    w.document.querySelector(".room-recordings").click();
    await waitFor(()=>w.document.querySelector(".recording-file-name"));
    assert.equal(w.document.querySelector("#recordings-dialog").open,true);
    const file=w.document.querySelector(".recording-file-name");
    assert.equal(file.textContent,"中文 & <测试>.mp4");
    assert.equal(file.querySelector("测试"),null);
    file.click();
    await waitFor(()=>w.document.querySelector("#recording-player").hasAttribute("src"));
    const player=w.document.querySelector("#recording-player");
    assert.equal(new URL(player.src).searchParams.get("path"),"0/B站直播/测试主播/中文 & <测试>.mp4");
    assert.equal(player.controls,true);
    assert.equal(new URL(w.document.querySelector("#recording-download").href).searchParams.get("download"),"1");
    player.dispatchEvent(new w.Event("error"));
    assert.match(w.document.querySelector("#recording-playback-status").textContent,/下载/);
    [...w.document.querySelectorAll(".recording-file-name")].find(b=>b.textContent.includes("2026-10-10")).click();
    await waitFor(()=>calls.some(c=>new URL(c.url,w.location.href).searchParams.get("folder")==="0/B站直播/测试主播/2026-10-10"));
    await waitFor(()=>w.document.querySelector("#recordings-list").children.length===1);
    assert.equal(player.hasAttribute("src"),false);
    w.document.querySelector("#recordings-up").click();
    await waitFor(()=>w.document.querySelector("#recordings-list").children.length===2);
    w.document.querySelector(".recording-file-name").click();
    await waitFor(()=>player.hasAttribute("src"));
    w.document.querySelector("#close-recordings").click();
    assert.equal(w.document.querySelector("#recordings-dialog").open,false);
    assert.equal(player.hasAttribute("src"),false);
  }finally{dom.window.close();}
});

function queueJob(index,status="done"){
  return {id:index.toString(16).padStart(64,"0"),name:`录像 ${index}.ts`,path:`测试主播/录像 ${index}.ts`,created:index,
    status,size:1000,output_size:status==="done"?500:null,saved_percent:status==="done"?50:null,error:"测试失败",
    modified:1750000000+index};
}
async function openQueue(w,count){
  await waitFor(()=>w.document.querySelector("#metric-rooms").textContent==="0");
  w.document.querySelector('[data-page="jobs"]').click();
  await waitFor(()=>w.document.querySelectorAll(".job-checkbox").length===count);
}
test("bulk selection excludes active work, confirms once, and sends an authenticated batch",async()=>{
  const jobs=[queueJob(1),queueJob(2,"failed"),queueJob(3,"queued"),queueJob(4,"running"),queueJob(5,"published")];
  const {dom,w,calls,jobState}=page(false,[],jobs);
  try{
    await openQueue(w,5);
    assert.equal(w.document.querySelectorAll(".job-checkbox:disabled").length,3);
    const all=w.document.querySelector("#select-all-jobs");all.click();
    assert.equal(w.document.querySelectorAll(".job-checkbox:checked").length,2);
    assert.equal(w.document.querySelector("#job-selection-count").textContent,"已选择 2 条");
    w.document.querySelector(".job-checkbox").click();
    assert.equal(all.indeterminate,true);
    all.click();
    const confirmations=[];w.confirm=text=>{confirmations.push(text);return false;};
    w.document.querySelector("#delete-selected-jobs").click();
    assert.equal(calls.some(call=>call.url==="/api/jobs/delete-records"),false);
    assert.match(confirmations[0],/录像.*保留/);
    w.confirm=()=>true;
    w.document.querySelector("#delete-selected-jobs").click();
    w.document.querySelector("#delete-selected-jobs").click();
    await waitFor(()=>w.document.querySelectorAll(".job-checkbox").length===3);
    const batches=calls.filter(call=>call.url==="/api/jobs/delete-records");assert.equal(batches.length,1);
    assert.deepEqual(JSON.parse(batches[0].options.body).ids,jobs.slice(0,2).map(job=>job.id));
    assert.equal(batches[0].options.headers["X-CSRF-Token"],"isolated-ui-csrf");
    assert.deepEqual(jobState.items.map(job=>job.status),["queued","running","published"]);
    assert.equal(w.document.querySelector("#job-selection-count").textContent,"已选择 0 条");
  }finally{dom.window.close();}
});
test("bulk selection is limited to the current page and clears when filters change",async()=>{
  const jobs=Array.from({length:26},(_,i)=>queueJob(i+1,i===25?"failed":"done"));
  const {dom,w}=page(false,[],jobs);
  try{
    await openQueue(w,25);w.document.querySelector("#select-all-jobs").click();
    assert.equal(w.document.querySelectorAll(".job-checkbox:checked").length,25);
    w.document.querySelector("#next-page").click();
    await waitFor(()=>w.document.querySelectorAll(".job-checkbox").length===1);
    assert.equal(w.document.querySelectorAll(".job-checkbox:checked").length,0);
    w.document.querySelector("#select-all-jobs").click();
    const filter=w.document.querySelector("#job-filter");filter.value="failed";filter.dispatchEvent(new w.Event("change"));
    await waitFor(()=>w.document.querySelector("#page-label").textContent==="第 1 页 / 共 1 页");
    assert.equal(w.document.querySelectorAll(".job-checkbox:checked").length,0);
    w.document.querySelector("#select-all-jobs").click();
    w.document.querySelector("#clear-job-selection").click();
    assert.equal(w.document.querySelector("#delete-selected-jobs").disabled,true);
  }finally{dom.window.close();}
});
test("refresh prunes retried records and failed batches retain the other selections",async()=>{
  const {dom,w,jobState}=page(false,[],[queueJob(1),queueJob(2,"failed")]);
  try{
    await openQueue(w,2);w.document.querySelector("#select-all-jobs").click();
    jobState.items[1].status="queued";
    w.document.querySelector("#refresh-jobs").click();
    await waitFor(()=>w.document.querySelector("#job-selection-count").textContent==="已选择 1 条");
    assert.equal(w.document.querySelectorAll(".job-checkbox:disabled").length,1);
    jobState.failBulk=true;w.document.querySelector("#delete-selected-jobs").click();
    await waitFor(()=>!w.document.querySelector("#delete-selected-jobs").disabled);
    assert.equal(w.document.querySelector("#job-selection-count").textContent,"已选择 1 条");
    assert.equal(jobState.items.length,2);
  }finally{dom.window.close();}
});
test("native password form is discoverable and JavaScript does not clear or intercept submission",async()=>{
  const {dom,w,calls}=page(false);
  try{
    await waitFor(()=>!w.document.querySelector("#app-view").hidden);
    const form=w.document.querySelector("#login-form"),password=form.elements.password;
    assert.equal(form.method,"post");assert.equal(new URL(form.action).pathname,"/login");
    assert.equal(form.elements.username.autocomplete,"username");
    assert.equal(form.elements.username.value,"admin");assert.equal(form.elements.username.readOnly,true);
    assert.equal(password.autocomplete,"current-password");
    password.value="Synthetic-Form-Password-42";
    const event=new w.SubmitEvent("submit",{bubbles:true,cancelable:true,submitter:form.querySelector('button[type="submit"]')});
    form.dispatchEvent(event);
    assert.equal(event.defaultPrevented,false);assert.equal(password.value,"Synthetic-Form-Password-42");
    assert.equal(calls.some(call=>call.url==="/api/login"),false);
  }finally{dom.window.close();}
});
test("room names link to live pages while the recordings button remains separate",async()=>{
  const common={id:"example-room",display_name:"测试 <直播间>",name:"测试",platform:"哔哩哔哩",
    state:"offline",quality:"原画",enabled:true,uses_proxy:false};
  const {dom,w}=page(false,[{...common,url:"https://live.bilibili.com/100001"},{...common,id:"invalid-room",url:"javascript:alert(1)"}]);
  try{
    await waitFor(()=>w.document.querySelectorAll(".room-live-link").length===2);
    const links=w.document.querySelectorAll(".room-live-link");
    assert.equal(links[0].href,"https://live.bilibili.com/100001");assert.equal(links[0].target,"_blank");
    assert.equal(links[0].rel,"noopener noreferrer");assert.equal(links[0].textContent,"测试 <直播间>");
    assert.equal(links[0].children.length,0);assert.equal(links[1].hasAttribute("href"),false);
    assert.equal(w.document.querySelectorAll(".room-recordings").length,2);
  }finally{dom.window.close();}
});
test("queue controls pause and resume current work, confirm stop, and keep stopped tasks visible",async()=>{
  const jobs=[queueJob(1,"running"),queueJob(2,"queued")];
  const {dom,w,calls}=page(false,[],jobs);
  try{
    await openQueue(w,2);
    w.document.querySelector("#queue-pause").click();w.document.querySelector("#queue-pause").click();
    await waitFor(()=>w.document.querySelector("#queue-control-state").textContent==="转码已暂停" && !w.document.querySelector("#queue-resume").disabled);
    assert.equal(calls.filter(call=>call.url==="/api/queue/control").length,1);
    assert.equal(w.document.querySelector(".status.paused").textContent,"已暂停");
    w.document.querySelector("#queue-resume").click();
    await waitFor(()=>!w.document.querySelector("#queue-pause").disabled);
    w.confirm=()=>false;w.document.querySelector("#queue-stop").click();
    assert.equal(calls.filter(call=>call.url==="/api/queue/control").length,2);
    let prompt="";w.confirm=text=>{prompt=text;return true;};w.document.querySelector("#queue-stop").click();
    await waitFor(()=>w.document.querySelector(".status.stopped") && !w.document.querySelector("#queue-resume").disabled);
    assert.match(prompt,/原录像.*保留/);assert.match(prompt,/从头转码/);
    const stopped=calls.filter(call=>call.url==="/api/queue/control").at(-1);
    assert.equal(JSON.parse(stopped.options.body).current_job_id,jobs[0].id);
    assert.equal(stopped.options.headers["X-CSRF-Token"],"isolated-ui-csrf");
    assert.equal(w.document.querySelector("#queue-stop").disabled,true);
    assert.equal([...w.document.querySelectorAll("button")].some(button=>button.textContent==="重新排队"),true);
  }finally{dom.window.close();}
});
test("waiting jobs expose global ranks, boundary-aware moves, and authenticated preset sorting",async()=>{
  const jobs=[{...queueJob(1,"queued"),size:300},{...queueJob(2,"queued"),size:100},{...queueJob(3,"queued"),size:200},queueJob(4,"running")];
  const {dom,w,calls,jobState}=page(false,[],jobs);
  try{
    await openQueue(w,4);
    const row=id=>w.document.querySelector(`tr[data-job-id="${id}"]`);
    assert.equal(row(jobs[0].id).querySelector('[data-move="top"]').disabled,true);
    assert.equal(row(jobs[2].id).querySelector('[data-move="down"]').disabled,true);
    assert.equal(row(jobs[3].id).querySelector(".queue-order-actions"),null);
    row(jobs[2].id).querySelector('[data-move="top"]').click();
    await waitFor(()=>!w.document.querySelector("#queue-apply-sort").disabled && jobState.items[0].id===jobs[2].id);
    assert.equal(row(jobs[2].id).querySelector(".queue-order-actions span").textContent,"第 1 项");
    w.document.querySelector("#queue-sort").value="smallest";w.document.querySelector("#queue-apply-sort").click();
    await waitFor(()=>!w.document.querySelector("#queue-apply-sort").disabled && jobState.items[0].id===jobs[1].id);
    const orders=calls.filter(call=>call.url==="/api/queue/order");
    assert.equal(orders.length,2);assert.equal(orders[0].options.headers["X-CSRF-Token"],"isolated-ui-csrf");
    assert.equal(JSON.parse(orders[1].options.body).sort,"smallest");
    jobState.failOrder=true;w.document.querySelector("#queue-apply-sort").click();
    await waitFor(()=>!w.document.querySelector("#queue-apply-sort").disabled && w.document.querySelector("#toast").textContent.includes("队列状态已变化"));
    assert.equal(jobState.items[0].id,jobs[1].id);
  }finally{dom.window.close();}
});
test('queued files can start now while the current file keeps its progress',async()=>{
  const jobs=[{...queueJob(1,'running'),queue_position:null},queueJob(2,'queued'),queueJob(3,'queued')];
  const {dom,w,calls,jobState}=page(false,[],jobs);
  try{
    await openQueue(w,3);
    const row=id=>w.document.querySelector(`tr[data-job-id="${id}"]`);
    const start=row(jobs[2].id).querySelector('button[aria-label^="开始转码"]');
    assert.ok(start);start.click();
    await waitFor(()=>calls.some(c=>c.url===`/api/jobs/${jobs[2].id}/start-now`));
    const request=calls.find(c=>c.url===`/api/jobs/${jobs[2].id}/start-now`);
    assert.equal(request.options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    assert.deepEqual(JSON.parse(request.options.body),{revision:jobState.control.order_revision-1});
    assert.equal(jobState.items.find(job=>job.id===jobs[0].id).status,'running');
    await waitFor(()=>calls.filter(c=>c.url.startsWith('/api/jobs?')).length>=2);
    assert.equal(row(jobs[0].id).querySelector('.status').textContent,'转码中');
  }finally{dom.window.close();}
});
test("offline queue controls remain disabled and stopped records can be batch-selected",async()=>{
  const {dom,w,jobState,calls}=page(false,[],[queueJob(1,"stopped"),queueJob(2),queueJob(3,"queued")]);
  jobState.control.online=false;jobState.control.state=jobState.control.phase="offline";
  try{
    await openQueue(w,3);
    for(const selector of ["#queue-pause","#queue-resume","#queue-stop","#queue-apply-sort"])assert.equal(w.document.querySelector(selector).disabled,true);
    assert.equal(w.document.querySelector('[data-move="top"]').disabled,true);
    assert.equal(w.document.querySelector("#queue-control-state").textContent,"转码控制未连接");
    w.document.querySelector("#select-all-jobs").click();
    assert.equal(w.document.querySelectorAll(".job-checkbox:checked").length,2);
    assert.equal(calls.some(call=>call.url.startsWith("/api/queue/")),false);
  }finally{dom.window.close();}
});
test("stopped task exposes a direct delete button that preserves media and authenticates the request",async()=>{
  const stopped=queueJob(1,"stopped");
  const {dom,w,calls,jobState}=page(false,[],[stopped,queueJob(2,"queued")]);
  try{
    await openQueue(w,2);
    const row=w.document.querySelector(`tr[data-job-id="${stopped.id}"]`);
    const remove=[...row.querySelectorAll("button")].find(button=>button.textContent==="删除记录");
    assert.equal(remove.disabled,false);
    let prompt="";w.confirm=text=>{prompt=text;return false;};remove.click();
    assert.equal(calls.some(call=>call.url==="/api/jobs/"+stopped.id),false);
    assert.match(prompt,/录像.*保留/);
    await waitFor(()=>!remove.disabled);
    w.confirm=()=>true;remove.click();
    await waitFor(()=>w.document.querySelectorAll(".job-checkbox").length===1);
    const request=calls.find(call=>call.url==="/api/jobs/"+stopped.id);
    assert.equal(request.options.method,"DELETE");assert.equal(request.options.headers["X-CSRF-Token"],"isolated-ui-csrf");
    assert.equal(jobState.items[0].status,"queued");
  }finally{dom.window.close();}
});
test("room exception keeps manual merge available and submits session revision with CSRF",async()=>{
  const room={id:"merge-room",name:"测试主播",display_name:"测试主播",url:"https://live.bilibili.com/100001",platform:"哔哩哔哩",quality:"原画",enabled:true,state:"offline",auto_merge:false};
  const {dom,w,calls}=page(false,[room]);
  try{
    await waitFor(()=>w.document.querySelector(".room-merge"));
    w.document.querySelector(".room-merge").click();
    await waitFor(()=>w.document.querySelector("#merge-sessions button"));
    assert.equal(w.document.querySelector("#merge-dialog").open,true);
    assert.match(w.document.querySelector("#merge-sessions").textContent,/已跳过 1 个已删除/);
    w.document.querySelector("#merge-sessions button").click();
    await waitFor(()=>calls.some(call=>call.url.endsWith("/merge")));
    const request=calls.find(call=>call.url.endsWith("/merge"));
    assert.equal(request.options.method,"POST");assert.equal(request.options.headers["X-CSRF-Token"],"isolated-ui-csrf");
    assert.equal(JSON.parse(request.options.body).revision,"merge-revision");
    w.document.querySelector("#close-merges").click();
    const edit=[...w.document.querySelectorAll("#room-table button")].find(button=>button.textContent==="编辑");edit.click();
    assert.equal(w.document.querySelector("#room-form").elements.auto_merge.checked,false);
    assert.equal(w.document.querySelector("#settings-form").elements.merge_parts.checked,true);
  }finally{dom.window.close();}
});

test('historical queue shows priority suspension, safe filenames, failure reasons and authenticated independent controls',async()=>{
  const {dom,w,calls,jobState}=page(false);
  try{
    await waitFor(()=>calls.some(c=>c.url==='/api/dashboard'));
    const job={id:'9'.repeat(64),name:'<img src=x onerror=alert(1)>.mp4',path:'历史录像/<video>.mp4',status:'running',size:2048,output_size:null,error:null,already_hevc:false,retry_requested:false};
    jobState.library={enabled:true,label:'录播姬历史录像',items:[job],total:1,page:1,page_size:25,
      summary:{running:1,queued:3,done:2,failed:0},already_hevc:1,sidecars_retained:1,log:'library/h265/postprocess.log',
      progress:{job_id:job.id,percent:35.2},control:{supported:true,online:true,mode:'running',phase:'waiting_priority',state:'waiting_priority',current_job_id:job.id,control_revision:0}};
    w.document.querySelector('[data-page="jobs"]').click();
    await waitFor(()=>!w.document.querySelector('#library-queue').hidden);
    assert.match(w.document.querySelector('#library-state').textContent,/等待直播任务让出空位/);
    assert.match(w.document.querySelector('#library-table').textContent,/进度保留/);
    assert.equal(w.document.querySelector('#library-table img'),null);
    assert.equal(w.document.querySelector('#library-table progress').value,35.2);
    w.document.querySelector('#library-pause').click();
    await waitFor(()=>w.document.querySelector('#library-state').textContent==='后台转码已暂停');
    w.document.querySelector('#library-resume').click();
    await waitFor(()=>w.document.querySelector('#library-state').textContent==='等待直播任务让出空位');
    const commands=calls.filter(c=>c.url==='/api/library/control');assert.equal(commands.length,2);
    assert.equal(commands[0].options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    assert.equal(calls.some(c=>c.url==='/api/queue/control'),false);
    job.status='failed';job.error='FFprobe failed: moov atom not found';
    w.document.querySelector('#refresh-jobs').click();
    await waitFor(()=>[...w.document.querySelectorAll('#library-table button')].some(b=>b.textContent==='查看原因'));
    [...w.document.querySelectorAll('#library-table button')].find(b=>b.textContent==='查看原因').click();
    assert.match(w.document.querySelector('#error-detail').textContent,/moov atom not found/);
    [...w.document.querySelectorAll('#library-table button')].find(b=>b.textContent==='重试').click();
    await waitFor(()=>job.retry_requested);
    assert.equal(calls.find(c=>c.url.endsWith('/library/jobs/'+job.id+'/retry')).options.headers['X-CSRF-Token'],'isolated-ui-csrf');
  }finally{dom.window.close();}
});

test('both concurrency controls send independent revision-guarded requests',async()=>{
  const {dom,w,calls,jobState}=page(false);
  jobState.control.max_workers=1;jobState.control.control_revision=4;
  jobState.library={enabled:true,label:'历史录像',items:[],total:0,page:1,page_size:25,
    summary:{queued:0},already_hevc:0,sidecars_retained:0,log:'test.log',
    control:{supported:true,online:true,mode:'running',phase:'idle',control_revision:7,max_workers:1}};
  try{
    await openQueue(w,0);
    await waitFor(()=>!w.document.querySelector('#library-queue').hidden);
    assert.match(w.document.querySelector('#queue-concurrency-note').textContent,/最多同时处理 3 个任务/);
    assert.match(w.document.querySelector('#library-concurrency-note').textContent,/核验期间可开始下一个/);
    const main=w.document.querySelector('#queue-concurrency');
    main.value='3';main.dispatchEvent(new w.Event('change'));
    await waitFor(()=>calls.some(c=>c.url==='/api/queue/concurrency'));
    const a=calls.find(c=>c.url==='/api/queue/concurrency');
    assert.deepEqual(JSON.parse(a.options.body),{max_workers:3,revision:4});
    assert.equal(a.options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    const history=w.document.querySelector('#library-concurrency');
    history.value='3';history.dispatchEvent(new w.Event('change'));
    await waitFor(()=>calls.some(c=>c.url==='/api/library/concurrency'));
    const b=calls.find(c=>c.url==='/api/library/concurrency');
    assert.deepEqual(JSON.parse(b.options.body),{max_workers:3,revision:7});
    assert.equal(b.options.headers['X-CSRF-Token'],'isolated-ui-csrf');
  }finally{dom.window.close();}
});


test('historical tasks sort the list and can apply that order to waiting jobs',async()=>{
  const {dom,w,calls,jobState}=page(false);
  try{
    await waitFor(()=>calls.some(c=>c.url==='/api/dashboard'));
    const jobs=[
      {id:'1'.repeat(64),name:'beta.mp4',path:'历史/beta.mp4',status:'queued',size:300,modified:1750000200,output_size:null,retry_requested:false},
      {id:'2'.repeat(64),name:'Alpha.mp4',path:'历史/Alpha.mp4',status:'queued',size:100,modified:1750000100,output_size:null,retry_requested:false},
      {id:'3'.repeat(64),name:'gamma.flv',path:'历史/gamma.flv',status:'queued',size:200,modified:1750000300,output_size:null,retry_requested:false}
    ];
    jobState.library={enabled:true,label:'历史录像',items:jobs,total:3,page:1,page_size:25,
      summary:{queued:3,done:0,saved_bytes:0},already_hevc:0,sidecars_retained:0,log:'test.log',
      control:{supported:true,online:true,mode:'running',phase:'idle',control_revision:0,order_revision:5}};
    w.document.querySelector('[data-page="jobs"]').click();
    await waitFor(()=>!w.document.querySelector('#library-queue').hidden);
    assert.equal(w.document.querySelectorAll('#library-table .job-modified').length,3);
    const sort=w.document.querySelector('#library-sort');
    assert.equal(sort.value,'default');
    sort.value='size:desc';sort.dispatchEvent(new w.Event('change'));
    await waitFor(()=>calls.some(c=>c.url.startsWith('/api/library/jobs?') &&
      new URL(c.url,w.location.href).searchParams.get('sort')==='size' &&
      new URL(c.url,w.location.href).searchParams.get('direction')==='desc'));
    await waitFor(()=>w.document.querySelector('#library-table tr')?.textContent.includes('beta.mp4'));
    await waitFor(()=>!w.document.querySelector('#library-apply-sort').disabled);
    w.document.querySelector('#library-apply-sort').click();
    await waitFor(()=>calls.some(c=>c.url==='/api/library/order'));
    const request=calls.find(c=>c.url==='/api/library/order');
    assert.equal(request.options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    assert.deepEqual(JSON.parse(request.options.body),{revision:5,sort:'largest'});
    assert.equal(jobState.libraryOrder,'largest');
  }finally{dom.window.close();}
});

test('historical queued files can start now and the request preserves the active job',async()=>{
  const {dom,w,calls,jobState}=page(false);
  try{
    await waitFor(()=>calls.some(c=>c.url==='/api/dashboard'));
    const jobs=[
      {id:'4'.repeat(64),name:'active.mp4',path:'历史/active.mp4',status:'running',size:300,modified:1750000300,output_size:null,retry_requested:false},
      {id:'5'.repeat(64),name:'next.mp4',path:'历史/next.mp4',status:'queued',size:100,modified:1750000100,output_size:null,retry_requested:false}
    ];
    jobState.library={enabled:true,label:'历史录像',items:jobs,total:2,page:1,page_size:25,
      summary:{queued:1,running:1,done:0,saved_bytes:0},already_hevc:0,sidecars_retained:0,log:'test.log',
      control:{supported:true,online:true,mode:'running',phase:'encoding',state:'encoding',
        current_job_id:jobs[0].id,control_revision:0,order_revision:5}};
    w.document.querySelector('[data-page="jobs"]').click();
    await waitFor(()=>w.document.querySelectorAll('#library-table tr').length===2);
    const row=w.document.querySelector('#library-table tr[data-library-job-id="'+jobs[1].id+'"]');
    const start=row.querySelector('button');
    assert.equal(start.textContent,'开始转码');start.click();
    await waitFor(()=>calls.some(c=>c.url===`/api/library/jobs/${jobs[1].id}/start-now`));
    const request=calls.find(c=>c.url===`/api/library/jobs/${jobs[1].id}/start-now`);
    assert.equal(request.options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    assert.deepEqual(JSON.parse(request.options.body),{revision:5});
    assert.equal(jobState.library.items[0].id,jobs[1].id);
    assert.equal(jobState.library.items.find(job=>job.id===jobs[0].id).status,'running');
  }finally{dom.window.close();}
});

test('both queues distinguish the priority encoder from the suspended file and keep both progress bars',async()=>{
  const jobs=[queueJob(1,'running'),queueJob(2,'running'),queueJob(3,'queued')];
  const {dom,w,jobState,snapshot}=page(false,[],jobs);
  try{
    Object.assign(jobState.control,{current_job_id:jobs[1].id,suspended_job_id:jobs[0].id,priority_job_id:jobs[1].id});
    snapshot.progress={job_id:jobs[1].id,percent:12,processed_seconds:12,duration:100,speed:1};
    snapshot.suspended_progress={job_id:jobs[0].id,percent:65,processed_seconds:65,duration:100,speed:1};
    jobState.library={enabled:true,label:'历史录像',items:jobs,total:3,page:1,page_size:25,
      summary:{running:2,queued:1},already_hevc:0,sidecars_retained:0,log:'test.log',
      control:{...jobState.control},progress:snapshot.progress,suspended_progress:snapshot.suspended_progress};
    await openQueue(w,3);
    const mainOld=w.document.querySelector(`tr[data-job-id="${jobs[0].id}"]`);
    const mainNew=w.document.querySelector(`tr[data-job-id="${jobs[1].id}"]`);
    assert.equal(mainOld.querySelector('.status').textContent,'已暂停');
    assert.equal(mainNew.querySelector('.status').textContent,'转码中');
    assert.equal(mainOld.querySelector('progress').value,65);
    assert.equal(mainNew.querySelector('progress').value,12);
    assert.equal(w.document.querySelector('#active-name').textContent,jobs[1].name);
    assert.equal(w.document.querySelector(`tr[data-job-id="${jobs[2].id}"] button[aria-label^="开始转码"]`).disabled,true);
    await waitFor(()=>w.document.querySelectorAll('#library-table tr').length===3);
    const old=w.document.querySelector(`[data-library-job-id="${jobs[0].id}"]`);
    const selected=w.document.querySelector(`[data-library-job-id="${jobs[1].id}"]`);
    assert.equal(old.querySelector('.status').textContent,'已暂停');
    assert.equal(selected.querySelector('.status').textContent,'转码中');
    assert.equal(old.querySelector('progress').value,65);
    assert.equal(selected.querySelector('progress').value,12);
    assert.equal(w.document.querySelector('#library-table button[data-start-job]').disabled,true);
  }finally{dom.window.close();}
});

test('verification progress is labelled separately and does not reuse encoding percentage',async()=>{
  const jobs=[queueJob(1,'running')];
  const {dom,w,jobState,snapshot}=page(false,[],jobs);
  try{
    Object.assign(jobState.control,{phase:'verifying',state:'verifying',current_job_id:jobs[0].id,
      workers:[{job_id:jobs[0].id,role:'primary',phase:'verifying'}]});
    const verifying={job_id:jobs[0].id,kind:'verification',stage:'output_decode',percent:25,
      processed_seconds:25,duration:100,speed:2};
    snapshot.progress=verifying;snapshot.progress_by_job={[jobs[0].id]:verifying};
    jobState.library={enabled:true,label:'历史录像',items:jobs,total:1,page:1,page_size:25,
      summary:{running:1,queued:0},already_hevc:0,sidecars_retained:0,log:'test.log',
      control:{...jobState.control},progress:verifying,progress_by_job:{[jobs[0].id]:verifying}};
    await openQueue(w,1);
    await waitFor(()=>w.document.querySelectorAll('#library-table tr').length===1);
    const main=w.document.querySelector(`tr[data-job-id="${jobs[0].id}"]`);
    const history=w.document.querySelector(`tr[data-library-job-id="${jobs[0].id}"]`);
    assert.match(main.textContent,/正在完整解码成品/);
    assert.match(history.textContent,/正在完整解码成品/);
    assert.match(main.querySelector('progress').getAttribute('aria-label'),/已核验 25%/);
    assert.match(history.querySelector('progress').getAttribute('aria-label'),/已核验 25%/);
    verifying.stage='audio_compare';verifying.percent=null;verifying.processed_seconds=null;
    w.document.querySelector('#refresh-jobs').click();
    await waitFor(()=>w.document.querySelector('#library-table').textContent.includes('正在核对音轨内容'));
    assert.equal(w.document.querySelector('#library-table progress'),null);
  }finally{dom.window.close();}
});

test('mixed-range merge visibly explains why source parts remain',async()=>{
  const jobs=[{...queueJob(1,'done'),operation:'merge',part_count:3,merge_retained_parts:true}];
  const {dom,w}=page(false,[],jobs);
  try{
    await openQueue(w,1);
    assert.match(w.document.querySelector('#job-table').textContent,/已保留原分 P/);
  }finally{dom.window.close();}
});

test('an old historical response cannot clamp the page selected by a newer sort',async()=>{
  const {dom,w,calls,jobState}=page(false);
  let release;
  try{
    await waitFor(()=>calls.some(c=>c.url==='/api/dashboard'));
    const jobs=Array.from({length:30},(_,i)=>({...queueJob(i+1,'queued'),modified:1750000000+i}));
    jobState.library={enabled:true,label:'历史录像',items:jobs,total:30,page:1,page_size:25,
      summary:{queued:30},already_hevc:0,sidecars_retained:0,log:'test.log',
      control:{supported:true,online:true,mode:'running',phase:'idle',order_revision:1}};
    w.document.querySelector('[data-page="jobs"]').click();
    await waitFor(()=>w.document.querySelectorAll('#library-table tr').length===25);
    const originalFetch=w.fetch;
    w.fetch=async(url,options)=>{
      const response=await originalFetch(url,options);
      if(url.startsWith('/api/library/jobs?') && new URL(url,w.location.href).searchParams.get('page')==='2' && !release){
        await new Promise(resolve=>{release=resolve;});
        return {ok:true,json:async()=>({...jobState.library,total:1,page:2,items:[]})};
      }
      return response;
    };
    w.document.querySelector('#library-next').click();
    await waitFor(()=>!!release);
    const select=w.document.querySelector('#library-sort');
    select.value='name:asc';select.dispatchEvent(new w.Event('change'));
    await waitFor(()=>!w.document.querySelector('#library-next').disabled);
    w.document.querySelector('#library-next').click();
    await waitFor(()=>w.document.querySelector('#library-page-label').textContent.includes('第 2 页'));
    const count=calls.filter(c=>c.url.startsWith('/api/library/jobs?')).length;
    release();await new Promise(resolve=>setTimeout(resolve,30));
    assert.equal(calls.filter(c=>c.url.startsWith('/api/library/jobs?')).length,count);
    assert.match(w.document.querySelector('#library-page-label').textContent,/第 2 页/);
    w.document.querySelector('#library-previous').click();
    await waitFor(()=>w.document.querySelector('#library-page-label').textContent.includes('第 1 页'));
    const query=new URL(calls.filter(c=>c.url.startsWith('/api/library/jobs?')).at(-1).url,w.location.href).searchParams;
    assert.equal(query.get('sort'),'name');assert.equal(query.get('direction'),'asc');
  }finally{if(release)release();dom.window.close();}
});

test('historical table headers toggle direction, reset pagination, and keep filters',async()=>{
  const {dom,w,calls,jobState}=page(false);
  try{
    await waitFor(()=>calls.some(c=>c.url==='/api/dashboard'));
    const jobs=Array.from({length:30},(_,i)=>({
      id:(i+1).toString(16).padStart(2,'0').repeat(32),name:'ep'+String(i).padStart(2,'0')+'.mp4',
      path:'历史/ep'+String(i).padStart(2,'0')+'.mp4',status:'queued',size:i+1,modified:1750000000+i,
      output_size:null,retry_requested:false}));
    jobState.library={enabled:true,label:'历史录像',items:jobs,total:30,page:1,page_size:25,
      summary:{queued:30,done:0,saved_bytes:0},already_hevc:0,sidecars_retained:0,log:'test.log',
      control:{supported:true,online:true,mode:'running',phase:'idle',control_revision:0,order_revision:5}};
    w.document.querySelector('[data-page="jobs"]').click();
    await waitFor(()=>w.document.querySelectorAll('#library-table tr').length===25);
    const filter=w.document.querySelector('#library-filter'),search=w.document.querySelector('#library-search');
    filter.value='queued';filter.dispatchEvent(new w.Event('change'));
    search.value='ep';search.dispatchEvent(new w.Event('input'));
    await new Promise(resolve=>setTimeout(resolve,400));
    await waitFor(()=>calls.some(c=>{
      const query=new URL(c.url,w.location.href).searchParams;
      return c.url.startsWith('/api/library/jobs?') && query.get('status')==='queued' && query.get('search')==='ep';
    }));
    w.document.querySelector('#library-next').click();
    await waitFor(()=>w.document.querySelector('#library-page-label').textContent.includes('第 2 页'));
    const sizeHeader=w.document.querySelector('[data-sort-column="size"]');
    sizeHeader.querySelector('button').click();
    await waitFor(()=>{
      const last=calls.filter(c=>c.url.startsWith('/api/library/jobs?')).at(-1);
      const query=new URL(last.url,w.location.href).searchParams;
      return query.get('sort')==='size' && query.get('direction')==='desc' &&
        query.get('page')==='1' && query.get('status')==='queued' && query.get('search')==='ep';
    });
    await waitFor(()=>sizeHeader.getAttribute('aria-sort')==='descending');
    assert.equal(w.document.querySelector('#library-sort').value,'size:desc');
    assert.match(w.document.querySelector('#library-table tr').textContent,/ep25\.mp4/);
    await waitFor(()=>!w.document.querySelector('#library-apply-sort').disabled);
    sizeHeader.querySelector('button').click();
    await waitFor(()=>{
      const query=new URL(calls.filter(c=>c.url.startsWith('/api/library/jobs?')).at(-1).url,w.location.href).searchParams;
      return query.get('sort')==='size' && query.get('direction')==='asc' && query.get('page')==='1';
    });
    await waitFor(()=>sizeHeader.getAttribute('aria-sort')==='ascending');
    assert.equal(w.document.querySelector('#library-sort').value,'size:asc');
    await waitFor(()=>w.document.querySelector('#library-table tr')?.textContent.includes('ep00.mp4'));
    assert.match(w.document.querySelector('#library-table tr').textContent,/ep00\.mp4/);
    assert.doesNotMatch(w.document.querySelector('#library-page-label').textContent,/第 2 页/);
  }finally{dom.window.close();}
});

test('historical bulk deletion respects status filters, excludes retries, preserves selection on conflicts',async()=>{
  const {dom,w,calls,jobState}=page(false);
  try{
    await waitFor(()=>calls.some(c=>c.url==='/api/dashboard'));
    const jobs=['failed','done','queued','running','failed'].map((status,i)=>({id:String(i+1).repeat(64),name:'video'+i+'.mp4',path:'video'+i+'.mp4',status,size:100,retry_requested:i===4}));
    jobState.library={enabled:true,label:'历史',items:jobs,total:5,page:1,page_size:25,summary:{},already_hevc:0,sidecars_retained:0,control:{}};
    w.document.querySelector('[data-page="jobs"]').click();
    await waitFor(()=>!w.document.querySelector('#library-queue').hidden);
    const filter=w.document.querySelector('#library-filter');
    filter.value='failed';filter.dispatchEvent(new w.Event('change'));
    await waitFor(()=>w.document.querySelectorAll('#library-table tr').length===2);
    assert.equal(w.document.querySelectorAll('#library-table input').length,1);
    const all=w.document.querySelector('#library-select-all');all.click();
    assert.match(w.document.querySelector('#library-selection-count').textContent,/1/);
    jobState.failLibraryBulk=true;w.document.querySelector('#library-delete-selected').click();
    await waitFor(()=>calls.some(c=>c.url==='/api/library/jobs/delete-records'));
    await waitFor(()=>!w.document.querySelector('#library-delete-selected').disabled);
    assert.equal(w.document.querySelector('#library-table input').checked,true);
    jobState.failLibraryBulk=false;w.document.querySelector('#library-delete-selected').click();
    await waitFor(()=>jobState.library.items.length===4);
    await waitFor(()=>w.document.querySelector('#library-delete-selected').disabled);
    filter.value='done';filter.dispatchEvent(new w.Event('change'));
    await waitFor(()=>w.document.querySelector('#library-table input')?.value===jobs[1].id && !all.disabled);
    all.click();w.document.querySelector('#library-delete-selected').click();
    await waitFor(()=>jobState.library.items.length===3);
    const requests=calls.filter(c=>c.url==='/api/library/jobs/delete-records');
    assert.deepEqual(JSON.parse(requests.at(-1).options.body).ids,[jobs[1].id]);
    assert.equal(requests.at(-1).options.headers['X-CSRF-Token'],'isolated-ui-csrf');
    assert.equal(calls.some(c=>c.url==='/api/jobs/delete-records'),false);
  }finally{dom.window.close();}
});
