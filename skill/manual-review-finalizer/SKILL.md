---
name: manual-review-finalizer
description: 处理人工复核后的 marked.txt 文件。先将保留删除标记的内容抽取到单独文件，并把未标记内容整理成“手工处理完毕”文档；再对手工处理完毕文档做异常校验，只检查重字、重复词、重复短语，以及异常名词、意思不明确的误识别词、读音相近的错误词，并把这些问题单独记录到校验文档中供人工复核。
---

# Manual Review Finalizer

这个技能用于处理人工复核完成后的 `*.marked.txt` 文件。

## 作用

1. 把仍保留删除标记的行提取到单独文件。
2. 把未标记的保留行整理成“手工处理完毕”的正文文件。
3. 对这个正文文件做重复问题校验。
4. 把疑似存在重字、重复词、重复短语、异常名词、同音误识别词的问题单独记录到校验文档中，供人工查看。
5. 基于 `manual-final.txt` 和 `manual-validation.txt` 再生成两份连续时间轴字幕，以及一份校验问题的连续时间戳定位文件。

## 第一步：整理人工复核结果

运行：

```powershell
python slirn\skill\manual-review-finalizer\scripts\finalize_marked_review.py `
  --input slirn\output\6-8-0_pipeline\6-8-0.marked.txt `
  --output slirn\output\6-8-0_pipeline\6-8-0.manual-final.txt `
  --removed-output slirn\output\6-8-0_pipeline\6-8-0.manual-removed.txt
```

输出：

- `*.manual-final.txt`：人工处理完毕、保留内容整理后的正文
- `*.manual-removed.txt`：仍保留删除标记的内容

## 第二步：异常问题校验

运行：

```powershell
python slirn\skill\manual-review-finalizer\scripts\validate_manual_final.py `
  --input slirn\output\6-8-0_pipeline\6-8-0.manual-final.txt `
  --output slirn\output\6-8-0_pipeline\6-8-0.manual-validation.txt `
  --repeat-marker 〿
```

输出：

- `*.manual-validation.txt`：疑似存在异常重复或异常名词问题的行

## 第三步：导出手工复核后的连续字幕

运行：

```powershell
python slirn\skill\manual-review-finalizer\scripts\export_manual_review_subtitles.py `
  --input slirn\output\6-8-0_pipeline\6-8-0.manual-final.txt `
  --validation-input slirn\output\6-8-0_pipeline\6-8-0.manual-validation.txt `
  --output slirn\output\6-8-0_pipeline\6-8-0.manual-final.continuous.srt `
  --marked-output slirn\output\6-8-0_pipeline\6-8-0.manual-validation.continuous.srt `
  --validation-timestamp-output slirn\output\6-8-0_pipeline\6-8-0.manual-validation.continuous-timestamps.txt `
  --max-chars-per-caption 20
```

输出：

- `*.manual-final.continuous.srt`：对应 `manual-final.txt` 正文内容的连续时间轴字幕
- `*.manual-validation.continuous.srt`：把校验项中的特殊标记替换进字幕后的连续时间轴字幕
- `*.manual-validation.continuous-timestamps.txt`：只记录校验项在连续时间轴里的字幕序号和时间位置

## 连续字幕规则

- 连续字幕按 `manual-final.txt` 的保留顺序重新拼接时间轴。
- 每条保留字幕的原始时长会保留，并平移到新的连续时间线上。
- 若单条字幕超过 `20` 个字，会优先按标点和停顿拆分。
- 两份连续字幕的序号和时间戳完全对齐，便于互相查找。
- 校验时间戳文件只保留异常项，方便人工快速定位。

## 校验原则

- 只检查这些问题：
  - 重复字
  - 重复词
  - 重复短语
  - 异常名词
  - 意思不明确的误识别词
  - 读音相近的错误词
- 对合理叠词不报错，例如：
  - `看看`
  - `想想`
  - `讲讲`
  - `试试`
- 对口语卡顿型重复进行标记，例如：
  - `我我`
  - `他他`
  - `然后然后`
- 对异常名词或同音误识别词进行标记，例如：
  - `短句` -> `短剧`
  - `短局` -> `短剧`
  - `短距` -> `短剧`
  - `慢剧` -> `漫剧`
- 对检测出的异常重复，用特殊符号替换，便于后续批量处理。
- 对检测出的异常名词或误识别词，也用同一个特殊符号替换，并在校验文档中给出建议词。
- 不检查这些问题：
  - 句子是否说完
  - 代词悬空
  - 半句承接词
  - 识别错乱导致的不通顺

## 维护词表

- 默认词表写在 [references/validation-rules.md](/d:/Slirn/WorkSpaces/WaytoAGI/ALI/FunClip-main/skill/manual-review-finalizer/references/validation-rules.md)。
- 后续如果发现新的误识别词，直接按 ``- `错误词` -> `建议词` `` 的格式追加即可。
