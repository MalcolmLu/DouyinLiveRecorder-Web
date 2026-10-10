"use strict";
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const {JSDOM}=require("jsdom");
const root=path.resolve(__dirname,"..");
const html=fs.readFileSync(path.join(root,"gui/static/index.html"),"utf8");
const script=fs.readFileSync(path.join(root,"gui/static/app.js"),"utf8");

async function waitFor(check){
  for(let i=0;i<100;i++){if(check())return;await new Promise(r=>setTimeout(r,10));}
  throw new Error("UI state did not settle");
}
function page(desktop,rooms=[]){
  const calls=[];
  const values={downloads:"/example/recordings",ffmpeg:"",encoder:"auto",bitrate_kbps:4000,cpu_threads:2,
    auto_start:true,close_to_tray:true,start_minimized:false,prevent_sleep:true,recording_roots:["/example/recordings"]};
  const snapshot={rooms,monitoring_count:rooms.length,recording_count:0,queue:{queued:0,running:0,done:0,failed:0,saved_bytes:0},
    recorder:{connected:true},updated:Date.now()/1000,revision:"record-revision",default_quality:"原画",
    encoding:{encoder:"hevc_videotoolbox"},desktop:desktop?{running:false,ffmpeg_ready:true,node_ready:true}:undefined};
  const dom=new JSDOM(html,{url:"http://127.0.0.1:18888/",runScripts:"outside-only",pretendToBeVisual:true});
  const w=dom.window;
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
    }else if(url.startsWith("/api/jobs"))data={items:[],total:0,page:1,page_size:25};
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(data))};
  };
  w.recorderDesktop={chooseFolder:async()=>"/example/new-recordings",chooseFFmpeg:async()=>"/example/bin/ffmpeg",
    preferencesChanged:async()=>true,openRecordings:async()=>true,openLogs:async()=>true};
  w.eval(script);
  return {dom,w,calls};
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
test("room click opens recordings, plays encoded file URLs, navigates folders, and stops on close",async()=>{
  const room={id:"example-room",display_name:"测试主播",name:"测试主播",platform:"哔哩哔哩",url:"https://live.bilibili.com/100001",
    state:"offline",quality:"原画",enabled:true,uses_proxy:false};
  const {dom,w,calls}=page(false,[room]);
  try{
    await waitFor(()=>w.document.querySelector(".room-open"));
    w.document.querySelector(".room-open").click();
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
