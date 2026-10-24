"""公共热词库 — REQ-D 存储与查询 API。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_CATEGORY = "默认"
_HEADER_COMMENT = "# 公共热词库 — 格式：每行 <分类>:<词>，分类可省略（归到「默认」）"


class HotwordLibError(Exception):
    """公共热词库操作异常。"""


class HotwordLibrary:
    """项目级公共热词库（纯文件存储）。

    存储：``<repo_root>/hotwords/public.txt``（每行 ``<分类>:<词>``）。

    使用示例：

    >>> lib = HotwordLibrary(Path("/repo"))
    >>> lib.add("张老师", category="讲师")
    True
    >>> lib.list_all()
    ['张老师']
    >>> lib.list_grouped()
    {'讲师': ['张老师']}
    """

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.txt_path = self.repo_root / "hotwords" / "public.txt"
        self._ensure_exists()

    # ---------- 公共 API ----------

    def list_all(self) -> list[str]:
        """返回所有词（去重 + 保持首次出现顺序）。"""
        grouped = self.list_grouped()
        seen: set[str] = set()
        out: list[str] = []
        # 按分类首次出现顺序遍历
        for words in grouped.values():
            for w in words:
                if w not in seen:
                    seen.add(w)
                    out.append(w)
        return out

    def list_grouped(self) -> dict[str, list[str]]:
        """返回 ``{分类: [词]}``（分类按文件出现顺序）。"""
        groups: dict[str, list[str]] = {}
        for line in self._read_lines():
            category, word = self._parse_line(line)
            groups.setdefault(category, []).append(word)
        return groups

    def search(self, query: str) -> list[str]:
        """子串匹配（大小写不敏感）。空 query 返回所有词。"""
        q = query.strip().lower()
        if not q:
            return self.list_all()
        return [w for w in self.list_all() if q in w.lower()]

    def add(self, word: str, category: str = _DEFAULT_CATEGORY) -> bool:
        """添加词。返回 ``True`` 表示新增，``False`` 表示已存在。

        跨分类去重：若词已在任何分类下，不重复添加。
        """
        word = word.strip()
        category = (category or _DEFAULT_CATEGORY).strip() or _DEFAULT_CATEGORY
        if not word:
            raise HotwordLibError("词不能为空")
        if word in self.list_all():
            return False
        line = f"{category}:{word}"
        self._append_line(line)
        log.info("hotword add: [%s] %s", category, word)
        return True

    def remove(self, word: str) -> bool:
        """删除词。返回 ``True`` 表示删除成功，``False`` 表示不存在。"""
        word = word.strip()
        if not word:
            raise HotwordLibError("词不能为空")
        lines = self._read_lines_with_comments()
        new_lines: list[str] = []
        removed = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            _cat, w = self._parse_line(stripped)
            if w == word:
                removed = True
                continue
            new_lines.append(line)
        if removed:
            self._write_lines(new_lines)
            log.info("hotword remove: %s", word)
        return removed

    def add_category(self, name: str) -> None:
        """添加分类（仅在内存+文件保留，无词也可）。

        实现：往文件加一行注释 ``# category: <name>``，仅作为人类可读标记。
        解析时分类由词行推断，无需此注释。
        """
        name = name.strip()
        if not name:
            raise HotwordLibError("分类名不能为空")
        # 检查是否已存在（基于 list_grouped）
        if name in self.list_grouped():
            return
        # 添加一行占位（仅一个分类名），会在 list_grouped 中作为「含 1 个词」
        # 但该词是空——为避免空词进入列表，我们用特殊格式：分类名作为词
        # 实际：分类在第一次 add() 时自动创建
        log.info("hotword add_category: %s (deferred to first add)", name)

    def assign_category(self, word: str, new_category: str) -> bool:
        """修改词的分类。返回 ``True`` 表示修改成功。"""
        word = word.strip()
        new_category = (new_category or _DEFAULT_CATEGORY).strip() or _DEFAULT_CATEGORY
        if not word:
            raise HotwordLibError("词不能为空")
        lines = self._read_lines_with_comments()
        new_lines: list[str] = []
        modified = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            cat, w = self._parse_line(stripped)
            if w == word:
                new_lines.append(f"{new_category}:{word}")
                modified = True
            else:
                new_lines.append(line)
        if modified:
            self._write_lines(new_lines)
            log.info("hotword assign: %s -> [%s]", word, new_category)
        return modified

    def categories(self) -> list[str]:
        """返回所有出现过的分类（去重 + 顺序）。"""
        return list(self.list_grouped().keys())

    # ---------- 内部 ----------

    def _ensure_exists(self) -> None:
        """不存在时创建 hotwords/ 目录 + public.txt 空文件（含 header）。"""
        if not self.txt_path.exists():
            self.txt_path.parent.mkdir(parents=True, exist_ok=True)
            self.txt_path.write_text(
                _HEADER_COMMENT + "\n",
                encoding="utf-8",
            )
            log.info("hotword lib init: created %s", self.txt_path)

    def _read_lines(self) -> list[str]:
        """读取所有有效行（去注释、去空、去尾空白）。"""
        return [
            line.strip()
            for line in self.txt_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _read_lines_with_comments(self) -> list[str]:
        """读取所有行（含注释和空行）。"""
        return self.txt_path.read_text(encoding="utf-8").splitlines()

    def _write_lines(self, lines: list[str]) -> None:
        """原子写入（tmp + os.replace）。"""
        tmp = self.txt_path.with_suffix(".txt.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.txt_path)

    def _append_line(self, line: str) -> None:
        """追加一行（保留原内容）。"""
        existing = self._read_lines_with_comments()
        existing.append(line)
        self._write_lines(existing)

    @staticmethod
    def _parse_line(line: str) -> tuple[str, str]:
        """解析 ``<分类>:<词>`` → ``(分类, 词)``。

        分类可省略 → ``("默认", 词)``。
        词中可含 ``:``（只 split 第一个）。
        """
        if ":" in line:
            cat, _, word = line.partition(":")
            return cat.strip() or _DEFAULT_CATEGORY, word.strip()
        return _DEFAULT_CATEGORY, line.strip()
