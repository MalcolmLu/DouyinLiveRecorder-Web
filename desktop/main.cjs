"use strict";
const {app,BrowserWindow,Menu,Tray,nativeImage,ipcMain,dialog,shell,session,powerSaveBlocker}=require("electron");
const {spawn}=require("node:child_process");
const fs=require("node:fs");
const path=require("node:path");
const readline=require("node:readline");
let window,backend,tray,origin,authCookie,quitting=false,shutdownPromise=null,awake=null,prefs={close_to_tray:true};
let shellLogFile;
const gotLock=app.requestSingleInstanceLock();
if(!gotLock){app.quit();}
else{
  app.on("second-instance",()=>showWindow());
  app.whenReady().then(start).catch(error=>{
    dialog.showErrorBox("录播软件启动失败",error.message+"\n请检查应用数据目录中的 logs。");
    quitting=true;app.quit();
  });
}
function logShell(event,fields={}){
  if(!shellLogFile)return;
  try{fs.appendFileSync(shellLogFile,JSON.stringify({time:new Date().toISOString(),event,...fields})+"\n");}catch{}
}
function showWindow(reason="request"){
  if(window){window.show();window.focus();logShell("window-restored",{reason:typeof reason==="string"?reason:"application"});}
}
function trusted(event){
  if(!window || event.sender!==window.webContents || !event.senderFrame ||
      new URL(event.senderFrame.url).origin!==origin)throw new Error("请求来源不正确。");
}
async function mainAPI(endpoint){
  const response=await fetch(origin+endpoint,{headers:{Cookie:"recorder_session="+authCookie}});
  if(!response.ok)throw new Error("无法读取桌面设置。");
  return response.json();
}
async function updatePreferences(){
  const data=await mainAPI("/api/desktop");prefs=data.values;
  updateTray();
}
function updateTray(){
  if(!tray)return;
  tray.setContextMenu(Menu.buildFromTemplate([
    {label:"显示录播控制台",click:()=>showWindow("tray-menu")},
    {label:"打开录像目录",click:async()=>{const data=await mainAPI("/api/desktop");shell.openPath(data.values.downloads);}},
    {type:"separator"},
    {label:"退出并保存录像",click:()=>{app.quit();}},
  ]));
}
async function start(){
  const dataDir=process.env.DLR_DESKTOP_TEST_DATA || app.getPath("userData");
  fs.mkdirSync(path.join(dataDir,"logs"),{recursive:true});
  shellLogFile=path.join(dataDir,"logs","desktop-shell.log");
  logShell("application-started",{version:app.getVersion()});
  const packaged=app.isPackaged;
  const root=path.resolve(__dirname,"..");
  const executable=packaged ? path.join(process.resourcesPath,"backend",process.platform==="win32"?"dlr-backend.exe":"dlr-backend") :
    process.env.DLR_PYTHON || (process.platform==="win32"?"python":"python3");
  const args=[...(packaged?[]:[path.join(root,"desktop","backend.py")]),"--data-dir",dataDir,
      "--tools-dir",path.join(packaged?process.resourcesPath:__dirname,"tools")];
  backend=spawn(executable,args,{stdio:["pipe","pipe","pipe"],windowsHide:true,
      env:{...process.env,PYTHONUTF8:"1",PYTHONIOENCODING:"utf-8",PYTHONDONTWRITEBYTECODE:"1"}});
  const errors=fs.createWriteStream(path.join(dataDir,"logs","desktop-backend.log"),{flags:"a"});
  backend.stderr.pipe(errors);
  const ready=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error("后台启动超时。")),120000);
    backend.once("error",error=>{clearTimeout(timer);reject(error);});
    backend.once("exit",code=>{clearTimeout(timer);reject(new Error("录制后台退出："+code));});
    const lines=readline.createInterface({input:backend.stdout});
    lines.on("line",line=>{
      try{
        const data=JSON.parse(line);
        if(data.event==="ready"){clearTimeout(timer);lines.close();resolve(data);}
      }catch{}
    });
  });
  const parsed=new URL(ready.url);
  if(parsed.protocol!=="http:" || parsed.hostname!=="127.0.0.1")throw new Error("本地后台地址不正确。");
  origin=parsed.origin;authCookie=ready.cookie;
  const browserSession=session.fromPartition("desktop-console");
  browserSession.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
  browserSession.setPermissionCheckHandler(()=>false);
  await browserSession.cookies.set({url:origin,name:"recorder_session",value:authCookie,
      httpOnly:true,secure:false,sameSite:"strict",path:"/"});
  window=new BrowserWindow({width:1280,height:860,minWidth:920,minHeight:650,show:false,
      title:"DouyinLiveRecorder",backgroundColor:"#f4f7f3",icon:path.join(__dirname,"assets","icon.png"),
      webPreferences:{preload:path.join(__dirname,"preload.cjs"),nodeIntegration:false,
        contextIsolation:true,sandbox:true,webSecurity:true,session:browserSession}});
  window.webContents.setWindowOpenHandler(()=>({action:"deny"}));
  window.webContents.on("will-navigate",(event,url)=>{if(new URL(url).origin!==origin)event.preventDefault();});
  window.webContents.on("will-attach-webview",event=>event.preventDefault());
  window.on("close",event=>{
    logShell("window-close-requested",{close_to_tray:!!prefs.close_to_tray,quitting});
    if(!quitting){event.preventDefault();if(prefs.close_to_tray)window.hide();else app.quit();}
  });
  window.on("hide",()=>logShell("window-hidden"));
  window.on("show",()=>logShell("window-shown"));
  ipcMain.handle("desktop:choose-folder",async event=>{
    trusted(event);const result=await dialog.showOpenDialog(window,{title:"选择录像保存目录",properties:["openDirectory","createDirectory"]});
    return result.canceled?null:result.filePaths[0];
  });
  ipcMain.handle("desktop:choose-ffmpeg",async event=>{
    trusted(event);const result=await dialog.showOpenDialog(window,{title:"选择 FFmpeg 程序",properties:["openFile"],
       ...(process.platform==="win32"?{filters:[{name:"FFmpeg",extensions:["exe"]}]}:{})});
    return result.canceled?null:result.filePaths[0];
  });
  for(const [channel,field] of [["desktop:open-recordings","recordings"],["desktop:open-logs","logs"]])
    ipcMain.handle(channel,async event=>{
      trusted(event);const data=await mainAPI("/api/desktop");
      const selected=field==="recordings"?data.values.downloads:path.join(data.data_dir,"logs");
      fs.mkdirSync(selected,{recursive:true});return shell.openPath(selected);
    });
  ipcMain.handle("desktop:preferences-changed",async event=>{trusted(event);await updatePreferences();return true;});
  const image=nativeImage.createFromBuffer(fs.readFileSync(path.join(__dirname,"assets",process.platform==="darwin"?"trayTemplate.png":"icon.png")))
      .resize({width:process.platform==="darwin"?18:32,height:process.platform==="darwin"?18:32});
  if(process.platform==="darwin")image.setTemplateImage(true);
  tray=new Tray(image);tray.setToolTip("DouyinLiveRecorder · 录播控制台");
  if(process.platform==="darwin"){
    tray.setTitle("录播");
    app.dock.setMenu(Menu.buildFromTemplate([
      {label:"显示录播控制台",click:()=>showWindow("dock-menu")},
      {label:"退出并保存录像",click:()=>app.quit()},
    ]));
  }
  logShell("tray-created",{image_empty:image.isEmpty(),bounds:tray.getBounds()});
  tray.on("click",()=>showWindow("tray-click"));tray.on("double-click",()=>showWindow("tray-double-click"));
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform==="darwin"?[{label:app.name,submenu:[{role:"about"},{type:"separator"},{label:"显示窗口",click:showWindow},
      {label:"退出并保存录像",accelerator:"Cmd+Q",click:()=>app.quit()}]}]:[]),
    {label:"编辑",submenu:[{role:"undo"},{role:"redo"},{type:"separator"},{role:"cut"},{role:"copy"},{role:"paste"},{role:"selectAll"}]},
    {label:"窗口",submenu:[{role:"minimize"},{label:"显示控制台",click:showWindow}]},
  ]));
  await updatePreferences();
  await window.loadURL(origin+"/#live");
  if(!prefs.start_minimized)window.show();
  backend.on("exit",()=>{if(!quitting){dialog.showErrorBox("录制后台已停止","请重新打开软件；已保存的录像和队列仍在应用数据目录。");app.quit();}});
  setInterval(async()=>{
    if(quitting)return;
    try{
      const data=await mainAPI("/api/dashboard");
      const active=data.recording_count>0 || data.queue.running>0;
      if(prefs.prevent_sleep && active && awake===null)awake=powerSaveBlocker.start("prevent-app-suspension");
      if((!prefs.prevent_sleep || !active) && awake!==null){powerSaveBlocker.stop(awake);awake=null;}
    }catch{}
  },5000).unref();
}
app.on("activate",showWindow);
app.on("window-all-closed",()=>{if(quitting)app.quit();});
app.on("before-quit",event=>{
  if(quitting)return;
  event.preventDefault();quitting=true;
  logShell("application-quit-requested");
  if(window)window.setTitle("正在保存录像并退出…");
  shutdownPromise=new Promise(resolve=>{
    if(!backend || backend.exitCode!==null){resolve();return;}
    const timeout=setTimeout(()=>{backend.kill();resolve();},75000);
    backend.once("exit",()=>{clearTimeout(timeout);resolve();});
    backend.stdin.end("shutdown\n");
  }).finally(()=>{logShell("application-stopped");tray?.destroy();if(awake!==null)powerSaveBlocker.stop(awake);app.quit();});
});
