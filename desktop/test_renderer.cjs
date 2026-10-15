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
    else if(url==="/api/dashboard")data=snapshot;
    else if(url==="/api/desktop"){
      if(options.method==="PUT")Object.assign(values,JSON.parse(options.body).values);
      data=options.method==="PUT"?{ok:true,message:"已保存"}:
        {values,revision:"desktop-revision",status:snapshot.desktop,encoders:[{value:"libx265",label:"CPU"},{value:"hevc_videotoolbox",label:"Apple VideoToolbox"}],
          data_dir:"/example/data",ffmpeg_path:"/example/tools/ffmpeg"};
    }else if(url.includes("/recordings?")){
      const folder=new URL(url,w.location.href).searchParams.get("folder") || "0/B站直播/测试主播";
      const child=folder.endsWith("/2026-10-10");
      data={room:{id:rooms[0]?.id,name:"测试主播"},folder,parent:child?"0/B站直播/测试主播":null,
        breadcrumbs:[{name:"测试主播",path:"0/B站直播/测试主播"}],total:child?1:2,page:1,page_size:25,
        items:[{kind:"file",name:"中文 & <测试>.mp4",path:folder+"/中文 & <测试>.mp4",size:1024,modified:1750000000,format:"MP4"},
          ...(!child?[{kind:"folder",name:"2026-10-10",path:folder+"/2026-10-10",modified:1750000000}]:[])]};
    }else if(url==="/api/jobs/delete-records"){
      if(jobState.failBulk)return {ok:false,status:409,json:async()=>({error:{code:"conflict",message:"任务已重新排队，请刷新后重试。"}})};
      const ids=JSON.parse(options.body).ids;
      jobState.items=jobState.items.filter(job=>!ids.includes(job.id));
      data={ok:true,deleted:ids.length,message:"已删除所选记录，录像文件已保留。"};
    }else if(url.startsWith("/api/jobs?")){
      const params=new URL(url,w.location.href).searchParams;
      const status=params.get("status"),search=params.get("search"),number=Number(params.get("page"));
      const filtered=jobState.items.filter(job=>(status==="all" || job.status===status) && (!search || job.name.includes(search)));
      data={items:filtered.slice((number-1)*jobState.pageSize,number*jobState.pageSize),total:filtered.length,page:number,page_size:jobState.pageSize};
    }
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(data))};
  };
  w.recorderDesktop={chooseFolder:async()=>"/example/new-recordings",chooseFFmpeg:async()=>"/example/bin/ffmpeg",
    preferencesChanged:async()=>true,openRecordings:async()=>true,openLogs:async()=>true};
  w.eval(script);
  return {dom,w,calls,jobState};
}
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
  return {id:index.toString(16).padStart(64,"0"),name:`录像 ${index}.ts`,path:`测试主播/录像 ${index}.ts`,
    status,size:1000,output_size:status==="done"?500:null,saved_percent:status==="done"?50:null,error:"测试失败"};
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
