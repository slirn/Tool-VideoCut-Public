# 系统默认 BGM（Lo-Fi 配乐）

REQ-20260921-NNN：把 5 个 Lo-Fi mp3 入库到仓库内，方便 clone 后立即可用。
之前硬编码 `D:\tmp\tttttt\` 路径，clone 后默认 BGM 全部不可用（`available=false`）。

## 文件清单（5 个，~20 MB）

| id | 显示名 | 文件名 | 大小 |
|---|---|---|---|
| lofi_beat_1 | Pretty John — Lo-Fi Beat | prettyjohn1-lo-fi-beat-580021.mp3 | 1.7 MB |
| lofi_love_loop | Sonican — Sentimental Jazzy Love | sonican-lo-fi-music-loop-sentimental-jazzy-love-473154.mp3 | 3.2 MB |
| the_mountain | The Mountain — Lo-Fi Beat | the_mountain-lo-fi-beat-567432.mp3 | 5.5 MB |
| zephira_lofi | Zephira Music — Lo-Fi | zephiramusic-lo-fi-581502.mp3 | 4.6 MB |
| zephira_relax | Zephira Music — Relaxing Lo-Fi | zephiramusic-relaxing-lo-fi-587547.mp3 | 5.8 MB |

## 来源

Pixabay 免版税 Lo-Fi 音乐（CC0 / Pixabay Content License），可商用。
原始下载路径（历史）：用户从 `D:\tmp\tttttt\` 提供。

## 路径解析逻辑（见 `slirn_home/app.py` `_DEFAULT_BGMS_DIR`）

```
1. 优先：<repo>/slirn_home/assets/default_bgms/   ← 仓库内，clone 后即可用
2. 回退：D:\tmp\tttttt\                          ← 用户本机历史缓存（兼容）
```

如果仓库内目录缺失（或 mp3 文件名改了），代码会**静默回退**到外部路径，
UI 不会报错，仅 `available=false` 灰显不选项。

## 如何替换

要换成自己机器上的 BGM：直接覆盖 `slirn_home/assets/default_bgms/` 下的文件，
但要保持 `filename` 与 `_DEFAULT_BGMS` 列表里一致，否则 UI 找不到。

如果想完全换目录，修改 `slirn_home/app.py` `_ASSETS_BGMS_DIR` 指向新位置。
