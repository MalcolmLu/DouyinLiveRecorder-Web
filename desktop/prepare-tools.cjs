"use strict";
const fs=require("node:fs");
const path=require("node:path");
const https=require("node:https");
const crypto=require("node:crypto");
const {execFileSync}=require("node:child_process");
const {pipeline}=require("node:stream/promises");
const tools=path.join(__dirname,"tools");
fs.mkdirSync(tools,{recursive:true});
function get(url){
  return new Promise((resolve,reject)=>{
    https.get(url,response=>{
      if(response.statusCode>=300 && response.statusCode<400 && response.headers.location){
        response.resume();get(new URL(response.headers.location,url).href).then(resolve,reject);return;
      }
      if(response.statusCode!==200){response.resume();reject(new Error("Download failed: "+response.statusCode));return;}
      resolve(response);
    }).on("error",reject);
  });
}
async function text(url){const response=await get(url);let result="";for await(const chunk of response)result+=chunk;return result;}
async function download(url,target){const response=await get(url);await pipeline(response,fs.createWriteStream(target));}
async function main(){
  const ext=process.platform==="win32"?".exe":"";
  if(process.platform==="darwin"){
    execFileSync(process.env.DLR_PYTHON || "python3",[path.join(__dirname,"prepare_macos_tools.py")],{stdio:"inherit"});
  }else{
    const ffmpeg=require("ffmpeg-static"),ffprobe=require("ffprobe-static").path;
    for(const [source,name] of [[ffmpeg,"ffmpeg"],[ffprobe,"ffprobe"]]){
      const version=execFileSync(source,["-version"],{encoding:"utf8",stdio:["ignore","pipe","pipe"]});
      if(version.includes("--enable-nonfree"))throw new Error("Non-redistributable FFmpeg build rejected");
      fs.copyFileSync(source,path.join(tools,name+ext));fs.chmodSync(path.join(tools,name+ext),0o755);
    }
    const licenses=path.join(tools,"licenses");fs.mkdirSync(licenses,{recursive:true});
    for(const file of ["ffmpeg.LICENSE","ffmpeg.README"]){
      const source=path.join(path.dirname(ffmpeg),file);
      if(fs.existsSync(source))fs.copyFileSync(source,path.join(licenses,file));
    }
    fs.copyFileSync(path.join(__dirname,"node_modules/ffprobe-static/LICENSE"),path.join(licenses,"ffprobe-static-LICENSE"));
    fs.writeFileSync(path.join(tools,"THIRD-PARTY-SOURCES.txt"),
      "FFmpeg tools: ffmpeg-static@5.3.0 and ffprobe-static@3.1.0.\n"+
      "Source and binary build scripts: https://github.com/eugeneware/ffmpeg-static/tree/b6.1.1\n"+
      "https://github.com/joshwnj/ffprobe-static/tree/v3.1.0\n");
  }
  const index=JSON.parse(await text("https://nodejs.org/dist/index.json"));
  const release=index.find(item=>item.version.startsWith("v22.") && item.lts);
  if(!release)throw new Error("No Node.js 22 LTS release found");
  const platform=process.platform==="win32"?"win":"darwin";
  const archive="node-"+release.version+"-"+platform+"-"+process.arch+(platform==="win"?".zip":".tar.gz");
  const url="https://nodejs.org/dist/"+release.version+"/"+archive;
  const file=path.join(tools,archive);
  await download(url,file);
  const shas=await text("https://nodejs.org/dist/"+release.version+"/SHASUMS256.txt");
  const expected=shas.split("\n").find(line=>line.endsWith("  "+archive))?.split(" ")[0];
  const actual=crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  if(!expected || expected!==actual)throw new Error("Node.js archive checksum mismatch");
  const extraction=path.join(tools,"node-extract");fs.mkdirSync(extraction,{recursive:true});
  if(platform==="win"){
    execFileSync("powershell",["-NoProfile","-NonInteractive","-Command",
      "Expand-Archive -LiteralPath '"+file.replace(/'/g,"''")+"' -DestinationPath '"+extraction.replace(/'/g,"''")+"' -Force"]);
  }else execFileSync("tar",["-xzf",file,"-C",extraction]);
  const folder=path.join(extraction,"node-"+release.version+"-"+platform+"-"+process.arch);
  fs.copyFileSync(path.join(folder,platform==="win"?"node.exe":"bin/node"),path.join(tools,"node"+ext));
  fs.chmodSync(path.join(tools,"node"+ext),0o755);
  fs.copyFileSync(path.join(folder,"LICENSE"),path.join(tools,"NODE-LICENSE"));
  const media=process.platform==="darwin" ? {homebrew:JSON.parse(fs.readFileSync(path.join(tools,"ffmpeg-provenance.json"),"utf8"))} :
    {ffmpeg:{npm:"ffmpeg-static@5.3.0",release:"b6.1.1"},ffprobe:{npm:"ffprobe-static@3.1.0"}};
  fs.writeFileSync(path.join(tools,"provenance.json"),JSON.stringify({node:{version:release.version,url,sha256:actual},...media},null,2));
  fs.rmSync(file);fs.rmSync(extraction,{recursive:true});
  for(const binary of ["ffmpeg","ffprobe","node"])execFileSync(path.join(tools,binary+ext),[binary==="node"?"--version":"-version"],{stdio:"ignore"});
  console.log("Bundled FFmpeg, ffprobe and SHA256-verified Node.js for "+process.platform+"/"+process.arch);
}
main().catch(error=>{console.error(error.message);process.exit(1);});
