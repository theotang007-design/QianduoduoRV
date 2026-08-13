"""AI 分析模块：新闻相关性与情绪判断。

支持 DeepSeek（OpenAI兼容）与 火山引擎，默认 DeepSeek。
Prompt 严格要求：只做信息层面解读，不出现"建议买入/卖出/目标价"等措辞。
"""
import json
import logging
import time

import requests

import config
import database as db

logger = logging.getLogger("ai")

SYSTEM_PROMPT = (
    "你是一名A股信息整理助手。你的任务是对给定股票的相关新闻做信息层面的客观解读。\n"
    "判断范围包括两类消息：\n"
    "1) 公司自身消息（业绩、公告、经营动态等）；\n"
    "2) 行业与政策消息（行业景气度、产业链事件、监管政策等）。"
    "行业消息即使未点名该公司，只要该公司处在受影响的产业链上，也应判定为相关，"
    "并在理由中说明传导路径。例如商业航天发射任务延期，会影响航天配套企业的订单节奏。\n"
    "严格要求：不得出现任何投资建议类措辞，如\"建议买入\"\"建议卖出\"\"目标价\"\"抄底\"\"卖出时机\"等，"
    "不对走势做预测，不评判买卖决策。只陈述新闻事实对公司基本面/行业的潜在影响。\n"
    "仅输出如下JSON，不要输出其他内容：\n"
    '{"相关性":"高/中/低/无关","情绪倾向":"利好/利空/中性",'
    '"事件分类":"业绩/政策/行业/公司治理/突发事件/其他",'
    '"影响范围":"公司/行业","简要理由":"一句话说明判断依据，行业消息需说明传导路径"}'
)


def _build_messages(code, name, news, industry=None):
    content = (
        f"股票：{name}（{code}）\n"
        + (f"所属行业：{industry}\n" if industry else "")
        + f"新闻标题：{news['title']}\n"
        f"新闻摘要：{news.get('summary') or '无'}\n"
        "请判断该新闻与此股票的关联性与信息倾向。"
        "若为行业或政策类消息，请结合所属行业判断是否会传导到该公司。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _call_deepseek(messages):
    url = config.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_volc(messages):
    url = config.VOLC_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.VOLC_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": config.VOLC_MODEL, "messages": messages, "temperature": 0.3}
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call(messages):
    if config.AI_PROVIDER == "volcengine":
        return _call_volc(messages)
    return _call_deepseek(messages)


def _parse_result(text):
    """解析并校验模型输出，非法枚举值归一化为安全默认值。"""
    try:
        data = json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"模型输出无法解析为JSON: {text[:100]}")
        data = json.loads(m.group(0))
    relevance = str(data.get("相关性", "无关"))
    sentiment = str(data.get("情绪倾向", "中性"))
    category = str(data.get("事件分类", "其他"))
    scope = str(data.get("影响范围", "公司"))
    reason = str(data.get("简要理由", ""))[:200]
    # 白名单校验
    if relevance not in ("高", "中", "低", "无关"):
        relevance = "无关"
    if sentiment not in ("利好", "利空", "中性"):
        sentiment = "中性"
    if category not in ("业绩", "政策", "行业", "公司治理", "突发事件", "其他"):
        category = "其他"
    if scope not in ("公司", "行业"):
        scope = "公司"
    return {"relevance": relevance, "sentiment": sentiment,
            "category": category, "scope": scope, "reason": reason}


def analyze_news(code, name, news, industry=None):
    """分析单条新闻。成功返回结果dict；失败返回 None（不入库，留待下次重试）。"""
    if not api_key_ready():
        logger.warning("未配置 %s 的 API Key，跳过AI分析", config.AI_PROVIDER)
        return None

    messages = _build_messages(code, name, news, industry=industry)
    for attempt in range(3):
        try:
            return _parse_result(_call(messages))
        except Exception as e:  # noqa: BLE001
            logger.warning("AI分析失败[%s] 第%d/3次: %s", news["title"][:30], attempt + 1, e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    logger.error("AI分析最终失败，本条消息保留待下次重试: %s", news["title"][:30])
    return None


def api_key_ready():
    """当前选定的AI服务是否已配置API Key。"""
    if config.AI_PROVIDER == "volcengine":
        return bool(config.VOLC_API_KEY)
    return bool(config.DEEPSEEK_API_KEY)


def test_connection():
    """用一条示例新闻实测AI服务是否可用。返回 (是否成功, 说明文本)。"""
    if not api_key_ready():
        return False, f"未配置 {config.AI_PROVIDER} 的 API Key"

    sample = {"title": "某公司发布上半年业绩预告，净利润同比增长20%",
              "summary": "公司公告显示上半年营收与净利润均实现同比增长。"}
    try:
        result = _parse_result(_call(_build_messages("000000", "示例公司", sample)))
        return True, (f"连接正常，模型返回：相关性={result['relevance']}、"
                      f"倾向={result['sentiment']}、分类={result['category']}")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "401" in msg or "Authorization" in msg:
            return False, "API Key 无效或已过期（401），请检查后重新保存"
        if "402" in msg or "Insufficient" in msg or "balance" in msg.lower():
            return False, "账户余额不足（402），请前往服务商充值"
        if "429" in msg:
            return False, "请求过于频繁或超出配额（429），请稍后再试"
        return False, f"调用失败：{msg[:120]}"


def analyze_code(code, name, news_date, industry=None):
    """分析某股票指定日期尚未分析的新闻，入库并返回结果列表。

    受 MAX_NEWS_PER_STOCK_PER_DAY 限制，控制API调用成本。
    分析失败的消息不写入结果表，下次运行会自动重试。
    """
    pending = db.get_unanalyzed_news(
        code, day=news_date, limit=config.MAX_NEWS_PER_STOCK_PER_DAY
    )
    if not pending:
        logger.info("%s(%s) 无待分析消息", name, code)
        return []

    results = []
    failed = 0
    for n in pending:
        result = analyze_news(code, name, n, industry=industry)
        if result is None:
            failed += 1
            continue
        db.add_analysis(n["id"], code, result["relevance"], result["sentiment"],
                        result["category"], result["reason"], result.get("scope", "公司"))
        results.append({"news": n, "analysis": result})

    logger.info("%s(%s) AI分析完成 %d 条，失败 %d 条", name, code, len(results), failed)
    return results