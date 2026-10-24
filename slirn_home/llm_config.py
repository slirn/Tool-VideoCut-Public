"""大模型用户配置（多厂商模型注册表）— REQ-20260915-008 v2 / REQ-20260916-001 v3。

用户在界面（顶栏 ⚙️）**动态注册**自己拥有的大模型，并指定当前使用哪个：

- 模型名（如 qwen-plus / deepseek-chat / gpt-4o）
- 厂商（自由文本，如 阿里云百炼 / DeepSeek）
- Base URL（端点根地址，具体路径按协议拼接，见下）
- API Key 对应的**系统环境变量名**（Key 本身始终从环境变量读取，界面不存储）
- 协议（REQ-20260916-001）：``openai`` → ``{base_url}/chat/completions``（默认，
  覆盖百炼/DeepSeek/OpenAI 及各类兼容网关）；``anthropic`` → ``{base_url}/v1/messages``

存储：``<repo_root>/config/llm.json``（机器本地运行时状态，与 tasks/、
hotwords/ 同级，gitignore）::

    {"models": [{"id", "provider", "base_url", "api_key_env", "protocol"}, ...],
     "current": "qwen-plus", "updated_at": "..."}

未做任何配置时回退内置默认（阿里云百炼 qwen-plus + DASHSCOPE_API_KEY）；
``SLIRN_LLM_MODEL`` 环境变量仍可覆盖默认模型 id（向后兼容）。
协议调用实现见 revision_service._chat_completion。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

LLM_CONFIG_REL = "config/llm.json"

# 内置默认注册项（未做任何配置时可用，保证开箱即用）
DEFAULT_MODELS: list[dict] = [
    {
        "id": "qwen-plus",
        "provider": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "protocol": "openai",
        "vision": False,
    },
    {
        "id": "qwen-vl-plus",
        "provider": "阿里云百炼（多模态）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "protocol": "openai",
        "vision": True,
    },
]

# 支持的调用协议（REQ-20260916-001）— 端点路径与请求格式见 revision_service
PROTOCOLS = ("openai", "anthropic")
PROTOCOL_LABELS = {"openai": "OpenAI 兼容", "anthropic": "Anthropic"}

# 模型名规则：字母数字开头，可含 . _ : -，≤64 字符（覆盖 qwen2.5-14b-instruct 等）
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")
# 环境变量名规则
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class LLMConfigError(ValueError):
    """配置值不合法。"""


# ---------- 校验 ----------

def _validate_entry(id_: str, provider: str, base_url: str, api_key_env: str,
                    protocol: str = "openai", vision: bool = False) -> dict:
    """校验并规范化一个模型注册项；非法抛 LLMConfigError。"""
    mid = str(id_ or "").strip()
    if not _ID_RE.match(mid):
        raise LLMConfigError(
            f"模型名不合法：{mid!r}（需字母/数字开头，仅可含 . _ : -，最长 64 字符）"
        )
    prov = str(provider or "").strip() or "未填写厂商"
    if len(prov) > 50:
        raise LLMConfigError("厂商名过长（≤50 字符）")
    url = str(base_url or "").strip().rstrip("/")
    pu = urlparse(url)
    if pu.scheme not in ("http", "https") or not pu.netloc:
        raise LLMConfigError(f"Base URL 不合法：{base_url!r}（需 http(s):// 开头的完整地址）")
    if len(url) > 300:
        raise LLMConfigError("Base URL 过长（≤300 字符）")
    env = str(api_key_env or "").strip()
    if not _ENV_RE.match(env):
        raise LLMConfigError(
            f"环境变量名不合法：{api_key_env!r}（字母/下划线开头，仅含字母数字下划线）"
        )
    proto = str(protocol or "openai").strip().lower() or "openai"
    if proto not in PROTOCOLS:
        raise LLMConfigError(f"协议不合法：{protocol!r}（可选：{' / '.join(PROTOCOLS)}）")
    # vision 字段：True/False；缺省 False（向后兼容 — 旧条目视为文本模型）
    vis = bool(vision) if isinstance(vision, bool) else (
        vision in (1, "1", "true", "True", "yes", "on")
    )
    return {"id": mid, "provider": prov, "base_url": url,
            "api_key_env": env, "protocol": proto, "vision": vis}


# ---------- 读写 ----------

def _config_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / LLM_CONFIG_REL


def _load(repo_root: Path | str) -> dict:
    """读配置；无文件/损坏/旧版单模型格式 → 回退默认注册表。

    v2 落盘的条目没有 protocol 字段 → 统一补 "openai"（向后兼容）。
    vision 字段缺省 → 启发式兜底（按模型名关键字判断），用户可在 UI 显式勾选覆盖。
    """
    p = _config_path(repo_root)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("models"), list):
                for m in data["models"]:
                    if isinstance(m, dict):
                        m.setdefault("protocol", "openai")
                        if "vision" not in m:
                            # 旧条目未声明 vision：按模型名启发式判定（仅供迁移过渡，
                            # 用户 UI 显式勾选会覆盖）
                            m["vision"] = _vision_heuristic(str(m.get("id") or ""))
                return data
            # 旧版单模型格式 {"model": "x"}（本功能当期开发中间态）→ 忽略回默认
            log.warning("llm 配置为旧格式，回退默认注册表")
        except Exception as e:  # noqa: BLE001 — 配置损坏回退默认，不阻断功能
            log.warning("llm 配置损坏（%s），回退默认注册表", e)
    return {"models": [dict(m) for m in DEFAULT_MODELS], "current": DEFAULT_MODELS[0]["id"]}


def _vision_heuristic(model_id: str) -> bool:
    """仅用于旧条目 vision 字段缺失时的兜底判定 — 与 revision_service._is_vision_entry 同步。

    新条目一律由用户在 UI 显式勾选，结果落盘无需启发式。
    """
    mid = model_id.strip().lower()
    if not mid:
        return False
    if mid.startswith("qwen-vl") or mid.startswith("qvq") or "qwen2-vl" in mid or "qwen2.5-vl" in mid:
        return True
    if mid.startswith("gpt-4o") or "gpt-4-vision" in mid:
        return True
    if mid.startswith("claude-3") or mid.startswith("claude-4"):
        return True
    return any(k in mid for k in ("vl", "vision", "4o", "opus", "sonnet"))


def _save(repo_root: Path | str, data: dict) -> None:
    p = _config_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def list_models(repo_root: Path | str) -> list[dict]:
    """注册的模型列表（附每个条目的环境变量是否已配置 key_present）。"""
    models = _load(repo_root)["models"]
    out = []
    for m in models:
        e = dict(m)
        e["key_present"] = bool(os.environ.get(m.get("api_key_env", ""), ""))
        out.append(e)
    return out


def get_current(repo_root: Path | str) -> str:
    """当前使用的模型 id（无配置文件时尊重 SLIRN_LLM_MODEL 环境变量覆盖）。"""
    p = _config_path(repo_root)
    if not p.exists():
        return os.environ.get("SLIRN_LLM_MODEL", "").strip() or DEFAULT_MODELS[0]["id"]
    data = _load(repo_root)
    cur = str(data.get("current") or "").strip()
    ids = [m.get("id") for m in data["models"]]
    if cur in ids:
        return cur
    return ids[0] if ids else ""


def get_current_entry(repo_root: Path | str) -> dict | None:
    """当前使用模型的完整注册项（无注册模型 → None）。

    无配置文件时：默认注册项 + SLIRN_LLM_MODEL 覆盖模型 id（向后兼容）。
    """
    data = _load(repo_root)
    cur = get_current(repo_root)
    for m in data["models"]:
        if m.get("id") == cur:
            return dict(m)
    if not _config_path(repo_root).exists() and cur:
        # 环境变量覆盖了模型 id，但注册项仍是默认厂商
        entry = dict(DEFAULT_MODELS[0])
        entry["id"] = cur
        return entry
    return data["models"][0] if data["models"] else None


def add_model(repo_root: Path | str, id_: str, provider: str, base_url: str,
              api_key_env: str, protocol: str = "openai", vision: bool = False) -> list[dict]:
    """添加模型注册项（id 重复拒绝）。返回新列表。

    vision：是否支持图片输入（多模态）。由用户在 UI 显式勾选，避免依赖模型名猜测。
    """
    entry = _validate_entry(id_, provider, base_url, api_key_env, protocol, vision)
    data = _load(repo_root)
    if any(m.get("id") == entry["id"] for m in data["models"]):
        raise LLMConfigError(f"模型 {entry['id']} 已存在（如需修改请点「编辑」）")
    data["models"].append(entry)
    if not data.get("current"):
        data["current"] = entry["id"]  # 第一个注册项自动成为当前模型
    _save(repo_root, data)
    log.info("llm 注册模型: %s (%s, %s, vision=%s)",
             entry["id"], entry["provider"], entry["protocol"], entry["vision"])
    return data["models"]


def update_model(repo_root: Path | str, id_: str, new_id: str, provider: str,
                 base_url: str, api_key_env: str, protocol: str = "openai",
                 vision: bool = False) -> list[dict]:
    """修改模型注册项（REQ-20260916-001）。返回新列表。

    - 原地替换（保持列表位置）；模型名可改，改后若原条目是当前模型 → current 跟随新名
    - 新名与其他条目冲突 → 拒绝
    - vision 字段与其它字段一起落盘
    """
    entry = _validate_entry(new_id, provider, base_url, api_key_env, protocol, vision)
    data = _load(repo_root)
    mid = str(id_ or "").strip()
    idx = next((k for k, m in enumerate(data["models"]) if m.get("id") == mid), None)
    if idx is None:
        raise LLMConfigError(f"模型 {mid} 不存在")
    if entry["id"] != mid and any(m.get("id") == entry["id"] for m in data["models"]):
        raise LLMConfigError(f"模型 {entry['id']} 已存在（不能与其他模型重名）")
    data["models"][idx] = entry
    if data.get("current") == mid:
        data["current"] = entry["id"]
    _save(repo_root, data)
    log.info("llm 更新模型: %s → %s (vision=%s)", mid, entry["id"], entry["vision"])
    return data["models"]


def remove_model(repo_root: Path | str, id_: str) -> list[dict]:
    """删除注册项；删的是当前模型 → 当前切到剩余第一个（无剩余则置空）。"""
    data = _load(repo_root)
    mid = str(id_ or "").strip()
    before = len(data["models"])
    data["models"] = [m for m in data["models"] if m.get("id") != mid]
    if len(data["models"]) == before:
        raise LLMConfigError(f"模型 {mid} 不存在")
    if data.get("current") == mid:
        data["current"] = data["models"][0]["id"] if data["models"] else ""
    _save(repo_root, data)
    log.info("llm 删除模型: %s", mid)
    return data["models"]


def use_model(repo_root: Path | str, id_: str) -> str:
    """设置当前使用的模型（必须已注册）。"""
    data = _load(repo_root)
    mid = str(id_ or "").strip()
    if not any(m.get("id") == mid for m in data["models"]):
        raise LLMConfigError(f"模型 {mid} 未注册")
    data["current"] = mid
    _save(repo_root, data)
    log.info("llm 当前模型 → %s", mid)
    return mid
