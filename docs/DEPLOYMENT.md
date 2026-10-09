# 部署、迁移与维护

## 首次运行

根目录 README 提供完整命令。先构建镜像，再生成对应 NAS IPv4 地址的 HTTPS 证书、在终端设置管理密码，最后启动两个服务。没有预置密码，也不提供免登录设置密码接口。

`docker compose run --rm --no-deps web ...` 不发布网页端口，也不需要核显映射，可用于设置密码和证书。设备只映射给 recorder 服务；web 服务的根文件系统只读，录像目录也只读。

## 极空间

将本仓库完整目录放入选定存储位置，在 Docker 的 Compose 中使用 `docker-compose.yaml`。NAS 路径由项目目录和 `./data/` 相对路径决定。宿主机 render 设备默认为 `/dev/dri/renderD128`，可通过 `.env` 的 `GPU_DEVICE` 修改。

不要将现有录制器和新项目同时映射到同一配置、录像或队列目录。迁移前先暂停监控，等待转码完成，然后停止旧实例并备份数据。

## 从之前的 NAS 版本迁移

1. 停止旧实例，完整备份旧 config、downloads、logs、backup_config。
2. 将上述目录分别复制到新项目的 `data/` 中，保持目录结构。
3. 保留旧 `config/gui/state`、`config/gui/rooms.json` 和 `config/h265/queue.sqlite3` 及其 WAL 文件。源码不再从运行配置目录加载，旧补丁文件可以暂存以便回退。
4. 设置相同的 NAS_HOST、WEB_PORT；若沿用相同的访问地址，可以保留已有 HTTPS 证书和网页密码。
5. 构建新镜像，运行 `docker compose up -d`，检查心跳、任务、队列和输出目录。
6. 只有确认新版本工作正常后，再自行清理备份。

已有配置不会被初始化脚本替换；不要把新 defaults 文件覆盖到旧配置上。端口或地址变更时同步更新 `.env` 和证书。证书脚本保留已有证书；要换地址，请先停 web 并备份 `data/config/gui/state/tls/`，再移走旧证书对后重新生成。

## 检查状态

~~~sh
docker compose ps
docker compose logs --tail 100 web
docker compose logs --tail 100 recorder
docker compose exec recorder python /app/h265/postprocess.py --status
docker compose exec recorder vainfo --display drm --device /dev/dri/renderD128
~~~

如果核显不可用，任务会失败并保留源录像。检查宿主机设备、容器映射、驱动、HEVC 编码支持及设备访问权限后，通过网页逐条重试。不要修改权限使网页服务拥有设备或 Docker socket。

默认转码设置在 `data/config/h265/settings.json`：`hevc_qsv`、ICQ 24、fast preset。UI 提供质量和启用开关，本版默认使用 Intel QSV；软件编码用于开发测试，不会作为硬件失败后的自动替代。

## 升级与回退

先备份 data 和当前源码版本，暂停监控并等待文件完成处理，再更新源码和执行 `docker compose build && docker compose up -d`。运行数据不参与构建。需要回退时使用之前的源码重新构建，保留 data；若未来数据库发生不兼容迁移，按对应版本说明恢复备份。

不要把 NAS 管理网页或 18888 端口直接暴露到公网。若需远程访问，使用已有的可信 VPN 或由管理员配置的安全入口。
