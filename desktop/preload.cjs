const {contextBridge,ipcRenderer}=require("electron");
contextBridge.exposeInMainWorld("recorderDesktop",Object.freeze({
  chooseFolder:()=>ipcRenderer.invoke("desktop:choose-folder"),
  chooseFFmpeg:()=>ipcRenderer.invoke("desktop:choose-ffmpeg"),
  openRecordings:()=>ipcRenderer.invoke("desktop:open-recordings"),
  openLogs:()=>ipcRenderer.invoke("desktop:open-logs"),
  preferencesChanged:()=>ipcRenderer.invoke("desktop:preferences-changed"),
}));
