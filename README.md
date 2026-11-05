# VideoCut — 本地视频自动剪辑工具

FunASR 字幕识别 + 时间戳剪辑 + 短视频生产管线 + Web 工作台。纯本地运行，无需联网上传视频。

## 环境要求

- Python 3.10 或 3.12（**3.13 不支持**：`numpy==1.26.4` 无 3.13 的 wheel）
- ffmpeg（加入 PATH，命令行执行 `ffmpeg -version` 可看到版本）；`ffprobe`
  建议与 ffmpeg 同目录（标准发行版自带）。只有 ffmpeg 没有 ffprobe 时
  （如部分 imageio-ffmpeg 部署），程序会自动回退用 ffmpeg 同目录的
  ffprobe；两者都没有时相关时长/声道探测降级跳过，不影响其余功能。
- 内存 ≥ 8GB；推荐 16GB。识别（FunASR）进程内加载模型峰值可达 ~6GB：
  8GB 机器建议设置环境变量 `SLIRN_ASR_SUBPROCESS=1`，识别改在独立
  python 子进程里跑（见下节），跑完内存全部归还，避免把系统内存顶满。

## 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip.exe install -r requirements.txt
# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

## FunASR 与模型的获取（不随仓库分发，按需下载）

本工具的语音识别基于阿里 **FunASR**，**不在本仓库内、也不随本仓库分发**，请从官方指定渠道获取：

1. **FunASR 库**：`requirements.txt` 已声明 `funasr`，`pip install` 时自动从
   [PyPI](https://pypi.org/project/funasr/) 安装；也可从官方源码仓
   <https://github.com/modelscope/FunASR> 获取。
2. **ASR 模型**：首次运行识别时自动从 [ModelScope（魔搭社区）](https://www.modelscope.cn)
   下载到本机缓存目录，模型清单：
   - `iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`（中文识别）
   - `iic/speech_paraformer_asr-en-16k-vocab4199-pytorch`（英文识别）
   - `damo/speech_fsmn_vad_zh-cn-16k-common-pytorch`（VAD 切分）
   - `damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch`（标点）
   - `damo/speech_campplus_sv_zh-cn-16k-common`（说话人，可选）

   首次下载约数 GB，请保证磁盘与网络；之后离线可用。

## 字幕字体（自动下载，不入库）

字幕渲染使用的 STHeitiMedium.ttc（约 54MB）**不随仓库分发**：首次启动时自动从上游
[FunClip 官方仓库](https://github.com/alibaba-damo-academy/FunClip/tree/main/font)下载到
`font/` 目录，之后离线可用；下载失败不影响启动（渲染时自动重试，彻底失败则回退系统字体）。

网络不便时可手动获取：从上述地址下载 `STHeitiMedium.ttc`，放到工程 `font/` 目录下即可。

## 启动

```bash
# Windows 双击 start.bat，或：
.venv/Scripts/python.exe funclip/launch.py
# 工作台模式（推荐，端口 7861）：
.venv/Scripts/python.exe funclip/launch.py --home slirn
```

浏览器打开 <http://127.0.0.1:7861>。

**默认管理员**：`admin` / `admin123!@#`（首次启动自动创建）。若仍在使用默认密码，
登录页会明确警示；新密码要求至少 8 位。未登录时只能访问登录页，其余页面与接口一律拦截。

## ASR 子进程隔离（小内存机器可选开关）

环境变量 `SLIRN_ASR_SUBPROCESS=1` 时，每次识别（长视频字幕生成、短视频
字幕提取都生效）改在独立 python 子进程里跑同一条链路：

- 主进程不再加载 torch/funasr/librosa（省 ~1-2GB 常驻内存）；
- 模型加载 + 推理的内存峰值被限制在子进程内，识别结束进程退出、内存
  全部归还 —— 8GB 机器上不会再把系统 commit 顶满、掐断并发 HTTP 连接；
- 代价：每次识别多付一次模型加载（模型已在本地缓存时约 20-40s）；
- 超时上限 6 小时，失败时错误信息回传前端（与进程内模式一致）。

默认关闭（不设即走原进程内路径，行为不变）。Windows 服务化部署可用
`setx SLIRN_ASR_SUBPROCESS 1` 持久化，重启服务后生效。

## 安全部署

- **默认密码**：首次启动用默认管理员登录后，登录页会一直显示「仍在使用默认密码」警示，
  请立刻在「用户管理」中修改（至少 8 位）；新建用户同样要求至少 8 位。
- **本机使用（推荐）**：默认只监听 `127.0.0.1`，浏览器访问 <http://127.0.0.1:7861>；
  需要局域网访问时才用 `--listen` 参数（此时会监听 `0.0.0.0`，见下条）。
- **`SLIRN_AUTH_BYPASS=1`（免登录调试开关）**：仅在客户端地址为本机回环
  （`127.0.0.1` / `::1` / `localhost`）时生效；从局域网或其他机器访问不会绕过登录。
  即使本机调试也请显式设置该环境变量才启用，用完即关。
- **`SLIRN_MIDDLEWARE_DEBUG=1`**：把认证中间件的拦截详情写到 `.temp/middleware_debug.log`
  （`/login` 之外的重定向等），排查权限问题时临时打开，平时不要开。
- **不要把 7861 端口直接暴露到公网**；确需局域网共享（`--listen`）时，请先改掉默认密码，
  并了解任务 / 视频文件按用户隔离（管理员可见全部）。

## 主要功能

- **长视频工作台**：字幕生成 → 字幕修订 → 切分修剪 → 粗剪合成 → 优化字幕 → 精剪合成，
  六个阶段可集中配置后**一键自动执行**（顶部「⚙ 流程配置」面板）
- **短视频管线**：视频选择 → 字幕提取 → 内容提取 → 内容优化 → 粗剪合成 → 精剪混编，
  同样支持「⚙ 流程配置」自动执行（提示词块 / 生成数量 / 时长上下限 / 起止阶段）
- **执行日志**：每个主要操作都留痕（时间、耗时、成功失败、错误信息），
  侧栏「📜 执行日志」可按类型 / 状态 / 模式过滤查看
- **单账号单会话**：同一账号在新终端登录会踢掉旧会话

## 目录

| 目录 | 内容 |
|---|---|
| `funclip/` | 上游 FunClip（识别 / 剪辑核心，fork） |
| `slirn_home/` | Web 工作台与短视频管线后端 |
| `tasklib/` | 任务数据模型 |
| `skill/` | 字幕提取 / 清洗 / 剪辑等脚本 |

## 许可

MIT（见 `LICENSE`）。
