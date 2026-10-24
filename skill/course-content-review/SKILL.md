---
name: course-content-review
description: 对清洗后的课程字幕文本进行人工复核辅助。先自动给与课程讲授内容关系较弱的行打上【待删除】标记，供人工判断；人工保留标记后，再将这些待删除内容抽取到单独文件，并从正文文件中删除。适用于对 cleaned.txt 做最后一轮人工审核的场景。
---

# Course Content Review

这个技能分两步：

1. 自动给疑似“非课程讲授内容”的行加 `×` 标记。
2. 人工审核后，把仍保留该标记的行抽到单独文件，并从正文中删除。

## 适用输入

- `long-video-subtitle-cleaner` 生成的 `*.cleaned.txt`

格式示例：

```text
1. [00:00:00,110 - 00:00:01,110] 这是字幕正文
```

## 第一步：自动打标

运行：

```powershell
python slirn\skill\course-content-review\scripts\mark_non_course_lines.py `
  --input slirn\output\6-8-0_pipeline\6-8-0.cleaned.txt `
  --output slirn\output\6-8-0_pipeline\6-8-0.marked.txt
```

作用：

- 根据启发式规则筛出疑似与课程讲授主线无关的行。
- 在该行序号前添加 `×`。
- 人工检查时：
  - 如果确认应删，保留这个标记。
  - 如果确认不该删，手动删除这个标记。

## 第二步：执行删除

运行：

```powershell
python slirn\skill\course-content-review\scripts\apply_marked_deletions.py `
  --input slirn\output\6-8-0_pipeline\6-8-0.marked.txt `
  --output slirn\output\6-8-0_pipeline\6-8-0.final.txt `
  --removed-output slirn\output\6-8-0_pipeline\6-8-0.manual-removed.txt
```

作用：

- 找出仍带 `×` 标记的行。
- 把这些行完整写入一个单独文件。
- 从正文输出文件中删除这些行。
- 对保留下来的行重新编号。

## 输出

- `*.marked.txt`：自动打标、等待人工审核的文件
- `*.manual-removed.txt`：人工确认删除的内容
- `*.final.txt`：删除这些内容后的最终文本

## 注意

- 这个技能的定位是“人工复核辅助”，不是完全自动判定。
- 打标规则宁可稍微多标，也不要直接自动删正文。
- 如果后续要继续生成连续字幕，建议基于 `*.final.txt` 再跑连续字幕导出。
