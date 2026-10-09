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
function page(desktop){
  const calls=[];
  const values={downloads:"/example/recordings",ffmpeg:"",encoder:"auto",bitrate_kbps:4000,cpu_threads:2,
    auto_start:true,close_to_tray:true,start_minimized:false,prevent_sleep:true,recording_roots:["/example/recordings"]};
  const snapshot={rooms:[],monitoring_count:0,recording_count:0,queue:{queued:0,running:0,done:0,failed:0,saved_bytes:0},
    recorder:{connected:true},updated:Date.now()/1000,revision:"record-revision",default_quality:"原画",
    encoding:{encoder:"hevc_videotoolbox"},desktop:desktop?{running:false,ffmpeg_ready:true,node_ready:true}:undefined};
  const dom=new JSDOM(html,{url:"http://127.0.0.1:18888/",runScripts:"outside-only",pretendToBeVisual:true});
  const w=dom.window;
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
