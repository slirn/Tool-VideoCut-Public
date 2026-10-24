# FunClip ASR 模型参考

## 可用模型

| 模型 | 模型 ID | 语言 | 显存 | 特点 |
|------|---------|------|------|------|
| Paraformer（默认） | `iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` | 中文 | ~2GB | 工业级高精度，13M+下载 |
| Paraformer-English | `iic/speech_paraformer_asr-en-16k-vocab4199-pytorch` | 英文 | ~2GB | 英文专用 |
| Fun-ASR-Nano | `FunAudioLLM/Fun-ASR-Nano-2512` | 31种语言 | ~1GB | 端到端LLM-based，流式 |
| SenseVoice | `iic/SenseVoiceSmall` | 中+多语言 | ~3GB | 情感识别+音频事件检测 |

## 启动命令

```shell
# 默认 Paraformer（中文）
python funclip/launch.py

# Fun-ASR-Nano（多语言）
python funclip/launch.py -m fun-asr-nano

# SenseVoice（情感+事件检测）
python funclip/launch.py -m sensevoice

# 英文识别
python funclip/launch.py -l en
```

## 命令行用法

```shell
# 识别视频，输出 ./output/total.srt
python funclip/videoclipper.py --stage 1 \
  --file examples/video.mp4 \
  --output_dir ./output

# 英文识别
python funclip/videoclipper.py --stage 1 \
  --file examples/video.mp4 \
  --output_dir ./output \
  --lang en
```

## VAD / PUNC / SPK 子模型

所有模型共享相同的 VAD、PUNC、SPK 子模型：
- VAD: `damo/speech_fsmn_vad_zh-cn-16k-common-pytorch`
- PUNC: `damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch`
- SPK: `damo/speech_campplus_sv_zh-cn-16k-common`

## 热词配置

热词可提升特定词汇识别准确率（仅支持中文）：

```python
hotwords = "习近平 乡村振兴"  # 空格分隔多个热词
clipper.video_recog(video_path, hotwords=hotwords)
```

在 Gradio 界面中通过"热词"输入框配置。
