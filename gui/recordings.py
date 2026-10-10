"""Read-only, room-scoped recording folders and media lookup."""
from pathlib import Path, PurePosixPath
import re

from platform_utils import recording_roots
from shared import AppError, read_json


MEDIA_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".ts": "video/mp2t",
               ".flv": "video/x-flv", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
PLATFORM_FOLDERS = {"哔哩哔哩": "B站直播", "虎牙": "虎牙直播", "YouTube": "Youtube",
                    "抖音": "抖音直播", "快手": "快手直播", "斗鱼": "斗鱼直播",
                    "Twitch": "TwitchTV", "TikTok": "TikTok直播"}


def author_name(value, remove_emoji=True):
    value = re.sub(r"^序号\d+\s*", "", str(value or ""))
    value = re.sub(r'[\/\\\:\*\？?"\<\>\|&#.。,， ~！·\u00a0]', "_", value.strip()).strip("_")
    value = value.replace("（", "(").replace("）", ")")
    if remove_emoji:
        value = re.sub(r"[\U0001f1e0-\U0001f1ff\U0001f300-\U0001faff\u2702-\u27b0]+", "_", value).strip("_")
    return value or "空白昵称"


class RecordingsService:
    page_size = 25

    def __init__(self, paths, config):
        self.paths, self.config = paths, config

    def _context(self, identifier):
        with self.config.lock:
            room = next((r for r in self.config.rooms() if r["id"] == identifier), None)
            by_author = self.config._section().get("保存文件夹是否以作者区分", "是") == "是"
            remove_emoji = self.config._section().get("是否去除名称中的表情符号", "是") == "是"
        if room is None:
            raise AppError("直播间不存在，请刷新直播任务。", "not_found", 404)
        runtime = read_json(self.paths.state / "runtime.json", {}).get("rooms", {}).get(identifier, {})
        names = {author_name(n, remove_emoji) for n in (room.get("_name_value"), room["name"], runtime.get("name")) if n}
        names.discard("")
        platforms = {PLATFORM_FOLDERS.get(room["platform"], room["platform"]), room["platform"], runtime.get("platform", "")}
        platforms = {p for p in platforms if p and p not in (".", "..") and not re.search(r"[/\\\x00-\x1f]", p)}
        roots = recording_roots(self.paths.config, self.paths.downloads)
        locations, seen_directories = [], set()
        for index, root in enumerate(roots):
            for platform in sorted(platforms):
                base = root / platform
                candidates = [base / name for name in sorted(names)] if by_author else [base]
                for candidate in candidates:
                    if candidate.is_symlink() or not candidate.is_dir():
                        continue
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(root):
                        continue
                    # macOS commonly preserves case in paths while treating
                    # YouTube and Youtube as the same filesystem directory.
                    try:
                        stat = resolved.stat()
                    except (FileNotFoundError, PermissionError):
                        continue
                    identity = (stat.st_dev, stat.st_ino) if stat.st_ino else resolved
                    if identity in seen_directories:
                        continue
                    seen_directories.add(identity)
                    locations.append({"root": root, "index": index, "base": resolved,
                                      "prefixes": None if by_author else tuple(n + "_" for n in names)})
        return room, locations

    @staticmethod
    def _token(location, path):
        return str(location["index"]) + "/" + path.relative_to(location["root"]).as_posix()

    def _resolve(self, token, locations):
        if not isinstance(token, str) or len(token) > 4096 or re.search(r"[\\\x00-\x1f]", token):
            raise AppError("录像路径不正确。", "invalid_path", 422)
        parts = token.split("/")
        if len(parts) < 2 or any(p in ("", ".", "..") for p in parts) or not parts[0].isdigit():
            raise AppError("录像路径不正确。", "invalid_path", 422)
        for location in locations:
            if parts[0] != str(location["index"]):
                continue
            path = (location["root"] / PurePosixPath(*parts[1:])).resolve()
            if path.is_relative_to(location["root"]) and path.is_relative_to(location["base"]):
                return location, path
        raise AppError("找不到该直播间的录像。", "not_found", 404)

    @staticmethod
    def _media(path, location):
        return (not path.name.startswith(".") and path.suffix.lower() in MEDIA_TYPES
                and not path.name.lower().endswith((".tmp.mp4", ".part.mp4"))
                and (location["prefixes"] is None or path.name.startswith(location["prefixes"])))

    def list(self, identifier, folder="", page=1):
        room, locations = self._context(identifier)
        if not isinstance(page, int) or not 1 <= page <= 100000:
            raise AppError("页码不正确。")
        if not folder and len(locations) == 1:
            folder = self._token(locations[0], locations[0]["base"])
        items, crumbs, parent = [], [], None
        if not folder:
            for location in locations:
                items.append({"kind": "folder", "name": location["base"].relative_to(location["root"]).as_posix(),
                              "path": self._token(location, location["base"]), "size": None, "modified": None})
            crumbs = [{"name": "录像文件夹", "path": ""}]
        else:
            location, path = self._resolve(folder, locations)
            if not path.is_dir():
                raise AppError("录像文件夹已不存在，请刷新。", "not_found", 404)
            base = location["base"]
            crumbs = ([{"name": "全部录像目录", "path": ""}] if len(locations) > 1 else [])
            crumbs.append({"name": base.name, "path": self._token(location, base)})
            cursor = base
            for part in path.relative_to(base).parts:
                cursor /= part
                crumbs.append({"name": part, "path": self._token(location, cursor)})
            parent = self._token(location, path.parent) if path != base else "" if len(locations) > 1 else None
            try:
                for entry in path.iterdir():
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    try:
                        if not entry.resolve().is_relative_to(base):
                            continue
                        directory = entry.is_dir()
                        if not directory and (not entry.is_file() or not self._media(entry, location)):
                            continue
                        stat = entry.stat()
                        items.append({"kind": "folder" if directory else "file", "name": entry.name,
                                      "path": self._token(location, entry), "size": None if directory else stat.st_size,
                                      "modified": stat.st_mtime, "format": entry.suffix[1:].upper() if not directory else None})
                    except (FileNotFoundError, PermissionError):
                        continue  # A finished transcode may replace a file during listing.
            except (FileNotFoundError, PermissionError):
                raise AppError("暂时无法读取录像文件夹，请刷新后重试。", "unavailable", 503)
        items.sort(key=lambda item: (item["kind"] != "folder", -(item["modified"] or 0), item["name"]))
        start = (page - 1) * self.page_size
        return {"room": {"id": identifier, "name": room["name"]}, "folder": folder,
                "breadcrumbs": crumbs, "parent": parent, "items": items[start:start + self.page_size],
                "total": len(items), "page": page, "page_size": self.page_size}

    def media(self, identifier, token):
        _, locations = self._context(identifier)
        location, path = self._resolve(token, locations)
        if not path.is_file() or not self._media(path, location):
            raise AppError("录像文件不存在，可能已被转码或移动，请刷新列表。", "not_found", 404)
        return path, MEDIA_TYPES[path.suffix.lower()]
