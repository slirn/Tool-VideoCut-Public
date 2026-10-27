---
name: video-timestamp-cutter
description: 根据字幕时间戳从视频中提取片段。输入视频文件和 SRT 字幕文件，输出按时间戳切分的视频片段。适用于：本地运行避免内存限制、从已有字幕批量提取视频片段、离线环境使用。不依赖 ASR 识别，纯本地视频剪切。
---

# Video Timestamp Cutter

根据 SRT 字幕中的时间戳信息，从视频中提取指定片段并拼接输出。

## 输入输出

| 输入 | 说明 |
|------|------|
| 视频文件 | MP4/AVI/MKV/MOV 等常见格式 |
| SRT 字幕文件 | 包含时间戳的字幕文件 |

| 输出 | 说明 |
|------|------|
| 剪切后视频 | MP4 格式，H.264 编码 |

## 使用方式

### 命令行

```powershell
# 基本用法 — 按字幕所有时间戳剪切
python skill/video-timestamp-cutter/scripts/cut_by_srt.py `
  --input "video.mp4" `
  --srt "video.srt" `
  --output "video_cut.mp4"

# 指定要提取的字幕序号（逗号分隔）
python skill/video-timestamp-cutter/scripts/cut_by_srt.py `
  --input "video.mp4" `
  --srt "video.srt" `
  --output "video_cut.mp4" `
  --indices "1,3,5"

# 指定时间范围（秒）
python skill/video-timestamp-cutter/scripts/cut_by_srt.py `
  --input "video.mp4" `
  --srt "video.srt" `
  --output "video_cut.mp4" `
  --start 10.5 `
  --end 65.3
```

### Python API

skill 目录不是 Python 包（路径含连字符，无法直接 import）。
需要编程调用时，把脚本目录加入 `sys.path` 再导入：

```python
import sys; sys.path.insert(0, "skill/video-timestamp-cutter/scripts")
from cut_by_srt import cut_video_by_srt

# 按 SRT 所有条目剪切
cut_video_by_srt("video.mp4", "video.srt", "output.mp4")

# 按指定序号剪切
cut_video_by_srt("video.mp4", "video.srt", "output.mp4", indices=[1, 3, 5])

# 按时间范围剪切
cut_video_by_srt("video.mp4", "video.srt", "output.mp4", start=10.5, end=65.3)
```

## 时间戳格式

支持以下时间戳格式：

**SRT 格式（标准）：**
```srt
1
00:00:10,500 --> 00:00:15,200
这是第一段字幕文本

2
00:00:20,100 --> 00:00:25,800
这是第二段字幕文本
```

**纯文本时间戳格式（兼容清洗技能输出）：**
```text
1. [00:01:11,830 - 00:01:18,940] 然后呃我们回到 IP 这个东西
```

**JSON 格式：**
```json
[{"start": 10.5, "end": 15.2, "text": "字幕文本"}, ...]
```

## 处理逻辑

1. 解析输入时间戳文件（SRT/JSON/文本）
2. 对每个时间戳区间调用 `video.subclip(start, end)`
3. 按原始顺序拼接所有片段
4. 编码输出 MP4 文件

## 编码参数

- 视频编码：H.264 (libx264)
- 音频编码：AAC
- 默认码率：自动（CRF 23）
- 输出格式：MP4

## 参考资料

- 时间戳格式详情：见 `references/timestamp-formats.md`
- 编码参数调整：见 `references/encoding-params.md`
