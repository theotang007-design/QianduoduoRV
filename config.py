"""全局配置加载：读取 .env 与 holdings 配置文件。"""
import compat  # noqa: F401  运行环境兼容补丁，必须最先导入
import os
import json
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "review.db"
HOLDINGS_FILE = BASE_DIR / "holdings.json"
ENV_FILE = BASE_DIR / ".env"

for d in (DATA_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 加载 .env（不存在则走默认空值）
load_dotenv(ENV_FILE)


def get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ---- 系统信息 ----
APP_NAME = "钱多多智能复盘"

# ---- 运行环境 ----
WEB_HOST = get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(get("WEB_PORT", "5000"))

# ---- AI 服务 ----
AI_PROVIDER = get("AI_PROVIDER", "deepseek").lower()
DEEPSEEK_API_KEY = get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = get("DEEPSEEK_MODEL", "deepseek-chat")
VOLC_API_KEY = get("VOLC_API_KEY")
VOLC_BASE_URL = get("VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
VOLC_MODEL = get("VOLC_MODEL", "doubao-pro-32k")

# ---- 数据限制 ----
MAX_NEWS_PER_STOCK_PER_DAY = int(get("MAX_NEWS_PER_STOCK_PER_DAY", "5"))
MAX_NEWS_PER_SOURCE = int(get("MAX_NEWS_PER_SOURCE", "10"))

# ---- 风险提示文案（产品要求固定展示） ----
RISK_NOTICE = (
    "本报告由程序自动抓取公开信息并结合AI辅助分析生成，仅供信息整理参考，"
    "可能存在信息遗漏、延迟或AI误判，不构成任何投资建议。"
    "股市有风险，投资决策及后果需由使用者自行判断和承担。"
)


def load_holdings():
    """读取持仓列表。返回 [{code, name, qty, cost, added_at}]。"""
    if not HOLDINGS_FILE.exists():
        return []
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_holdings(holdings):
    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)


# ---- AI 配置的读写与热更新 ----
# 允许通过网页修改的键，白名单限定，避免任意键被写入 .env
_EDITABLE_KEYS = (
    "AI_PROVIDER",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "VOLC_API_KEY", "VOLC_BASE_URL", "VOLC_MODEL",
    "MAX_NEWS_PER_STOCK_PER_DAY",
)


def _read_env_file():
    """解析 .env 为字典，保留未被本次修改的其他键。"""
    data = {}
    if not ENV_FILE.exists():
        return data
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


def save_ai_settings(updates: dict):
    """将AI相关配置写入本地 .env 并立即生效。

    仅接受白名单内的键；空值表示不修改该项（避免误清空已保存的Key）。
    """
    data = _read_env_file()
    for k, v in updates.items():
        if k not in _EDITABLE_KEYS:
            continue
        v = (v or "").strip()
        if v:
            data[k] = v

    # 注释用英文，避免部分编辑器/终端以本地编码打开时出现乱码
    lines = ["# Managed by the app settings page. Do NOT commit this file.",
             "# Format: KEY=VALUE", ""]
    for k in _EDITABLE_KEYS:
        if k in data:
            lines.append(f"{k}={data[k]}")
    # 保留其他非AI配置项（如 WEB_PORT）
    for k, v in data.items():
        if k not in _EDITABLE_KEYS:
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reload_settings()


def reload_settings():
    """重新加载 .env 并刷新模块级配置，使修改无需重启服务即生效。"""
    global AI_PROVIDER, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
    global VOLC_API_KEY, VOLC_BASE_URL, VOLC_MODEL
    global MAX_NEWS_PER_STOCK_PER_DAY, MAX_NEWS_PER_SOURCE

    # 以文件内容为准：load_dotenv 只会新增/覆盖，无法反映"键被删除"的情况，
    # 因此先清掉进程内的旧值，避免删除 .env 后仍读到残留配置。
    for k in _EDITABLE_KEYS:
        os.environ.pop(k, None)
    load_dotenv(ENV_FILE, override=True)

    AI_PROVIDER = get("AI_PROVIDER", "deepseek").lower()
    DEEPSEEK_API_KEY = get("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = get("DEEPSEEK_MODEL", "deepseek-chat")
    VOLC_API_KEY = get("VOLC_API_KEY")
    VOLC_BASE_URL = get("VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    VOLC_MODEL = get("VOLC_MODEL", "doubao-pro-32k")
    MAX_NEWS_PER_STOCK_PER_DAY = int(get("MAX_NEWS_PER_STOCK_PER_DAY", "5") or 5)
    MAX_NEWS_PER_SOURCE = int(get("MAX_NEWS_PER_SOURCE", "10") or 10)


def mask_key(key: str) -> str:
    """对API Key做掩码，页面只展示首尾片段，避免明文回显。"""
    if not key:
        return ""
    if len(key) <= 11:
        return key[:3] + "***"
    return f"{key[:6]}…{key[-4:]}"