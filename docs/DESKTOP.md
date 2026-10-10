# Windows 与 macOS 桌面版

桌面版使用 Electron 作为窗口和托盘，直接运行随软件提供的 Python 录制后台，沿用网页端 HTML、直播任务、逐直播间代理与转码队列。无需 Docker 或另行安装 Python。

第一版目标平台：Windows 10/11 x64、macOS 15 及以上 Apple Silicon（arm64）。

## 使用方式

启动软件后会打开控制台，后台仅监听 127.0.0.1 的随机端口。应用主进程创建本地会话，使用隔离的浏览器会话传递 HttpOnly Cookie；网页继续校验会话、CSRF、Origin 和 Host，局域网其他设备无法连接。

- “直播任务”“转码队列”“录制设置”“代理设置”沿用现有界面。
- 点击直播间名称或“录像文件夹”，可浏览该直播间的录像和子目录，并选择文件播放或下载。播放能力取决于系统与内嵌浏览器支持的编码格式。
- 新录制的每个分 P 文件以该段实际开始时间命名；转码后保留同一文件名，只更改扩展名。更新软件不会自动重命名已有录像。
- “桌面设置”可选择录像目录、FFmpeg 路径、编码器、VideoToolbox 码率、CPU 线程数，以及启动、托盘和睡眠策略。
- 默认关闭窗口后留在 Windows 托盘或 macOS 菜单栏继续录制；点击托盘图标可恢复窗口。Mac 菜单栏入口显示“录播”文字，也可点击或右键 Dock 图标恢复窗口。
- 使用托盘菜单“退出并保存录像”完全退出。正在录制的文件先结束并保存；队列与可用原件保留，下次启动继续处理。
- 修改录像目录或工具路径前，先停止采集器并等转码队列处理完成。已有录像不会搬移，旧目录仍保留在任务路径许可范围内。
- 暂不提供系统开机登录自启动、自动软件升级或云端账号。

## 编码器

macOS 自动检测 Apple VideoToolbox；Windows 自动测试 Intel QSV、NVIDIA NVENC、AMD AMF。初次自动检测都不可用时，选择 CPU libx265。选择固定硬件编码器后，任务失败会保留源录像，不自动改用软件编码。

“测试当前编码器”执行少量测试帧，检测当前硬件和驱动是否真的可用。VideoToolbox 以 kbps 设置码率；QSV / NVENC / AMF 的质量数值及 CPU CRF 在“录制设置”调整。

软件暂停或退出会按原策略把已完成文件加入队列。仅验证通过的成品才会触发删除原件。系统手动休眠、合盖、断电或强制终止进程仍可能中断录像。

## 数据

Windows：Electron 的用户数据目录（通常为 %APPDATA%/douyin-live-recorder-desktop）。
macOS：~/Library/Application Support/douyin-live-recorder-desktop。

实际目录会显示在“桌面设置”。其中 config 保存录制设置、代理、Cookie、队列和状态；logs 保存后台日志；录像默认存放于 recordings，可改为其他绝对路径。

不要把用户数据目录提交到 GitHub。卸载程序保留用户数据。

## 开发与构建

在对应操作系统上执行：

~~~sh
python -m venv .venv
# 激活环境后：
python -m pip install -r desktop/requirements.txt
cd desktop
npm ci
npm run prepare-tools
cd ..
python desktop/build_backend.py
cd desktop
npm run dist -- --publish never
~~~

开发运行可设置 DLR_PYTHON 指向上述环境的 Python，再运行 npm start。

GitHub Actions 的 Desktop installers 工作流在 Windows 和 macOS 原生构建，并运行冻结后台启动、登录保护、停止采集、直播间增删、录像目录与 HTTP 分段读取、实际分 P 时间命名和真实软件 HEVC 转码检查。输出 Windows NSIS .exe、macOS .dmg 与 .zip，保存在对应工作流的 Artifacts 中。

安装包当前未使用付费的 Windows 发行签名或 Apple Developer ID 公证。它们是供测试的构建，操作系统可能要求由用户本人确认打开；应用不会绕过系统安全提示。

## 第三方工具

Node.js 从官方发行站点下载并核对 SHASUMS256。macOS FFmpeg 来自校验 SHA256 的 Homebrew bottles，依赖动态库重定位到应用目录并保留许可证与精确源码元数据；Windows FFmpeg 使用 npm 的 ffmpeg-static / ffprobe-static 包。构建拒绝带 --enable-nonfree 的 FFmpeg。工具许可证和来源记录随应用 tools 目录保存。
