"""技术面简报模块：均线、MACD、RSI、量能变化，客观数据陈述。"""
import logging

import numpy as np

import database as db

logger = logging.getLogger("technical")


def _series(rows, key):
    return [r.get(key) for r in rows]


def _ema(values, span):
    if not values:
        return []
    out = []
    k = 2 / (span + 1)
    prev = values[0]
    for v in values:
        prev = v if prev is None else prev * (1 - k) + v * k
        out.append(prev)
    return out


def _ma(values, n):
    if len(values) < n:
        return None
    return float(np.mean(values[-n:]))


def _rsi(values, n=14):
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    ag = float(np.mean(gains[-n:]))
    al = float(np.mean(losses[-n:]))
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def compute_technical(code):
    """基于已入库行情计算技术指标，返回结构化简报。"""
    rows = db.get_quotes(code, limit=60)
    if not rows:
        return {"error": "无行情数据", "code": code}

    closes = [r["close"] for r in rows if r["close"] is not None]
    vols = [r["volume"] for r in rows if r["volume"] is not None]
    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None

    tech = {
        "code": code,
        "date": latest.get("date"),
        "close": latest.get("close"),
        "pct_change": latest.get("pct_change"),
        "vol": latest.get("volume"),
    }

    # 均线
    tech["ma5"] = _ma(closes, 5)
    tech["ma10"] = _ma(closes, 10)
    tech["ma20"] = _ma(closes, 20)

    # MACD
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)] if ema12 and ema26 else []
    dea = _ema(dif, 9) if dif else []
    macd = None
    if dif and dea:
        macd = (dif[-1] - dea[-1]) * 2
    tech["macd_dif"] = dif[-1] if dif else None
    tech["macd_dea"] = dea[-1] if dea else None
    tech["macd"] = macd

    # RSI
    tech["rsi14"] = _rsi(closes, 14)

    # 量能变化（较近5日均量）
    if len(vols) >= 6:
        avg5 = float(np.mean(vols[-6:-1]))
        tech["vol_ratio"] = round(latest["volume"] / avg5, 2) if avg5 else None
    else:
        tech["vol_ratio"] = None

    # 量能分组标签，供报告按放量/缩量/平稳归类
    tech["vol_level"] = vol_level(tech["vol_ratio"])

    # 5日/20日趋势
    tech["trend_5d"] = _ma(closes, 5) and _ma(closes, 5) >= _ma(closes, 20) if _ma(closes, 5) and _ma(closes, 20) else None

    return tech


def vol_level(vol_ratio):
    """按量比划分量能状态：放量 / 缩量 / 量能平稳。"""
    if vol_ratio is None:
        return "无数据"
    if vol_ratio > 1.5:
        return "放量"
    if vol_ratio < 0.7:
        return "缩量"
    return "量能平稳"


def build_summary(code):
    """生成简洁技术面文字描述（客观陈述）。"""
    t = compute_technical(code)
    if "error" in t:
        return "无行情数据"
    lines = []
    close = t["close"]
    pct = t["pct_change"]
    if pct is not None:
        lines.append(f"收盘 {close}，当日{'跌' if pct < 0 else '涨'}幅 {pct:+.2f}%")
    else:
        lines.append(f"收盘 {close}")

    # 均线位置：带出均线数值与偏离幅度，便于判断偏离程度
    if t["ma5"]:
        dev5 = (close - t["ma5"]) / t["ma5"] * 100
        lines.append(f"{'站上' if close >= t['ma5'] else '跌破'}MA5"
                     f"({t['ma5']:.2f}，{dev5:+.1f}%)")
    if t["ma20"]:
        dev20 = (close - t["ma20"]) / t["ma20"] * 100
        lines.append(f"{'站上' if close >= t['ma20'] else '跌破'}MA20"
                     f"({t['ma20']:.2f}，{dev20:+.1f}%)")

    # 量能
    if t["vol_ratio"] is not None:
        if t["vol_ratio"] > 1.5:
            lines.append(f"放量（量比{t['vol_ratio']:.2f}）")
        elif t["vol_ratio"] < 0.7:
            lines.append(f"缩量（量比{t['vol_ratio']:.2f}）")
        else:
            lines.append(f"量能平稳（量比{t['vol_ratio']:.2f}）")

    # RSI
    if t["rsi14"] is not None:
        lines.append(f"RSI14={t['rsi14']:.0f}")

    return "；".join(lines)


