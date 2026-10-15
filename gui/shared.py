"""Shared filesystem contract between the recorder and its web console."""
import configparser
from contextlib import contextmanager
from dataclasses import dataclass
from platform_utils import acquire_lock, release_lock, sync_directory
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit


class AppError(Exception):
    def __init__(self, message, code="validation", status=422):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


@dataclass(frozen=True)
class Paths:
    config: Path
    downloads: Path
    state: Path

    @classmethod
    def from_env(cls):
        config = Path(os.environ.get("RECORDER_CONFIG_DIR", "/app/config")).resolve()
        downloads = Path(os.environ.get("RECORDER_DOWNLOADS_DIR", "/app/downloads")).resolve()
        state = Path(os.environ.get("GUI_STATE_DIR", str(config / "gui/state"))).resolve()
        return cls(config, downloads, state)

    @property
    def queue(self):
        return self.config / "h265/queue.sqlite3"


class SharedLock:
    """Reentrant in-process mutex plus a cross-process advisory lock."""
    def __init__(self, path):
        self.path = Path(path)
        self.mutex = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.mutex.acquire()
        depth = getattr(self.local, "depth", 0)
        try:
            if not depth:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.local.file = self.path.open("a")
                acquire_lock(self.local.file)
            self.local.depth = depth + 1
        except BaseException:
            self.mutex.release()
            raise
        return self

    def __exit__(self, *args):
        self.local.depth -= 1
        if not self.local.depth:
            release_lock(self.local.file)
            self.local.file.close()
        self.mutex.release()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {} if default is None else default


def atomic_write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


HOSTS = {
    "live.douyin.com", "v.douyin.com", "www.douyin.com", "live.kuaishou.com",
    "www.huya.com", "www.douyu.com", "www.yy.com", "live.bilibili.com",
    "www.redelight.cn", "www.xiaohongshu.com", "xhslink.com", "www.bigo.tv",
    "slink.bigovideo.tv", "app.blued.cn", "cc.163.com", "qiandurebo.com",
    "fm.missevan.com", "look.163.com", "twitcasting.tv", "live.baidu.com",
    "weibo.com", "fanxing.kugou.com", "fanxing2.kugou.com", "mfanxing.kugou.com",
    "www.huajiao.com", "www.7u66.com", "wap.7u66.com", "live.acfun.cn", "m.acfun.cn",
    "live.tlclw.com", "wap.tlclw.com", "live.ybw1666.com", "wap.ybw1666.com",
    "www.inke.cn", "www.zhihu.com", "www.haixiutv.com", "h5webcdnp.vvxqiu.com",
    "17.live", "www.lang.live", "m.pp.weimipopo.com", "v.6.cn", "m.6.cn",
    "www.lehaitv.com", "h.catshow168.com", "e.tb.cn", "huodong.m.taobao.com",
    "3.cn", "eco.m.jd.com", "www.miguvideo.com", "m.miguvideo.com",
    "show.lailianjie.com", "www.imkktv.com", "www.picarto.tv", "www.tiktok.com",
    "play.sooplive.co.kr", "m.sooplive.co.kr", "www.sooplive.com", "m.sooplive.com",
    "www.pandalive.co.kr", "www.winktv.co.kr", "www.flextv.co.kr", "www.ttinglive.com",
    "www.popkontv.com", "www.twitch.tv", "www.liveme.com", "www.showroom-live.com",
    "chzzk.naver.com", "m.chzzk.naver.com", "www.youtube.com", "youtu.be",
    "www.faceit.com",
}
QUALITIES = ("原画", "蓝光", "超清", "高清", "标清", "流畅")
PROXY_MODES = ("inherit", "direct", "shared")
RECORD_KEYS = {
    "segment_enabled": "分段录制是否开启",
    "segment_seconds": "视频分段时间(秒)",
    "record_format": "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频",
    "quality": "原画|超清|高清|标清|流畅",
    "poll_seconds": "循环时间(秒)",
    "post_format": "录制完成后自动转为mp4格式",
    "delete_original": "追加格式后删除原文件",
}


def canonical_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise AppError("请输入直播间链接。")
    if re.search(r"[\s,，\x00-\x1f\x7f]", value):
        raise AppError("直播间链接不能包含空格、换行或逗号。")
    try:
        u = urlsplit(value.strip())
        port = u.port
    except ValueError:
        raise AppError("直播间链接格式不正确。")
    if u.scheme not in ("http", "https") or u.username or u.password or port:
        raise AppError("请使用不含账号和端口的 HTTP 或 HTTPS 直播间链接。")
    host = (u.hostname or "").lower()
    aliases = {"youtube.com": "www.youtube.com", "huya.com": "www.huya.com",
               "twitch.tv": "www.twitch.tv", "douyu.com": "www.douyu.com"}
    host = aliases.get(host, host)
    if host not in HOSTS and not (
        host.startswith("live.shopee.") and host.split(".")[-1] in ("com", "tw", "vn", "th", "ph", "sg", "my", "id", "br")
        or host.endswith(".shp.ee") and host.count(".") == 2
    ):
        raise AppError("此链接不属于当前录制器支持的直播平台。")
    path = u.path.rstrip("/") or "/"
    if host == "www.youtube.com" and path.startswith(("/@", "/channel/")):
        path = re.sub(r"/(?:streams|videos|featured|live)$", "", path) + "/live"
    if host in ("live.bilibili.com", "www.huya.com", "live.douyin.com", "www.douyu.com"):
        query = ""
    else:
        query = u.query
    return urlunsplit(("https", host, path, query, ""))


def room_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def clean_label(value):
    if not isinstance(value, str) or len(value) > 80 or re.search(r"[\r\n\x00-\x1f,，#]", value):
        raise AppError("备注最多 80 个字符，不能包含换行、逗号或 #。")
    return value.strip()


def platform(url):
    host = urlsplit(url).hostname or ""
    for domain, label in (("bilibili", "哔哩哔哩"), ("huya", "虎牙"), ("youtube", "YouTube"),
                          ("douyin", "抖音"), ("douyu", "斗鱼"), ("kuaishou", "快手"),
                          ("twitch", "Twitch"), ("tiktok", "TikTok")):
        if domain in host:
            return label
    return host


def read_ini(path):
    parser = configparser.RawConfigParser()
    parser.read(path, encoding="utf-8-sig")
    return parser


def sanitize_error(value):
    text = str(value or "")
    text = re.sub(r"https?://[^\s'\"<>]+", "[链接已隐藏]", text)
    text = text.replace("/app/downloads/", "录像/").replace("/app/config/", "配置/")
    return text[:1200]
