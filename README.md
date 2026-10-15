# DouyinLiveRecorder-Web

基于 [ihmily/DouyinLiveRecorder v4.0.7](https://github.com/ihmily/DouyinLiveRecorder/releases/tag/v4.0.7) 的独立直播录制项目，增加网页管理、YouTube 音视频录制修复和 H.265 Intel QSV 自动转码。

保留上游多平台录制源码；本仓库独立维护、独立构建 Docker 镜像。上游源码版本、提交和本项目版本见 [UPSTREAM.json](UPSTREAM.json)。原作者 Hmily 的版权声明与 MIT 许可保留在 [LICENSE](LICENSE)。

## 功能

- **网页直播任务管理**：添加、编辑链接和备注、选择画质、暂停、恢复、删除；显示检测、开播、录制、异常状态。
- **逐直播间代理**：直连或共享 HTTP 代理，支持账号密码；保存过的凭据不回显，留空保留。
- **YouTube 更新**：频道主页与 `/streams` 自动规范为 `/live`；使用 yt-dlp 解析直播，适配 HLS 分离的视频和音频；两条输入使用同一代理，显式映射音视频轨道。
- **代理兼容**：适配某些 OpenClash/Mihomo 代理的 Basic 认证响应。临时转接仅绑定容器内部回环端口，仅放行 YouTube / Googlevideo HTTPS 目标，录制结束关闭。
- **录制选项**：分 P 开关、每段秒数、TS / MKV / MP4、默认画质、检测间隔、最终保存 MP4 或保留录制格式、转换成功后是否删除原件。
- **自动转码**：录制结束自动入队，Intel QSV 编码 H.265，MP4 使用 hvc1 标签和 faststart；已是 HEVC 的视频可直接封装。
- **转码队列**：单任务顺序执行、SQLite 持久化、重启恢复、真实进度与速度、体积变化、错误原因、失败重试、逐条或批量删除记录。等待任务可置顶、上移、下移、置底，也可按入队时间、文件大小或文件名调整实际转码顺序。支持暂停并保留当前进度、继续转码，以及停止当前任务并暂停后续队列；录制不受影响。
- **数据保护**：转换验证编码、分辨率、音轨数量和时长后再发布；源文件变化、同名输出、核显不可用时保留可用录像，不自动改用 CPU 编码。
- **HTTPS 管理**：单管理员密码、服务器端会话、来源和 CSRF 校验、登录限速。网页容器不挂载 Docker socket，也不接触核显设备。

> 默认配置：每 3600 秒分段、TS 录制、结束后保存 H.265 MP4、成功验证后删除对应 TS。整次直播录制结束或从网页暂停后，分段文件统一进入转码队列；分段期间不会立即转码仍在录制的文件。

## 桌面版

新增 Windows x64 与 macOS Apple Silicon 桌面端，沿用 HTML 控制台，提供本机目录选择、编码器检测、托盘后台录制和睡眠策略。构建与下载方式见 [桌面版说明](docs/DESKTOP.md)。桌面源码位于 desktop/，NAS Docker 部署继续使用下面的流程。

## 环境要求

- Linux x86_64 / amd64，支持 HEVC 编码的 Intel 核显，Docker Engine 与 Docker Compose v2。
- 宿主机能访问 `/dev/dri/renderD128`；极空间请启用 Docker 的“调用核心显卡”或进行等效设备映射。
- 默认配置面向 Intel QSV。AMD、Rockchip、NVIDIA、ARM 和非 Linux 桌面端不在本版默认部署支持范围。
- 下载镜像及 Python / Debian 依赖需要网络连接。海外直播按需要配置可用的 HTTP 代理。

镜像从官方 Python / Node 镜像构建，FFmpeg、Intel 媒体驱动和 VPL 运行库来自 Debian 软件仓库。驱动参考：[Debian Intel media driver](https://packages.debian.org/trixie/intel-media-va-driver-non-free)、[Intel VPL runtime](https://packages.debian.org/trixie/libmfx-gen1.2)；编码参数参考 [FFmpeg QSV 文档](https://ffmpeg.org/ffmpeg-codecs.html#QSV-Encoders)。

## 首次部署

~~~sh
git clone https://github.com/MalcolmLu/DouyinLiveRecorder-Web.git
cd DouyinLiveRecorder-Web
cp .env.example .env
~~~

编辑 `.env`，将 `NAS_HOST` 改成 NAS 实际的局域网 IPv4 地址，并按需要修改端口、时区和宿主机核显设备。默认 `127.0.0.1` 仅供本机访问。端口默认 18888；请避免与现有实例冲突。

~~~sh
docker compose build
# 将下面的 192.0.2.10 替换为 .env 中相同的 NAS_HOST 地址。
docker compose run --rm --no-deps web python /app/scripts/setup.py certificate --host 192.0.2.10
docker compose run --rm --no-deps web python /app/gui/manage.py set-password
docker compose up -d
~~~

密码在终端输入两次，至少 12 个字符，不会显示，也不会放进环境变量或仓库。使用浏览器打开 `https://你的NAS地址:18888`，由管理员本人核对并信任自签名证书，然后登录。登录表单显示固定账号 `admin`，使用上述管理密码；标准表单提交支持浏览器密码管理器。Chrome 在证书报错时会禁用密码保存与自动填充，因此需要有效、受信任的 HTTPS 证书，首次登录后再选择保存密码。

在极空间 Compose 中部署时，项目目录内需包含本仓库完整文件，以便构建 Dockerfile；首次初始化、证书和密码设置可使用容器终端运行相同命令。详细目录迁移与故障处理见 [部署说明](docs/DEPLOYMENT.md)。

## 录制与代理设置

在“直播任务”添加链接，选择画质和“使用共享代理”。先在“代理设置”填写 HTTP 代理地址；如使用 iStoreOS / OpenClash，填写其局域网地址和 HTTP 代理端口，并按实际填写认证信息。没有勾选代理的任务直连。

YouTube 支持频道主页、`/live`、`/streams`、`watch?v=...` 和短链接。频道必须正在公开直播，直播源需要提供可用的 H.264 HLS 音视频；地区限制、登录验证、会员或平台接口变化可能导致解析失败，页面会显示原因。

在“录制设置”调整分段秒数、录制格式和最终格式。录制参数等待所有当前录制结束后应用；编码参数从下一个转码任务开始生效。QSV 质量值越小通常质量越高、体积越大，默认 ICQ 24；实际压缩率由内容决定。

在“转码队列”顶部选择等待任务的排序方式并点击“应用顺序”，或用任务下方的置顶、上移、下移、置底按钮调整优先顺序。排序作用于全部等待任务，正在处理的任务继续完成；新任务追加在队尾。顺序保存在 SQLite 中，页面刷新和服务重启后仍然有效。

“暂停转码”会暂停当前编码或验证进程，保留进度；“继续转码”接着执行。“停止转码”会中止当前未完成任务、清理该任务的临时输出并保留原录像，同时停止领取后续任务。已停止任务可点“重新排队”，从头转码；点击“继续转码”只继续其他等待任务。已经验证并进入最终保存步骤的任务会先完成安全收尾。暂停和停止仅控制转码，直播录制和新录像入队继续进行。停止操作由实际转码进程确认后才会显示“已停止”；若其他页面已修改队列或当前任务已切换，过期操作会被拒绝并刷新状态。

已完成、失败或已停止任务右侧可“删除记录”，也可以勾选多条并点击“删除所选记录”；全选范围为当前页可删除的任务，切换页码、筛选或搜索会清空选择。删除记录保留视频与去重指纹，排队中及处理中不能删除。若所选任务已重新排队或开始处理，整批删除会拒绝并刷新状态。网页不提供“已删除记录”列表。删除直播间也不会删除既有录像。点击直播任务中的直播间名称会在新标签页打开直播页面，“录像文件夹”按钮用于浏览和播放已有录像。

## 数据目录

所有私人运行数据放在被 Git 与 Docker 构建忽略的 `data/` 下：

| 目录 / 文件 | 内容 |
|---|---|
| `data/config/config.ini`、`URL_config.ini` | 实际录制配置、Cookie、代理及直播间列表 |
| `data/config/gui/rooms.json` | 备注及逐直播间代理选择 |
| `data/config/gui/state/` | 网页密码散列、会话、HTTPS 证书、采集器状态 |
| `data/config/h265/` | 编码设置、转码队列、进度与转码日志 |
| `data/downloads/` | 原录像和最终视频 |
| `data/logs/`、`data/backup_config/` | 采集日志与配置备份 |

源码 `gui/` 和 `h265/` 随镜像部署，运行数据与源码分开；升级源码并重新构建不会自动替换已有配置。请备份整个 `data/`，其中包含代理、Cookie 和登录状态等私人信息，勿上传到公开仓库。

## 测试

~~~sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r gui/requirements.txt -r gui/requirements-youtube.txt
PYTHONPATH=gui python -m unittest discover -s gui -p 'test_*.py' -v
PYTHONPATH=gui python -m unittest discover -s tests -v
# 需要 FFmpeg；使用隔离的临时媒体和软件编码验证队列。
python h265/selftest.py
# 使用隔离的测试视频验证实际排序、暂停、停止及原录像保留。
python h265/queue_selftest.py
# 仅在具有核显映射的 Linux 机器上验证实际 QSV。
python h265/selftest.py --hardware
~~~

GitHub Actions 检查接口、初始化、源码集成、JavaScript 语法，并构建 Linux 镜像运行 FFmpeg 转码及 HTTPS 冒烟测试。公共 CI 没有核显，硬件测试需在支持 QSV 的宿主机执行。

合并前的 NAS 实测已确认 YouTube 1080p 音视频经认证 HTTP 代理录制后可自动转成 HEVC + AAC MP4，并可完整解码；公开仓库中的新 Docker 打包需以本仓库 CI 构建结果为准，不能把 NAS 旧镜像实测等同于所有机器兼容。

## 许可与致谢

- 上游：[ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)，基础版本 v4.0.7，MIT License。
- 本项目新增网页、录制适配和转码代码使用 MIT License，详见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
- yt-dlp、Flask、Gunicorn、FFmpeg、Node.js 和 Intel 驱动遵循各自许可证；依赖在构建时安装，未把本次 NAS 的二进制运行库或第三方 vendor 目录提交到仓库。
- 仅录制你有权保存的内容，并遵守对应平台的使用规则。
