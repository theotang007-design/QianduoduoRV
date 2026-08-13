"""复盘报告生成与归档。生成结构化JSON报告并保存到 daily_reports。"""
import logging
from datetime import datetime

import config
import database as db
import technical
import signals
import ai_analyzer

logger = logging.getLogger("report")


def build_report(account_id, news_date):
    """生成指定账户某交易日的结构化复盘报告。"""
    holdings = db.list_holdings(account_id)
    stocks = []
    total_pending = 0        # 已抓取但未完成AI分析的消息数
    alerts = []              # 触发顶部/底部信号的股票，用于报告页顶部提醒
    for h in holdings:
        code, name = h["code"], h["name"]
        tech = technical.compute_technical(code)
        tech_summary = technical.build_summary(code)
        tech_commentary = technical.build_commentary(code)
        tech_plain = technical.build_plain(code)

        # 波段趋势信号：阶段顶部（减仓警戒）/ 波段底部（低吸观察）
        sig = signals.build_signal(code)
        if sig.get("alert"):
            alerts.append({"code": code, "name": name, **sig["alert"]})

        # 相对均线的偏离幅度：偏离过大是调仓的重要参考
        if tech.get("ma5") and tech.get("close"):
            tech["dev_ma5"] = (tech["close"] - tech["ma5"]) / tech["ma5"] * 100
        else:
            tech["dev_ma5"] = None
        if tech.get("ma20") and tech.get("close"):
            tech["dev_ma20"] = (tech["close"] - tech["ma20"]) / tech["ma20"] * 100
        else:
            tech["dev_ma20"] = None

        # 当日消息按情绪分组；"无关"不进报告，未分析的单独计数用于提示
        grouped = {"利好": [], "利空": [], "中性": []}
        pending = 0
        for n in db.get_news_for_date(code, news_date):
            relevance = n.get("relevance")
            if not relevance:
                pending += 1          # 尚未分析，不能当作"无消息"
                continue
            if relevance == "无关":
                continue
            sent = n.get("sentiment") or "中性"
            if sent not in grouped:
                sent = "中性"
            grouped[sent].append({
                "title": n["title"], "reason": n.get("reason") or "",
                "category": n.get("category") or "其他",
                "relevance": relevance,
                "scope": n.get("scope") or "公司",
                "source": n.get("source") or "",
                "publish_time": n.get("publish_time") or "",
                "url": n.get("url") or "",
            })

        total_pending += pending
        stocks.append({
            "code": code, "name": name,
            "qty": h.get("qty"), "cost": h.get("cost"),
            "tech": tech, "tech_summary": tech_summary,
            "tech_commentary": tech_commentary,
            "tech_plain": tech_plain,
            "signal": sig,
            "news": grouped,
            "has_news": any(grouped.values()),
            "pending_news": pending,
        })

    account = db.get_account(account_id) or {}
    # 提醒排序：见顶/止跌确认优先，其次警戒与观察
    _order = {"confirm": 0, "warn": 1, "watch": 2}
    alerts.sort(key=lambda a: _order.get(a.get("level"), 9))
    return {
        "date": news_date,
        "account_id": account_id,
        "account_name": account.get("name", ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stocks,
        "alerts": alerts,
        "risk_notice": config.RISK_NOTICE,
        "ai_ready": ai_analyzer.api_key_ready(),
        "pending_news": total_pending,
    }


def generate_and_save(account_id, news_date):
    """生成并持久化指定账户的当日报告。"""
    report = build_report(account_id, news_date)
    db.save_report(account_id, news_date, report)
    return report


def render_text(report):
    """生成纯文本版报告（便于命令行查看/调试）。"""
    title = f"【每日复盘报告】{report['date']}"
    if report.get("account_name"):
        title += f"  账户：{report['account_name']}"
    lines = [title, "", "一、持仓总览"]
    for s in report["stocks"]:
        pct = (s["tech"] or {}).get("pct_change")
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        lines.append(f"  - {s['name']}({s['code']})  当日涨跌幅: {pct_str}")

    if report.get("alerts"):
        lines.extend(["", "★ 波段信号提醒"])
        for a in report["alerts"]:
            lines.append(f"  - {a['name']}({a['code']})：{a['text']}")

    lines.extend(["", "二、逐股详情"])
    for s in report["stocks"]:
        lines.append(f"  ┌ {s['name']}（{s['code']}）")
        lines.append(f"  ├ 技术面：{s['tech_summary']}")
        if s.get("tech_commentary"):
            lines.append(f"  ├ 技术面解读：{s['tech_commentary']}")
        if s.get("tech_plain"):
            lines.append(f"  ├ 大白话：{s['tech_plain']}")
        sig = s.get("signal") or {}
        if not sig.get("error"):
            lines.append(f"  ├ 阶段顶部：{sig['top']['label']}")
            for hit in sig["top"]["warn_hits"] + sig["top"]["confirm_hits"]:
                lines.append(f"  │    · {hit}")
            lines.append(f"  ├ 波段底部：{sig['bottom']['label']}")
            for hit in sig["bottom"]["watch_hits"] + sig["bottom"]["confirm_hits"]:
                lines.append(f"  │    · {hit}")
        lines.append("  ├ 消息面：")
        if not s["has_news"]:
            lines.append("  │    今日无重大消息")
        else:
            for sent in ("利好", "利空", "中性"):
                for it in s["news"][sent]:
                    lines.append(f"  │    [{sent}] {it['title']}")
                    if it["reason"]:
                        lines.append(f"  │        理由：{it['reason']}")
                    lines.append(
                        f"  │        （{it['source']}　{it['publish_time']}　{it['url']}）"
                    )
        lines.append("  └")

    lines.extend(["", "三、风险提示", f"  {config.RISK_NOTICE}"])
    return "\n".join(lines)