"""Recording, room and proxy business rules over an injected file repository."""
import hashlib
import re
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from shared import AppError, PROXY_MODES, QUALITIES, RECORD_KEYS, canonical_url, clean_label, platform, room_id


class ConfigService:
    def __init__(self, repository):
        self.store = repository
        self.paths = repository.paths
        self.lock = repository.lock
        for name in ("urls", "ini", "metadata", "h265", "pending", "deleted"):
            setattr(self, name, getattr(repository, name))

    def revision(self):
        digest = hashlib.sha256()
        for path in (self.urls, self.ini, self.metadata, self.h265, self.pending, self.deleted):
            digest.update(str(path.name).encode())
            digest.update(self.store.bytes(path) if self.store.exists(path) else b"")
        return digest.hexdigest()

    def check_revision(self, expected):
        if expected != self.revision():
            raise AppError("配置已被更新，请刷新后再保存。", "conflict", 409)

    def _section(self):
        with self.lock:
            p = self.store.ini_read(self.ini)
            return p["录制设置"] if p.has_section("录制设置") else {}

    def rooms(self):
        data = self.store.json_read(self.metadata, {})
        result = []
        seen = set()
        default_quality = self._section().get(RECORD_KEYS["quality"], "原画")
        for index, raw in enumerate(self.store.text(self.urls).splitlines() if self.store.exists(self.urls) else []):
            enabled = not raw.lstrip().startswith("#")
            line = raw.lstrip().lstrip("#").strip()
            match = re.search(r"https?://[^,，\s]+", line)
            if not match:
                continue
            try:
                url = canonical_url(match.group())
            except AppError:
                continue
            if url in seen:
                continue
            seen.add(url)
            before = line[:match.start()].strip(" ,，")
            quality = before if before in QUALITIES else default_quality
            name = re.sub(r"^(?:主播|备注)\s*[:：]\s*", "", line[match.end():].strip(" ,，"))
            meta = data.get(url, {})
            result.append({
                "id": room_id(url), "url": url, "name": meta.get("name") or name or urlsplit(url).path.strip("/"),
                "quality": quality, "enabled": enabled, "proxy": meta.get("proxy", "inherit"),
                "platform": platform(url), "_line": index, "_name_value": meta.get("name") or name,
            })
        return result

    def proxy_url(self):
        return self._section().get("代理地址", "").strip()

    def uses_proxy(self, room):
        if room["proxy"] == "direct":
            return False
        if room["proxy"] == "shared":
            return bool(self.proxy_url())
        section = self._section()
        primary = section.get("使用代理录制的平台(逗号分隔)", "")
        extra = section.get("额外使用代理录制的平台(逗号分隔)", "")
        enabled = section.get("是否使用代理ip(是/否)", "否") == "是"
        return bool(self.proxy_url()) and (
            enabled and any(x.strip() and x.strip() in room["url"] for x in primary.split(","))
            or any(x.strip() and x.strip() in room["url"] for x in extra.split(",")))

    def proxy_public(self):
        value = self.proxy_url()
        if not value:
            return {"server": "", "has_username": False, "has_password": False, "configured": False}
        try:
            u = urlsplit(value)
            host = ("[" + u.hostname + "]") if ":" in (u.hostname or "") else u.hostname
            server = urlunsplit((u.scheme, (host or "") + (":" + str(u.port) if u.port else ""), "", "", ""))
            return {"server": server, "has_username": bool(u.username),
                    "has_password": bool(u.password), "configured": True}
        except ValueError:
            return {"server": "", "has_username": False, "has_password": False,
                    "configured": False, "invalid": True}

    def settings(self):
        s = self._section()
        h = self.store.json_read(self.h265, {})
        actual = {
            "segment_enabled": s.get(RECORD_KEYS["segment_enabled"], "是") == "是",
            "segment_seconds": int(s.get(RECORD_KEYS["segment_seconds"], "3600")),
            "record_format": s.get(RECORD_KEYS["record_format"], "TS").upper(),
            "quality": s.get(RECORD_KEYS["quality"], "原画"),
            "poll_seconds": int(s.get(RECORD_KEYS["poll_seconds"], "300")),
            "post_format": "mp4" if s.get(RECORD_KEYS["post_format"], "是") == "是" else "keep",
            "delete_original": s.get(RECORD_KEYS["delete_original"], "是") == "是",
            "h265": h.get("enabled", True), "qsv_quality": int(h.get("qsv_quality", 24)),
        }
        pending = self.store.json_read(self.pending, {})
        desired = dict(actual, **pending.get("values", {}))
        return {"values": desired, "actual": actual, "pending": bool(pending),
                "revision": self.revision(), "encoder": h.get("encoder", "unknown")}

    def _change_ini(self, values):
        text = self.store.text(self.ini) if self.store.exists(self.ini) else "[录制设置]\n"
        lines = text.splitlines()
        start = next((i for i, s in enumerate(lines) if s.strip() == "[录制设置]"), None)
        if start is None:
            lines += ["", "[录制设置]"]
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[.*\]\s*$", lines[i])), len(lines))
        section = lines[start + 1:end]
        for key, value in values.items():
            replaced = False
            for i, line in enumerate(section):
                if re.match(r"^\s*" + re.escape(key) + r"\s*=", line):
                    section[i] = key + " = " + str(value)
                    replaced = True
                    break
            if not replaced:
                section.append(key + " = " + str(value))
        lines[start + 1:end] = section
        self.store.write(self.ini, "\n".join(lines) + "\n")

    def create_room(self, body):
        with self.lock:
            self.check_revision(body.get("revision"))
            url = canonical_url(body.get("url"))
            name = clean_label(body.get("name", ""))
            quality = body.get("quality", "原画")
            mode = body.get("proxy", "direct")
            if quality not in QUALITIES or mode not in PROXY_MODES:
                raise AppError("请选择有效的画质和代理方式。")
            if any(r["url"] == url for r in self.rooms()):
                raise AppError("这个直播间已经添加过了。", "duplicate", 409)
            if mode == "shared" and not self.proxy_public()["configured"]:
                raise AppError("请先保存代理服务器设置，再为直播间启用代理。")
            text = self.store.text(self.urls) if self.store.exists(self.urls) else ""
            if text and not text.endswith("\n"):
                text += "\n"
            text += quality + "," + url + (",主播: " + name if name else "") + "\n"
            meta = self.store.json_read(self.metadata, {})
            meta[url] = {"proxy": mode, "name": name}
            self.store.json_write(self.metadata, meta)
            self.store.write(self.urls, text)
            return room_id(url)

    def update_room(self, identifier, body):
        with self.lock:
            self.check_revision(body.get("revision"))
            row = next((r for r in self.rooms() if r["id"] == identifier), None)
            if not row:
                raise AppError("直播间不存在，请刷新列表。", "not_found", 404)
            url = canonical_url(body.get("url", row["url"]))
            if url != row["url"]:
                if any(r["url"] == url and r["id"] != identifier for r in self.rooms()):
                    raise AppError("这个直播间已经添加过了。", "duplicate", 409)
                self.check_idle_room(identifier)
            name = clean_label(body.get("name", row["name"]))
            quality = body.get("quality", row["quality"])
            mode = body.get("proxy", row["proxy"])
            enabled = body.get("enabled", row["enabled"])
            if quality not in QUALITIES or mode not in PROXY_MODES or type(enabled) is not bool:
                raise AppError("直播间设置不正确。")
            if mode == "shared" and not self.proxy_public()["configured"]:
                raise AppError("请先保存代理服务器设置。")
            lines = self.store.text(self.urls).splitlines()
            lines[row["_line"]] = ("" if enabled else "# ") + quality + "," + url + (",主播: " + name if name else "")
            meta = self.store.json_read(self.metadata, {})
            if url != row["url"]:
                meta.pop(row["url"], None)
            meta[url] = {"proxy": mode, "name": name}
            self.store.json_write(self.metadata, meta)
            self.store.write(self.urls, "\n".join(lines) + "\n")
            return room_id(url)

    def check_idle_room(self, identifier):
        runtime = self.store.json_read(self.paths.state / "runtime.json", {})
        active = runtime.get("rooms", {}).get(identifier, {})
        if time.time() - runtime.get("heartbeat", 0) < 20 and active.get("state") == "recording":
            raise AppError("当前直播间正在录制，请先暂停，等待录像保存完成后再修改链接或删除。", "recording", 409)

    def deleted_rooms(self):
        return sorted(self.store.json_read(self.deleted, {}).values(), key=lambda r:r["deleted_at"], reverse=True)

    def delete_room(self, identifier, body):
        with self.lock:
            self.check_revision(body.get("revision"))
            row = next((r for r in self.rooms() if r["id"] == identifier), None)
            if not row:
                raise AppError("直播间不存在，请刷新列表。", "not_found", 404)
            self.check_idle_room(identifier)
            archive = self.store.json_read(self.deleted, {})
            archive[identifier] = {k:row[k] for k in ("id", "url", "name", "quality", "proxy", "platform")}
            archive[identifier]["deleted_at"] = time.time()
            self.store.json_write(self.deleted, archive)
            lines = self.store.text(self.urls).splitlines()
            # Remove all canonical duplicates, including old hand-written rows.
            retained = []
            for line in lines:
                match = re.search(r"https?://[^,，\s]+", line)
                try:
                    same = bool(match) and canonical_url(match.group()) == row["url"]
                except AppError:
                    same = False
                if not same:
                    retained.append(line)
            self.store.write(self.urls, "\n".join(retained) + ("\n" if retained else ""))
            meta = self.store.json_read(self.metadata, {})
            meta.pop(row["url"], None)
            self.store.json_write(self.metadata, meta)

    def restore_room(self, identifier, body):
        with self.lock:
            self.check_revision(body.get("revision"))
            archive = self.store.json_read(self.deleted, {})
            row = archive.get(identifier)
            if not row:
                raise AppError("没有找到已删除的直播间。", "not_found", 404)
            url = canonical_url(row["url"])
            if any(r["url"] == url for r in self.rooms()):
                raise AppError("这个直播间已在当前列表中，无需重复恢复。", "duplicate", 409)
            name = clean_label(row["name"])
            text = self.store.text(self.urls) if self.store.exists(self.urls) else ""
            if text and not text.endswith("\n"):
                text += "\n"
            text += "# " + row["quality"] + "," + url + (",主播: " + name if name else "") + "\n"
            meta = self.store.json_read(self.metadata, {})
            meta[url] = {"name":name, "proxy":row["proxy"]}
            self.store.json_write(self.metadata, meta)
            self.store.write(self.urls, text)
            archive.pop(identifier)
            self.store.json_write(self.deleted, archive)
            return room_id(url)

    def save_settings(self, body):
        with self.lock:
            self.check_revision(body.get("revision"))
            values = body.get("values")
            if not isinstance(values, dict) or set(values) != set(self.settings()["values"]):
                raise AppError("录制设置字段不完整，请刷新页面。")
            for key in ("segment_enabled", "delete_original", "h265"):
                if type(values[key]) is not bool:
                    raise AppError("开关设置不正确。")
            for key, low, high in (("segment_seconds", 60, 86400), ("poll_seconds", 30, 3600), ("qsv_quality", 1, 51)):
                if type(values[key]) is not int or not low <= values[key] <= high:
                    raise AppError({"segment_seconds": "分段时长应为 60–86400 秒。",
                                    "poll_seconds": "检测间隔应为 30–3600 秒。",
                                    "qsv_quality": "H.265 质量参数应为 1–51。"}[key])
            if values["record_format"] not in ("TS", "MKV", "MP4") or values["quality"] not in QUALITIES or values["post_format"] not in ("mp4", "keep"):
                raise AppError("请选择支持的录制格式、保存格式和画质。")
            record = {k: v for k, v in values.items() if k not in ("h265", "qsv_quality")}
            self.store.json_write(self.pending, {"values": record, "requested_at": time.time()})
            h = self.store.json_read(self.h265, {})
            if values["post_format"] == "mp4":
                h["enabled"] = values["h265"]
            h["qsv_quality"] = values["qsv_quality"]
            h["encoder"] = "hevc_qsv"
            self.store.json_write(self.h265, h)

    def apply_pending(self):
        with self.lock:
            pending = self.store.json_read(self.pending, {})
            if not pending:
                return False
            values = pending["values"]
            ini = {}
            for key, value in values.items():
                if key in ("segment_enabled", "delete_original"):
                    value = "是" if value else "否"
                elif key == "post_format":
                    value = "是" if value == "mp4" else "否"
                ini[RECORD_KEYS[key]] = value
            ini["mp4格式重新编码为h264"] = "否"
            self._change_ini(ini)
            self.store.remove(self.pending)
            return True

    def save_proxy(self, body):
        with self.lock:
            self.check_revision(body.get("revision"))
            server = body.get("server", "")
            if not isinstance(server, str) or len(server) > 512 or re.search(r"[\s\x00-\x1f]", server):
                raise AppError("代理服务器地址格式不正确。")
            try:
                u = urlsplit(server)
                port = u.port
            except ValueError:
                raise AppError("代理端口不正确。")
            if u.scheme not in ("http", "https") or not u.hostname or not port or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
                raise AppError("请输入代理协议、主机和端口，例如 http://192.0.2.2:7893。账号和密码请填写在各自的输入框。")
            previous = urlsplit(self.proxy_url() or "http://unused:1")
            username = body.get("username", "")
            password = body.get("password", "")
            clear = body.get("clear_auth", False)
            if type(clear) is not bool or any(not isinstance(x, str) or len(x) > 512 or re.search(r"[\r\n\x00]", x) for x in (username, password)):
                raise AppError("代理账号或密码格式不正确。")
            if clear:
                username = password = ""
            else:
                username = username or unquote(previous.username or "")
                password = password or unquote(previous.password or "")
            host = "[" + u.hostname + "]" if ":" in u.hostname else u.hostname
            auth = (quote(username, safe="") + ":" + quote(password, safe="") + "@") if username else ""
            address = urlunsplit((u.scheme, auth + host + ":" + str(port), "", "", ""))
            self._change_ini({"代理地址": address, "是否使用代理ip(是/否)": "是"})
