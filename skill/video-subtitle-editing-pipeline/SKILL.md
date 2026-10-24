---
name: video-subtitle-editing-pipeline
description: 将视频字幕处理全流程串起来。输入原始视频，先提取 SRT 字幕，再清洗出有效字幕与有效时间戳，随后按时间戳剪辑视频，并生成剪辑后可直接导入剪映的连续时间轴字幕文件。适用于从视频直接生成剪辑后视频和剪辑后字幕的场景。
---

# Video Subtitle Editing Pipeline

这个技能把现有三个技能串成一条完整链路：

1. 从视频提取原始字幕。
2. 从原始字幕生成中间文件、最终清洗文件、删除比对文件、重复问题文件。
3. 根据最终清洗文件中的有效时间戳剪辑视频。
4. 根据最终清洗文件生成剪辑后连续时间轴字幕。

## 输入

- 原始视频文件，例如 `mp4`、`mov`、`mkv`

## 最终输出

- 剪辑后视频：`<视频名>_cut.mp4`
- 剪辑后连续字幕：`<视频名>.continuous.srt`

## 同时输出的中间文件

- 原始识别字幕：`<视频名>.raw.srt`
- 中间文本：`<视频名>.intermediate.txt`
- 最终清洗文本：`<视频名>.cleaned.txt`
- 删除比对文件：`<视频名>.removed.txt`
- 审核对照文件：`<视频名>.review.md`
- 重复问题文件：`<视频名>.repeat-only.txt`
- 连续字幕重复问题文件：`<视频名>.continuous.repeat.txt`

## 使用方式

```powershell
python slirn\skill\video-subtitle-editing-pipeline\scripts\run_pipeline.py `
  --input "D:\path\to\video.mp4" `
  --output-dir "D:\path\to\output" `
  --model paraformer `
  --lang zh `
  --repeat-marker 〿 `
  --max-chars-per-caption 20
```

## 处理顺序

### 第一步：提取原始字幕

调用：

- `skill/video-subtitle-extractor/scripts/extract_subtitle.py`

输出：

- `<视频名>.raw.srt`

### 第二步：清洗字幕并生成有效时间戳

调用：

- `skill/long-video-subtitle-cleaner/scripts/srt_to_intermediate.py`
- `skill/long-video-subtitle-cleaner/scripts/clean_intermediate_subtitles.py`

输出：

- `<视频名>.intermediate.txt`
- `<视频名>.cleaned.txt`
- `<视频名>.removed.txt`
- `<视频名>.review.md`
- `<视频名>.repeat-only.txt`

### 第三步：按有效时间戳剪辑视频

调用：

- `skill/video-timestamp-cutter/scripts/cut_by_srt.py`

输入：

- 原始视频
- `<视频名>.cleaned.txt`

输出：

- `<视频名>_cut.mp4`

### 第四步：生成剪辑后连续字幕

调用：

- `skill/long-video-subtitle-cleaner/scripts/export_continuous_srt.py`

输出：

- `<视频名>.continuous.srt`
- `<视频名>.continuous.repeat.txt`

## 说明

- 剪辑后字幕的时间轴是连续的，适合导入剪映。
- 连续字幕里会写入重复问题标记符，便于后续人工定位。
- 连续字幕超过 `20` 字时，会优先按标点和停顿切分。
