const $ = (s) => document.querySelector(s);
const ctx = {csrf:null, snapshot:null, page:"live", jobPage:1, jobs:null, revision:null, settingsRevision:null, proxyRevision:null, busy:false,desktop:false,desktopData:null};
const library = {room:null, folder:"", page:1, data:null, request:0};
const jobSelection = {ids:new Set(), deleting:false, request:0};
const queueActions = {busy:false, control:null};
const statusText = {unknown:"等待检测", checking:"检测中", offline:"等待开播", live:"正在直播",
  recording:"正在录制", disabled:"已暂停", error:"采集异常", queued:"排队中", running:"转码中",
  published:"验证后收尾", done:"已完成", failed:"失败", paused:"已暂停", stopped:"已停止"};
let toastTimer, searchTimer;

class ApiError extends Error {
  constructor(message, status, code) {super(message); this.status=status; this.code=code;}
}
async function api(path, options={}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {
      ...options, credentials:"same-origin", signal:controller.signal,
      headers:{"Content-Type":"application/json", ...(ctx.csrf ? {"X-CSRF-Token":ctx.csrf} : {}), ...(options.headers || {})}
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status===401 && path!=="/api/login") showLogin();
      throw new ApiError(data.error?.message || "暂时无法完成操作，请稍后重试。", response.status, data.error?.code);
    }
    return data;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(e.name==="AbortError" ? "连接超时，请稍后重试。" : "无法连接管理服务，请检查网络。",0,"network");
  } finally { clearTimeout(timeout); }
}
function node(tag, text, className) {
  const e=document.createElement(tag);
  if(text!==undefined && text!==null) e.textContent=String(text);
  if(className) e.className=className;
  return e;
}
function setText(selector, value) {$(selector).textContent=String(value);}
function bytes(n) {
  if(n===null || n===undefined) return "—";
  const a=Math.abs(n); const units=["B","KB","MB","GB","TB"];
  let i=0, value=a;
  while(value>=1000 && i<units.length-1) {value/=1000;i++;}
  return (n<0 ? "−" : "") + new Intl.NumberFormat("zh-CN",{maximumFractionDigits:i>=3 ? 1 : 0}).format(value)+" "+units[i];
}
function elapsed(epoch) {
  if(!epoch) return "—";
  const s=Math.max(0,Math.floor(Date.now()/1000-epoch));
  const h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return (h ? h+" 小时 " : "")+m+" 分钟";
}
function shortTime(epoch) {
  return epoch ? new Intl.DateTimeFormat("zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false,timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone}).format(new Date(epoch*1000)) : "—";
}
function badge(state) {return node("span",statusText[state] || "未知", "status "+(Object.hasOwn(statusText,state) ? state : "unknown"));}
function toast(message) {
  clearTimeout(toastTimer);setText("#toast",message);$("#toast").hidden=false;
  toastTimer=setTimeout(() => {$("#toast").hidden=true;},5000);
}
function globalError(message="") {setText("#global-error",message);$("#global-error").hidden=!message;}
function showLogin() {closeRecordings();clearJobSelection();$("#app-view").hidden=true;$("#login-view").hidden=false;ctx.csrf=null;}
function showApp() {$("#login-view").hidden=true;$("#app-view").hidden=false;}
function button(text, fn, className="row-button") {
  const b=node("button",text,className);b.type="button";b.addEventListener("click",fn);return b;
}
async function action(buttonElement, fn, errorSelector=null) {
  buttonElement.disabled=true;
  if(errorSelector) setText(errorSelector,"");
  try {await fn();}
  catch(e) {if(errorSelector) setText(errorSelector,e.message); else toast(e.message);}
  finally {buttonElement.disabled=false;}
}

function renderRooms() {
  const data=ctx.snapshot; if(!data) return;
  const labels={hevc_qsv:"Intel QSV",hevc_videotoolbox:"Apple VideoToolbox",hevc_nvenc:"NVIDIA NVENC",hevc_amf:"AMD AMF",libx265:"CPU"};
  const encoder=labels[data.encoding?.encoder] || "H.265";
  setText("#engine-title",data.encoding?.encoder==="libx265"?"H.265 软件转码":"H.265 硬件转码");
  setText("#engine-description",encoder+" · 单任务队列");setText("#active-encoder-label","H.265 · "+encoder);
  renderDesktopStatus(data.desktop);
  setText("#metric-rooms",data.monitoring_count);
  setText("#metric-recording",data.recording_count);
  setText("#metric-queue",data.queue.queued+data.queue.running+(data.queue.published||0));
  setText("#metric-queue-note",data.queue.running ? "1 个正在处理，其余按序排队" : "按顺序自动处理");
  setText("#metric-saved",bytes(data.queue.saved_bytes));
  setText("#nav-rooms",data.monitoring_count);setText("#nav-jobs",data.queue.queued+data.queue.running);
  setText("#last-update","更新于 "+shortTime(data.updated));
  const connection=$("#connection");connection.classList.toggle("offline",!data.recorder.connected);
  connection.lastElementChild.textContent=data.recorder.connected ? "采集器已连接" : "采集器未连接";
  $("#room-loading").hidden=true;$("#room-empty").hidden=data.rooms.length>0;$("#room-table-wrap").hidden=!data.rooms.length;
  const body=$("#room-table");body.replaceChildren();
  for(const room of data.rooms) {
    const tr=node("tr");
    const main=node("td");const cell=node("div",null,"room-cell");
    const initials={"哔哩哔哩":"B站","虎牙":"虎牙","YouTube":"YT","抖音":"抖音","Twitch":"TW","TikTok":"TK"};
    cell.append(node("span",initials[room.platform] || room.platform.slice(0,2),"platform-avatar"));
    const title=node("div"), roomLink=node("a",room.display_name,"cell-title room-live-link");
    try {
      const url=new URL(room.url);
      if(["http:","https:"].includes(url.protocol) && !url.username && !url.password){
        roomLink.href=url.href;roomLink.target="_blank";roomLink.rel="noopener noreferrer";
        roomLink.title="打开“"+room.display_name+"”的直播页面";
        roomLink.setAttribute("aria-label",room.display_name+" 的直播页面（新标签页）");
      }
    }catch(_){/* An invalid stored URL must never become an executable link. */}
    title.append(roomLink,node("div",room.platform+" · "+room.url,"cell-subtitle"));
    cell.append(title);main.append(cell);tr.append(main);
    const state=node("td");state.append(badge(room.state));if(room.last_error){const sub=node("div","检测失败，稍后自动重试","cell-subtitle");sub.title=room.last_error;state.append(sub);}tr.append(state);
    const route=node("td");route.append(node("span",room.uses_proxy ? "共享代理" : "直连","route-tag"+(room.uses_proxy ? " proxy" : "")));
    if(room.applied_proxy!==null && room.applied_proxy!==undefined && room.applied_proxy!==room.uses_proxy && room.state==="recording") route.append(node("div","下次检测生效","cell-subtitle"));
    tr.append(route,node("td",room.quality),node("td",room.state==="recording" ? elapsed(room.started_at) : "—"));
    const controls=node("td",null,"align-right"), actions=node("div",null,"actions");
    actions.append(button("录像文件夹",()=>openRecordings(room),"row-button room-recordings"),button("编辑",() => openRoom(room)));
    const toggle=button(room.enabled ? "暂停监控" : "恢复监控",() => action(toggle,async() => {
      if(room.state==="recording" && !window.confirm("暂停会结束这个直播间当前的录制，已写入的视频会保留并按设置处理。是否暂停？")) return;
      await api("/api/rooms/"+room.id,{method:"PATCH",body:JSON.stringify({enabled:!room.enabled,revision:ctx.snapshot.revision})});
      toast(room.enabled ? "已暂停监控。" : "已恢复监控，采集器将自动检测。");await refresh();
    }));
    const remove=button("删除",()=>action(remove,async()=>{
      if(!window.confirm("删除“"+room.name+"”的监控任务？已录制的视频和转码记录会保留。")) return;
      await api("/api/rooms/"+room.id,{method:"DELETE",body:JSON.stringify({revision:ctx.snapshot.revision})});
      toast("直播间已删除，录像已保留。");await refresh();
    }));
    actions.append(toggle,remove);controls.append(actions);tr.append(controls);body.append(tr);
  }
}
function stopPlayback() {
  const player=$("#recording-player");
  player.pause();player.removeAttribute("src");player.load();
  $("#recording-player-wrap").hidden=true;$("#recording-download").hidden=true;
  $("#recording-download").removeAttribute("href");
  setText("#recording-selected","选择一个录制文件");setText("#recording-playback-status","");
}
function closeRecordings() {
  library.request++;library.room=null;library.data=null;
  stopPlayback();
  if($("#recordings-dialog").open) $("#recordings-dialog").close();
}
async function openRecordings(room) {
  stopPlayback();library.room=room;library.folder="";library.page=1;library.data=null;
  setText("#recordings-title",room.display_name+" · 录像文件夹");
  if(!$("#recordings-dialog").open) $("#recordings-dialog").showModal();
  await loadRecordings("",1);
}
function mediaURL(file, download=false) {
  const query=new URLSearchParams({path:file.path});if(download)query.set("download","1");
  return "/api/rooms/"+encodeURIComponent(library.room.id)+"/recordings/media?"+query;
}
async function playRecording(file) {
  const player=$("#recording-player"), download=$("#recording-download");
  player.pause();setText("#recording-selected",file.name);setText("#recording-playback-status","正在加载录像…");
  download.href=mediaURL(file,true);download.hidden=false;
  $("#recording-player-wrap").hidden=false;player.src=mediaURL(file);player.load();
  try{await player.play();}
  catch(e){setText("#recording-playback-status",e.name==="NotAllowedError" ? "录像已加载，请点击播放器的播放按钮。" : "浏览器暂时无法播放。文件可能尚未写入完成，或浏览器不支持此编码；可以下载后使用本机播放器打开。");}
}
async function loadRecordings(folder=library.folder,page=library.page) {
  if(!library.room) return;
  const request=++library.request, room=library.room;
  setText("#recordings-error","");setText("#recordings-count","正在读取文件夹…");
  $("#recordings-refresh").disabled=true;
  try{
    let data=await api("/api/rooms/"+encodeURIComponent(room.id)+"/recordings?"+new URLSearchParams({folder,page:String(page)}));
    const last=Math.max(1,Math.ceil(data.total/data.page_size));
    if(data.page>last) data=await api("/api/rooms/"+encodeURIComponent(room.id)+"/recordings?"+new URLSearchParams({folder:data.folder,page:String(last)}));
    if(request!==library.request || room!==library.room) return;
    if(library.data && data.folder!==library.folder) stopPlayback();
    library.folder=data.folder;library.page=data.page;library.data=data;
    const breadcrumbs=$("#recordings-breadcrumbs");breadcrumbs.replaceChildren();
    for(const crumb of data.breadcrumbs) breadcrumbs.append(button(crumb.name,()=>loadRecordings(crumb.path,1),"text-button"));
    const list=$("#recordings-list");list.replaceChildren();
    for(const item of data.items){
      const row=node("div",null,"recording-file"), details=node("div",null,"recording-file-details");
      const name=button((item.kind==="folder"?"📁 ":"")+item.name,()=>item.kind==="folder"?loadRecordings(item.path,1):playRecording(item),"recording-file-name");
      name.title=item.name;details.append(name);
      details.append(node("small",item.kind==="folder"?"文件夹":item.format+" · "+bytes(item.size)+" · "+new Date(item.modified*1000).toLocaleString("zh-CN"),"muted"));
      row.append(details,button(item.kind==="folder"?"打开":"播放",()=>item.kind==="folder"?loadRecordings(item.path,1):playRecording(item)));
      list.append(row);
    }
    if(!data.items.length) list.append(node("p","此文件夹暂无录像。直播录制后，可在这里查看和播放。","recordings-empty muted"));
    setText("#recordings-count",data.total+" 项 · 第 "+data.page+" / "+Math.max(1,Math.ceil(data.total/data.page_size))+" 页");
    $("#recordings-up").disabled=data.parent===null;
    $("#recordings-previous").disabled=data.page<=1;
    $("#recordings-next").disabled=data.page*data.page_size>=data.total;
  }catch(e){
    if(request===library.request){setText("#recordings-error",e.message);setText("#recordings-count","读取失败");}
  }finally{if(request===library.request)$("#recordings-refresh").disabled=false;}
}
$("#close-recordings").addEventListener("click",closeRecordings);
$("#recordings-dialog").addEventListener("cancel",event=>{event.preventDefault();closeRecordings();});
$("#recordings-dialog").addEventListener("close",()=>{if(library.room)closeRecordings();});
$("#recordings-refresh").addEventListener("click",()=>loadRecordings());
$("#recordings-up").addEventListener("click",()=>{if(library.data && library.data.parent!==null)loadRecordings(library.data.parent,1);});
$("#recordings-previous").addEventListener("click",()=>loadRecordings(library.folder,library.page-1));
$("#recordings-next").addEventListener("click",()=>loadRecordings(library.folder,library.page+1));
$("#recording-player").addEventListener("playing",()=>setText("#recording-playback-status","正在播放"));
$("#recording-player").addEventListener("error",()=>{
  if(library.room && $("#recording-player").hasAttribute("src"))setText("#recording-playback-status","浏览器无法播放此录像。文件可能尚未写入完成，或浏览器不支持当前格式 / H.265 编码；请下载后使用本机播放器打开。");
});
function renderSummary() {
  const q=ctx.snapshot?.queue;if(!q) return;
  const target=$("#queue-summary");target.replaceChildren();
  for(const [key,label] of [["running","处理中"],["queued","排队中"],["stopped","已停止"],["done","已完成"],["failed","失败"]]){
    const d=node("span",label);d.append(node("strong",q[key]||0));target.append(d);
  }
  renderQueueControl(ctx.snapshot?.queue_control);
}
function renderQueueControl(control) {
  if(control) queueActions.control=control;
  const state=queueActions.control, ready=state?.supported && state.online && state.phase!=="maintenance";
  const busy=queueActions.busy || jobSelection.deleting, mode=state?.mode || "running";
  const labels={offline:"转码控制未连接",maintenance:"转码服务更新中",starting:"转码服务启动中",idle:"等待转码任务",preparing:"准备转码",encoding:"正在转码",verifying:"正在验证视频",publishing:"正在保存视频",finalizing:"正在完成收尾",pausing:"正在暂停转码",paused:"转码已暂停",stopping:"正在停止转码",stopped:"转码已停止",resuming:"正在继续转码"};
  setText("#queue-control-state",labels[state?.state] || "正在连接转码服务");
  setText("#queue-control-detail",!ready ? "正在连接转码进程，请稍后刷新。" : mode==="stopped" ? "停止会保留未完成任务的原录像。继续后处理其余等待任务；已停止的任务需重新排队，从头转码。" : mode==="paused" ? "当前进度保留，继续后接着转码。直播录制仍会继续。" : "按下方顺序转码。暂停保留进度；停止保留未完成任务的原录像。" );
  $("#queue-pause").disabled=busy || !ready || mode!=="running";
  $("#queue-resume").disabled=busy || !ready || mode==="running" || state?.state==="stopping";
  $("#queue-stop").disabled=busy || !ready || mode==="stopped";
  const queued=ctx.snapshot?.queue?.queued || (ctx.jobs?.items || []).filter(job=>job.status==="queued").length;
  $("#queue-apply-sort").disabled=busy || !ready || !queued;
  $("#queue-sort").disabled=busy || !ready;
  if(mode!=="running")setText("#metric-queue-note",mode==="paused" ? "转码已暂停，录制继续" : "转码已停止，录制继续");
}
async function queueRequest(path,payload) {
  if(queueActions.busy || jobSelection.deleting)return;
  queueActions.busy=true;renderQueueControl();if(ctx.jobs)renderJobs(ctx.jobs);
  try {
    const result=await api(path,{method:"POST",body:JSON.stringify(payload)});
    renderQueueControl(result.control);toast(result.message);await loadJobs();
    ctx.snapshot=await api("/api/dashboard");renderRooms();renderSummary();
  } catch(e) {
    toast(e.message);try{await loadJobs();}catch(error){globalError(error.message);}
  } finally {queueActions.busy=false;renderQueueControl();if(ctx.jobs)renderJobs(ctx.jobs);}
}
function renderJobs(data) {
  ctx.jobs=data;const body=$("#job-table");body.replaceChildren();
  renderQueueControl(data.control);
  const deletable=new Set(data.items.filter(job=>["done","failed","stopped"].includes(job.status)).map(job=>job.id));
  for(const id of jobSelection.ids) if(!deletable.has(id)) jobSelection.ids.delete(id);
  $("#jobs-empty").hidden=data.items.length>0;$("#job-table-wrap").hidden=!data.items.length;
  setText("#jobs-total",data.total+" 个任务");setText("#page-label","第 "+data.page+" 页 / 共 "+Math.max(1,Math.ceil(data.total/data.page_size))+" 页");
  $("#previous-page").disabled=data.page<=1;$("#next-page").disabled=data.page*data.page_size>=data.total;
  let active=null;
  for(const job of data.items) {
    const tr=node("tr"), title=node("td");
    tr.dataset.jobId=job.id;
    const selectCell=node("td",null,"queue-select-cell"), checkbox=node("input",null,"queue-checkbox job-checkbox");
    checkbox.type="checkbox";checkbox.dataset.jobId=job.id;
    checkbox.setAttribute("aria-label","选择“"+job.name+"”的转码记录");
    checkbox.disabled=!deletable.has(job.id) || jobSelection.deleting;
    checkbox.title=deletable.has(job.id) ? "选择这条转码记录" : "排队中或处理中的记录暂不能删除";
    checkbox.addEventListener("change",()=>{
      if(checkbox.checked) jobSelection.ids.add(job.id);else jobSelection.ids.delete(job.id);
      syncJobSelection();
    });
    selectCell.append(checkbox);tr.append(selectCell);
    const t=node("div",job.name,"cell-title");t.title=job.name;
    title.append(t,node("div",job.path,"cell-subtitle"));
    if(job.status==="queued" && job.queue_position){
      const order=node("div",null,"queue-order-actions");order.append(node("span","第 "+job.queue_position+" 项"));
      for(const [move,label] of [["top","置顶"],["up","上移"],["down","下移"],["bottom","置底"]]){
        const change=button(label,()=>queueRequest("/api/queue/order",{revision:ctx.jobs.order_revision,job_id:job.id,move}));
        change.setAttribute("aria-label",label+"“"+job.name+"”");change.dataset.move=move;
        change.disabled=queueActions.busy || jobSelection.deleting || !queueActions.control?.online || !queueActions.control?.supported ||
          ((move==="top" || move==="up") && job.queue_position===1) || ((move==="down" || move==="bottom") && job.queue_position===job.queued_total);
        order.append(change);
      }
      title.append(order);
    }
    tr.append(title);
    const paused=job.status==="running" && queueActions.control?.current_job_id===job.id && queueActions.control.phase==="paused";
    const status=node("td");status.append(badge(paused ? "paused" : job.status));
    const progress=ctx.snapshot?.progress;
    if(job.status==="running") {
      active=job;
      if(progress?.job_id===job.id && progress.percent!==null) {
        const p=node("progress");p.max=100;p.value=progress.percent;p.setAttribute("aria-label","已转码 "+progress.percent+"%");status.append(p,node("div",progress.percent+"%","cell-subtitle"));
      }
    }
    tr.append(status,node("td",bytes(job.size)),node("td",bytes(job.output_size)));
    const savings=node("td",job.saved_percent===null ? "—" : (job.saved_percent>=0 ? "减少 " : "增加 ")+Math.abs(job.saved_percent)+"%",job.saved_percent===null ? "" : job.saved_percent>=0 ? "positive" : "negative");
    tr.append(savings);
    const control=node("td",null,"align-right"), actions=node("div",null,"actions");
    if(["failed","stopped"].includes(job.status)){
      if(job.status==="failed")actions.append(button("查看原因",()=>{setText("#error-detail",job.error || "没有记录到详细错误。");$("#error-dialog").showModal();}));
      const retry=button(job.status==="stopped" ? "重新排队" : "重试",()=>action(retry,async()=>{
        await api("/api/jobs/"+job.id+"/retry",{method:"POST",body:"{}"});toast("任务已重新排队。");await refresh();
      }));retry.disabled=jobSelection.deleting || queueActions.busy;actions.append(retry);
    } else {actions.append(node("span",job.status==="done" ? "已验证" : "自动处理","muted"));}
    const remove=button("删除记录",()=>action(remove,async()=>{
      if(!window.confirm("删除“"+job.name+"”的转码记录？原录像和转码后的视频都会保留。")) return;
      const result=await api("/api/jobs/"+job.id,{method:"DELETE"});
      toast(result.message);await refresh();
    }));
    remove.disabled=!["done","failed","stopped"].includes(job.status) || jobSelection.deleting || queueActions.busy;
    remove.title=remove.disabled ? "排队中或处理中，请等待任务结束后删除记录。" : "仅删除记录，保留录像文件。";
    actions.append(remove);
    control.append(actions);tr.append(control);body.append(tr);
  }
  syncJobSelection();
  $("#active-encoding").hidden=!active;
  if(active){
    const phase=queueActions.control?.phase, paused=phase==="paused";
    $("#active-encoding").classList.toggle("is-paused",paused);
    setText("#active-phase",paused ? "已暂停 · 进度保留" : phase==="verifying" ? "正在验证视频" : phase==="publishing" ? "正在保存视频" : "正在处理");
    setText("#active-name",active.name);
    const p=ctx.snapshot?.progress, matches=p?.job_id===active.id;
    setText("#active-percent",matches && p.percent!==null ? p.percent+"%" : "读取中");
    const bar=$("#active-progress");
    if(matches && p.percent!==null) bar.value=p.percent;else bar.removeAttribute("value");
    setText("#active-detail",matches ? "已处理 "+Math.floor(p.processed_seconds/60)+" 分钟 / 共 "+Math.ceil(p.duration/60)+" 分钟"+(paused ? " · 已暂停" : p.speed>0 ? " · "+p.speed.toFixed(1)+"×" : "") : "正在准备视频，等待编码器上报进度。");
  }
}
function syncJobSelection() {
  const eligible=(ctx.jobs?.items || []).filter(job=>["done","failed","stopped"].includes(job.status));
  const count=jobSelection.ids.size, all=$("#select-all-jobs");
  all.checked=eligible.length>0 && count===eligible.length;
  all.indeterminate=count>0 && count<eligible.length;
  all.disabled=jobSelection.deleting || !eligible.length;
  setText("#job-selection-count","已选择 "+count+" 条");
  $("#delete-selected-jobs").disabled=jobSelection.deleting || queueActions.busy || !count;
  $("#clear-job-selection").disabled=jobSelection.deleting || !count;
  document.querySelectorAll(".job-checkbox").forEach(input=>{
    input.checked=jobSelection.ids.has(input.dataset.jobId);
    input.closest("tr").classList.toggle("queue-row-selected",input.checked);
  });
}
function clearJobSelection() {jobSelection.ids.clear();syncJobSelection();}
async function loadJobs() {
  const request=++jobSelection.request;
  const params=new URLSearchParams({status:$("#job-filter").value,search:$("#job-search").value,page:String(ctx.jobPage)});
  let data=await api("/api/jobs?"+params);
  if(request!==jobSelection.request)return;
  const lastPage=Math.max(1,Math.ceil(data.total/data.page_size));
  if(data.page>lastPage){
    ctx.jobPage=lastPage;params.set("page",String(lastPage));
    data=await api("/api/jobs?"+params);
  }
  if(request!==jobSelection.request)return;
  renderJobs(data);
}
async function refresh() {
  if(!ctx.csrf || ctx.busy) return;
  ctx.busy=true;
  try {
    ctx.snapshot=await api("/api/dashboard");renderRooms();renderSummary();
    if(ctx.page==="jobs") await loadJobs();
    globalError("");
  } catch(e){globalError(e.message);}
  finally{ctx.busy=false;}
}
async function openPage(page) {
  ctx.page=page;
  document.querySelectorAll(".page").forEach(e=>e.hidden=e.id!=="page-"+page);
  document.querySelectorAll(".nav-item").forEach(e=>e.classList.toggle("active",e.dataset.page===page));
  if(location.hash!=="#"+page) history.replaceState(null,"","#"+page);
  try{
    if(page==="jobs") await loadJobs();
    if(page==="settings") await loadSettings();
    if(page==="proxy") await loadProxy();
    if(page==="desktop" && ctx.desktop) await loadDesktop();
  }catch(e){globalError(e.message);}
}
function openRoom(room=null) {
  const form=$("#room-form");form.reset();form.dataset.id=room?.id || "";
  form.elements.url.value=room?.url || "";form.elements.url.readOnly=false;
  form.elements.name.value=room?.name || "";form.elements.quality.value=room?.quality || ctx.snapshot?.default_quality || "原画";
  form.elements.use_proxy.checked=room?.uses_proxy || false;
  setText("#room-dialog-title",room ? "编辑直播间" : "新增直播间");
  setText("#save-room",room ? "保存直播间" : "添加并开始监控");
  setText("#room-error","");
  setText("#room-url-hint",room ? "可修改直播间链接；正在录制时请先暂停，等待录像保存完成。YouTube 频道链接会自动规范为 /live。" : "支持录制器已有的直播平台；YouTube 频道链接会自动规范为 /live。");
  setText("#room-proxy-hint",ctx.snapshot?.recorder.proxy_ready ? "使用已保存的代理，账号与密码不会回显。" : "尚未配置共享代理，请先到“代理设置”保存。");
  $("#room-dialog").showModal();
}
async function loadSettings() {
  const data=await api("/api/settings");ctx.settingsRevision=data.revision;
  const form=$("#settings-form");
  for(const [key,value] of Object.entries(data.values)){
    const input=form.elements[key];if(!input)continue;
    if(input.type==="checkbox") input.checked=value;else input.value=value;
  }
  $("#settings-pending").hidden=!data.pending;
  setText("#settings-state",data.pending ? "有设置等待应用" : "配置已读取");
  $("#h265-quality-setting").hidden=data.encoder==="hevc_videotoolbox";
  setText("#quality-unit",data.encoder==="libx265"?"CRF":data.encoder==="hevc_qsv"?"ICQ":"质量");
  updateSettingsDependencies();setText("#settings-error","");
}
function updateSettingsDependencies() {
  const f=$("#settings-form");
  f.elements.segment_seconds.disabled=!f.elements.segment_enabled.checked;
  f.elements.h265.disabled=f.elements.post_format.value!=="mp4";
  f.elements.qsv_quality.disabled=f.elements.post_format.value!=="mp4" || !f.elements.h265.checked;
  f.elements.delete_original.disabled=f.elements.post_format.value!=="mp4";
}
async function loadProxy() {
  const data=await api("/api/proxy");ctx.proxyRevision=data.revision;const f=$("#proxy-form");f.reset();f.elements.server.value=data.server;
  setText("#proxy-state",data.configured ? "已保存共享代理" : "尚未配置");
  setText("#stored-username",data.has_username ? "已保存用户名，留空可保留。" : "没有已保存的用户名。");
  setText("#stored-password",data.has_password ? "已保存密码，留空可保留；密码不会回显。" : "没有已保存的密码。");
  setText("#proxy-error","");
}

$("#logout").addEventListener("click",()=>action($("#logout"),async()=>{await api("/api/logout",{method:"POST",body:"{}"});location.replace("/");}));
for(const [selector,command] of [["#queue-pause","pause"],["#queue-resume","resume"],["#queue-stop","stop"]]){
  $(selector).addEventListener("click",()=>{
    const state=queueActions.control;if(!state || queueActions.busy)return;
    if(command==="stop" && !window.confirm("停止当前转码并暂停后续队列？未完成任务的原录像会保留，当前转码进度会丢弃。已停止的任务重新排队后将从头转码。"))return;
    queueRequest("/api/queue/control",{action:command,revision:state.control_revision,current_job_id:state.current_job_id});
  });
}
$("#queue-apply-sort").addEventListener("click",()=>{
  if(ctx.jobs)queueRequest("/api/queue/order",{revision:ctx.jobs.order_revision,sort:$("#queue-sort").value});
});
$("#select-all-jobs").addEventListener("change",e=>{
  if(jobSelection.deleting)return;
  jobSelection.ids.clear();
  if(e.target.checked) for(const job of ctx.jobs?.items || []) if(["done","failed","stopped"].includes(job.status)) jobSelection.ids.add(job.id);
  syncJobSelection();
});
$("#clear-job-selection").addEventListener("click",clearJobSelection);
$("#delete-selected-jobs").addEventListener("click",async()=>{
  if(jobSelection.deleting || queueActions.busy || !jobSelection.ids.size)return;
  const ids=[...jobSelection.ids];
  if(!window.confirm("删除所选的 "+ids.length+" 条转码记录？原录像和转码后的视频都会保留。"))return;
  jobSelection.deleting=true;if(ctx.jobs)renderJobs(ctx.jobs);else syncJobSelection();
  try {
    const result=await api("/api/jobs/delete-records",{method:"POST",body:JSON.stringify({ids})});
    jobSelection.ids.clear();toast(result.message);await loadJobs();
    ctx.snapshot=await api("/api/dashboard");renderRooms();renderSummary();
  }catch(e){toast(e.message);try{await loadJobs();}catch(error){globalError(error.message);}}
  finally {jobSelection.deleting=false;if(ctx.jobs)renderJobs(ctx.jobs);else syncJobSelection();}
});
document.querySelectorAll(".nav-item").forEach(b=>b.addEventListener("click",()=>openPage(b.dataset.page)));
$("#add-room").addEventListener("click",()=>openRoom());$("#add-first-room").addEventListener("click",()=>openRoom());
for(const id of ["#close-room","#cancel-room"]) $(id).addEventListener("click",()=>$("#room-dialog").close());
$("#close-error").addEventListener("click",()=>$("#error-dialog").close());
$("#room-form").addEventListener("submit",e=>{
  e.preventDefault();const f=e.currentTarget;
  action(e.submitter,async()=>{
    const data={url:f.elements.url.value,name:f.elements.name.value,quality:f.elements.quality.value,
      proxy:f.elements.use_proxy.checked ? "shared" : "direct",revision:ctx.snapshot.revision};
    await api(f.dataset.id ? "/api/rooms/"+f.dataset.id : "/api/rooms",{method:f.dataset.id ? "PATCH" : "POST",body:JSON.stringify(data)});
    $("#room-dialog").close();toast(f.dataset.id ? "直播间已保存，下次检测时生效。" : "直播间已添加，采集器会自动开始检测。");await refresh();
  },"#room-error");
});
$("#settings-form").addEventListener("change",updateSettingsDependencies);
$("#settings-form").addEventListener("submit",e=>{
  e.preventDefault();const f=e.currentTarget;
  action(e.submitter,async()=>{
    const values={};
    for(const key of ["segment_enabled","delete_original","h265"])values[key]=f.elements[key].checked;
    for(const key of ["segment_seconds","poll_seconds","qsv_quality"])values[key]=Number(f.elements[key].value);
    for(const key of ["record_format","quality","post_format"])values[key]=f.elements[key].value;
    const result=await api("/api/settings",{method:"PUT",body:JSON.stringify({values,revision:ctx.settingsRevision})});
    toast(result.message);await loadSettings();await refresh();
  },"#settings-error");
});
$("#proxy-form").addEventListener("submit",e=>{
  e.preventDefault();const f=e.currentTarget;
  action(e.submitter,async()=>{
    const result=await api("/api/proxy",{method:"PUT",body:JSON.stringify({
      server:f.elements.server.value,username:f.elements.username.value,password:f.elements.password.value,
      clear_auth:f.elements.clear_auth.checked,revision:ctx.proxyRevision})});
    f.elements.password.value="";toast(result.message);await loadProxy();await refresh();
  },"#proxy-error");
});
$("#refresh-jobs").addEventListener("click",()=>action($("#refresh-jobs"),refresh));
$("#job-filter").addEventListener("change",()=>{clearJobSelection();ctx.jobPage=1;loadJobs().catch(e=>globalError(e.message));});
$("#job-search").addEventListener("input",()=>{clearJobSelection();jobSelection.request++;clearTimeout(searchTimer);searchTimer=setTimeout(()=>{ctx.jobPage=1;loadJobs().catch(e=>globalError(e.message));},350);});
$("#previous-page").addEventListener("click",()=>{clearJobSelection();ctx.jobPage--;loadJobs().catch(e=>globalError(e.message));});
$("#next-page").addEventListener("click",()=>{clearJobSelection();ctx.jobPage++;loadJobs().catch(e=>globalError(e.message));});

function renderDesktopStatus(status){
  if(!ctx.desktop || !status)return;
  setText("#desktop-state",status.stopping ? "正在保存录像" : status.running ? "采集器运行中" : "采集器已停止");
  $("#desktop-start").disabled=status.running || status.stopping || !status.ffmpeg_ready || !status.node_ready;
  $("#desktop-stop").disabled=!status.running || status.stopping;
}
async function loadDesktop(){
  const data=await api("/api/desktop");ctx.desktopData=data;
  const form=$("#desktop-form");
  const select=form.elements.encoder;select.replaceChildren(node("option","自动检测硬件，必要时使用 CPU"));
  select.firstChild.value="auto";
  for(const item of data.encoders){const o=node("option",item.label);o.value=item.value;select.append(o);}
  if(data.values.encoder!=="auto" && !data.encoders.some(i=>i.value===data.values.encoder)){
    const o=node("option","当前编码器不可用："+data.values.encoder);o.value=data.values.encoder;select.append(o);
  }
  for(const [key,value] of Object.entries(data.values)){
    const input=form.elements[key];if(!input)continue;
    if(input.type==="checkbox")input.checked=value;else input.value=value;
  }
  setText("#desktop-tools",(data.ffmpeg_path || "未找到 FFmpeg")+" · Node.js "+(data.status.node_ready?"已就绪":"未找到"));
  setText("#desktop-data-dir","应用数据目录："+data.data_dir);
  $("#videotoolbox-bitrate").hidden=data.platform!=="darwin";
  renderDesktopStatus(data.status);setText("#desktop-error","");
}
$("#desktop-form").addEventListener("submit",event=>{
  event.preventDefault();const form=event.currentTarget;
  action(event.submitter,async()=>{
    const values={};
    for(const key of ["downloads","ffmpeg","encoder"])values[key]=form.elements[key].value;
    for(const key of ["bitrate_kbps","cpu_threads"])values[key]=Number(form.elements[key].value);
    for(const key of ["auto_start","close_to_tray","start_minimized","prevent_sleep"])values[key]=form.elements[key].checked;
    const result=await api("/api/desktop",{method:"PUT",body:JSON.stringify({values,revision:ctx.desktopData.revision})});
    await window.recorderDesktop?.preferencesChanged();toast(result.message);await loadDesktop();await refresh();
  },"#desktop-error");
});
for(const [selector,endpoint] of [["#desktop-start","start"],["#desktop-stop","stop"]])
  $(selector).addEventListener("click",()=>action($(selector),async()=>{
    const result=await api("/api/desktop/"+endpoint,{method:"POST",body:"{}"});toast(result.message);await loadDesktop();await refresh();
  }));
$("#choose-downloads").addEventListener("click",()=>action($("#choose-downloads"),async()=>{
  const value=await window.recorderDesktop?.chooseFolder();if(value)$("#desktop-form").elements.downloads.value=value;
}));
$("#choose-ffmpeg").addEventListener("click",()=>action($("#choose-ffmpeg"),async()=>{
  const value=await window.recorderDesktop?.chooseFFmpeg();if(value)$("#desktop-form").elements.ffmpeg.value=value;
}));
$("#desktop-recordings").addEventListener("click",()=>window.recorderDesktop?.openRecordings());
$("#desktop-logs").addEventListener("click",()=>window.recorderDesktop?.openLogs());
$("#test-encoder").addEventListener("click",()=>action($("#test-encoder"),async()=>{
  const value=$("#desktop-form").elements.encoder.value;
  const encoder=value==="auto"?ctx.desktopData.status.encoder:value;
  const result=await api("/api/desktop/test-encoder",{method:"POST",body:JSON.stringify({encoder})});toast(result.message);
},"#desktop-error"));

async function initialize(){
  try{
    const session=await api("/api/session");ctx.desktop=!!session.desktop;$("#desktop-nav").hidden=!ctx.desktop;$("#logout").hidden=ctx.desktop;
    if(ctx.desktop){setText("#workspace-name","我的电脑");setText("#app-version","DouyinLiveRecorder Desktop 1.1.2 · Core v4.0.7");}$("#setup-notice").hidden=!session.setup_required;
    $("#environment-banner").hidden=session.environment!=="development";
    if(session.authenticated){ctx.csrf=session.csrf;showApp();await refresh();
      const requested=location.hash.slice(1);await openPage(["live","jobs","settings","proxy",...(ctx.desktop?["desktop"]:[])].includes(requested) ? requested : "live");
    } else showLogin();
  }catch(e){showLogin();setText("#login-error",e.message);}
}
setInterval(()=>{if(!document.hidden)refresh();},5000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
initialize();
