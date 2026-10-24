---
name: video-subtitle-extractor
description: 从视频文件（MP4/AVI/MKV/MOV等）中提取字幕。通过 FunASR 语音识别模型（Paraformer/SenseVoice）将视频音频转为 SRT 字幕文件。适用场景：批量提取视频字幕、生成双语字幕源、后续进行字幕清洗或翻译。当用户要求从视频提取字幕、音频转字幕、视频转文字时触发。
---

# Video Subtitle Extractor

从视频文件提取语音识别字幕，输出 SRT 格式字幕文件。

## 支持格式

**输入**：MP4, AVI, MKV, FLV, MOV, WebM, TS, MPEG 等常见视频格式
**输出**：SRT 字幕文件

## 可用 ASR 模型

| 模型 | 语言 | 特点 |
|------|------|------|
| `paraformer`（默认） | 中文 | 工业级高精度模型，支持热词 |
| `fun-asr-nano` | 31种语言 | 端到端 LLM-based ASR，更高准确率 |
| `sensevoice` | 中文优先+多语言 | 支持情感识别和音频事件检测 |

## 使用方法

### 方式一：命令行（推荐）

```powershell
# 默认模型（中文 Paraformer）
python funclip/videoclipper.py --stage 1 --file <视频路径> --output_dir <输出目录>

# 指定英文识别
python funclip/videoclipper.py --stage 1 --file <视频路径> --output_dir <输出目录> -l en

# 指定 SenseVoice 模型
python funclip/launch.py -m sensevoice --file <视频路径> --output_dir <输出目录>
```

### 方式二：Gradio Web 界面

```powershell
python funclip/launch.py
# 访问 http://localhost:7860
# 上传视频 → 点击"识别" → 下载 SRT 字幕
```

## 输出文件

识别完成后，输出目录包含：
- `total.srt` — 完整视频的 SRT 字幕
- `state.json` — 识别中间状态（用于后续剪辑）

## 脚本方式提取字幕

若只想提取字幕而不剪辑，使用 `scripts/extract_subtitle.py`：

```powershell
python skill/video-subtitle-extractor/scripts/extract_subtitle.py `
  --input "D:\path\to\video.mp4" `
  --output "D:\path\to\output\video.srt" `
  --model paraformer
```

## 依赖说明

- FunASR 模型首次运行时会自动下载
- 识别过程需要音频解码，Windows 用户确保 ffmpeg 在 PATH 中
- 显存要求：Paraformer ~2GB，SenseVoice ~3GB

## 注意事项

- 视频必须有音轨，无音轨视频会报错退出
- 长视频识别时间与视频时长成正比
- 如识别结果时间戳偏差大，可尝试添加热词增强识别精度
