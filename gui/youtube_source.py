"""YouTube live metadata and separate HLS audio/video input adaptation."""
import asyncio
from http.cookies import SimpleCookie
from http.cookiejar import Cookie
from pathlib import Path
import sys

from shared import sanitize_error


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def live_data(info):
    if not isinstance(info, dict):
        raise ValueError("YouTube 未返回有效的直播信息。")
    name = info.get("channel") or info.get("uploader") or "YouTube"
    if not info.get("is_live"):
        return {"anchor_name":name, "is_live":False}
    formats = info.get("formats", [])
    video = [f for f in formats if f.get("url") and str(f.get("protocol", "")).startswith("m3u8")
             and str(f.get("vcodec", "")).startswith(("avc", "h264"))]
    video.sort(key=lambda f:(f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    if not video:
        raise ValueError("YouTube 直播未提供可用的 H.264 HLS 视频流，请检查代理或稍后重试。")
    muxed = [f for f in video if f.get("acodec") not in (None, "none")]
    if muxed:
        video, audio = muxed, None
    else:
        candidates = [f for f in formats if f.get("url") and f.get("vcodec") == "none"
                      and str(f.get("protocol", "")).startswith("m3u8")]
        if not candidates:
            raise ValueError("YouTube 直播缺少音频流，已取消录制以避免保存无声视频。")
        audio = max(candidates, key=lambda f:f.get("abr") or f.get("tbr") or 0)["url"]
    # Upstream's five quality levels select descending video resolutions.
    distinct = {}
    for entry in video:
        distinct.setdefault(entry.get("height"), entry["url"])
    urls = list(distinct.values())
    return {"anchor_name":name, "is_live":True, "title":info.get("title", ""),
            "m3u8_url":urls[0], "flv_url":urls[0], "play_url_list":urls, "audio_url":audio}


def extract_sync(url, proxy=None, cookies=None):
    vendor = Path(__file__).resolve().parent / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    import yt_dlp
    options = {"proxy":proxy or "", "quiet":True, "skip_download":True,
               "socket_timeout":20, "noplaylist":True, "logger":QuietLogger(),
               "ignore_no_formats_error":True, "cachedir":False}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            if cookies:
                parsed = SimpleCookie(); parsed.load(cookies)
                for key, value in parsed.items():
                    downloader.cookiejar.set_cookie(Cookie(0,key,value.value,None,False,".youtube.com",
                        True,True,"/",True,True,None,True,None,None,{},False))
            info = downloader.extract_info(url, download=False)
        return live_data(info)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        if "not currently live" in message or "This live event will begin" in message:
            return {"anchor_name":url.split("/")[3], "is_live":False}
        raise ValueError("YouTube 解析失败：" + sanitize_error(message)) from None


async def extract_live(url, proxy=None, cookies=None):
    return await asyncio.to_thread(extract_sync, url, proxy, cookies)


def with_audio(command, audio):
    """Copy network input options, then map both tracks for video recordings."""
    command = list(command)
    position = command.index("-i")
    inputs = []
    paired = {"-http_proxy", "-rw_timeout", "-user_agent", "-protocol_whitelist", "-headers",
              "-thread_queue_size", "-analyzeduration", "-probesize", "-fflags"}
    index = 1
    while index < position:
        item = command[index]
        if item in paired:
            inputs.extend(command[index:index+2]); index += 2
        else:
            if item == "-re": inputs.append(item)
            index += 1
    command[position+2:position+2] = inputs + ["-i", audio]
    result = []; index = 0; mapped = False
    while index < len(command):
        if command[index] == "-map" and index+1 < len(command):
            target = command[index+1]
            if target == "0":
                result += ["-map", "0:v:0", "-map", "1:a:0"]
            elif target in ("0:a", "0:a:0"):
                result += ["-map", "1:a:0"]
            else:
                result += command[index:index+2]
            mapped = True; index += 2
        else:
            result.append(command[index]); index += 1
    if not mapped:
        result[-1:-1] = ["-map", "0:v:0", "-map", "1:a:0"]
    return result