def build_full_tech(code):
    """完整技术指标dict，供个股详情页图表使用。"""
    return compute_technical(code)


def build_commentary(code):
    """基于技术指标生成简短的**技术面解读**（客观陈述，不构成投资建议）。

    描述当前技术面特征，如均线排布、MACD状态、RSI位置等，不做买卖指令。
    """
    t = compute_technical(code)
    if "error" in t:
        return ""

    parts = []
    close = t["close"]
    ma5, ma10, ma20 = t["ma5"], t["ma10"], t["ma20"]

    # 均线排布
    if ma5 and ma10 and ma20:
        # 多头排列：MA5 > MA10 > MA20
        if ma5 > ma10 > ma20:
            parts.append("均线多头排列")
        elif ma20 > ma10 > ma5:
            parts.append("均线空头排列")
        elif ma5 > ma10 and ma10 < ma20:
            parts.append("短期均线上穿，中期趋势待确认")
        elif ma5 < ma10 and ma10 > ma20:
            parts.append("短期均线回踩，中期趋势尚在")
        else:
            parts.append("均线粘合，方向待选择")

    # 价格与均线位置
    if ma20 and close is not None:
        if close > ma20 * 1.05:
            parts.append("明显站上MA20")
        elif close < ma20 * 0.95:
            parts.append("明显跌破MA20")
        else:
            parts.append("MA20附近整理")

    # MACD 状态
    dif, dea, macd = t["macd_dif"], t["macd_dea"], t["macd"]
    if dif is not None and dea is not None:
        if dif > dea and macd is not None and macd > 0:
            parts.append("MACD金叉，动能偏强")
        elif dif < dea and macd is not None and macd < 0:
            parts.append("MACD死叉，动能偏弱")
        elif dif > dea:
            parts.append("MACD金叉，动能待观察")
        elif dif < dea:
            parts.append("MACD死叉，动能待观察")

    # RSI
    rsi = t["rsi14"]
    if rsi is not None:
        if rsi > 70:
            parts.append("RSI超买区")
        elif rsi < 30:
            parts.append("RSI超卖区")
        elif rsi > 55:
            parts.append("RSI偏强")
        elif rsi < 45:
            parts.append("RSI偏弱")
        else:
            parts.append("RSI中性")

    return "；".join(parts) if parts else ""


def build_plain(code):
    """把技术面翻译成大白话，供不熟悉指标的人理解当前状态。

    只描述"现在是什么情况"，不给买卖指令。
    """
    t = compute_technical(code)
    if "error" in t:
        return ""

    close, ma5, ma20 = t["close"], t["ma5"], t["ma20"]
    dif, dea = t["macd_dif"], t["macd_dea"]
    rsi, vr = t["rsi14"], t["vol_ratio"]
    say = []

    # 趋势：价格与中期均线的关系
    if ma20 and close is not None:
        if close > ma20 * 1.05:
            say.append("股价明显高于近一个月的平均成本，目前处在上涨趋势里")
        elif close < ma20 * 0.95:
            say.append("股价明显低于近一个月的平均成本，目前处在下跌趋势里")
        else:
            say.append("股价在近一个月的平均成本附近来回震荡，方向还不明朗")

    # 短期强弱
    if ma5 and close is not None:
        if close > ma5:
            say.append("最近几天买盘占优")
        else:
            say.append("最近几天卖盘占优")

    # MACD：多空力量
    if dif is not None and dea is not None:
        if dif > dea:
            say.append("上涨的力量正在增强")
        else:
            say.append("下跌的力量正在增强")

    # 量能：参与度
    if vr is not None:
        if vr > 1.5:
            say.append("今天成交量突然放大，说明关注度明显升高，通常意味着有资金在积极进出")
        elif vr < 0.7:
            say.append("今天成交量偏小，买卖双方都比较犹豫，观望情绪浓")
        else:
            say.append("今天成交量和往常差不多，市场情绪平稳")

    # RSI：过热或过冷
    if rsi is not None:
        if rsi > 70:
            say.append("短期涨得有点急，已经进入过热区间，往后回调的可能性在变大")
        elif rsi < 30:
            say.append("短期跌得比较多，已经进入超跌区间，往后反弹的可能性在变大")
        elif rsi > 55:
            say.append("整体偏强但还没过热")
        elif rsi < 45:
            say.append("整体偏弱但还没到超跌")
        else:
            say.append("多空双方力量比较均衡")

    return "，".join(say) + "。" if say else ""