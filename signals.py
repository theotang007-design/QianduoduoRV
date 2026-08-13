"""波段趋势信号模块：阶段顶部（减仓警戒）与波段底部（低吸观察）判定。

设计前提（由使用者设定）：
- 标的默认为业绩基本面优质的品种，不适用于垃圾股与连板妖股；
- 只做波段趋势的**指标状态陈述**，不预测涨跌、不给买卖指令；
- 主升浪允许指标钝化，单一指标过热不作为顶部判据。

使用指标：MA20、BIAS20（20日乖离率）、RSI14、30分钟MACD。
"""
import logging

import numpy as np

import database as db
import technical

logger = logging.getLogger("signals")

# ---- 阈值参数（集中管理，便于按个股波动特性调整） ----
BIAS_HOT = 12.0          # BIAS20 正乖离进入过热参考区
BIAS_EXTREME = 20.0      # BIAS20 极端正乖离
BIAS_OVERSOLD = -10.0    # BIAS20 负乖离进入超跌参考区
BIAS_DEEP = -15.0        # BIAS20 深度负乖离
RSI_HOT = 70.0           # RSI14 高位区
RSI_COLD = 30.0          # RSI14 低位区
RSI_TURN = 5.0           # RSI 由高位回落的幅度阈值，视为高位拐头
MA20_FLAT = 0.15         # MA20 5日斜率绝对值低于该百分比视为走平
VOL_HEAVY = 1.5          # 放量阈值（量比）
VOL_DRY = 0.7            # 缩量阈值（量比）


def _pct(a, b):
    """b 为基准的百分比差。"""
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100


def _ma_series(closes, n):
    """滚动均线序列，长度不足的位置为 None。"""
    out = []
    for i in range(len(closes)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(float(np.mean(closes[i + 1 - n:i + 1])))
    return out


def _rsi_series(closes, n=14):
    """滚动 RSI 序列，长度不足的位置为 None。"""
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i < n:
            continue
        out[i] = technical._rsi(closes[: i + 1], n)
    return out


def _macd_hist(closes):
    """MACD 柱状值序列（(DIF-DEA)*2）。"""
    ema12 = technical._ema(closes, 12)
    ema26 = technical._ema(closes, 26)
    if not ema12 or not ema26:
        return [], [], []
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = technical._ema(dif, 9)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif, dea, hist


def _swing_highs(values, span=3):
    """局部高点下标：左右各 span 根都不高于自身。"""
    idx = []
    for i in range(span, len(values) - span):
        window = values[i - span:i + span + 1]
        if values[i] == max(window) and values[i] > values[i - 1]:
            idx.append(i)
    return idx


def _swing_lows(values, span=3):
    """局部低点下标：左右各 span 根都不低于自身。"""
    idx = []
    for i in range(span, len(values) - span):
        window = values[i - span:i + span + 1]
        if values[i] == min(window) and values[i] < values[i - 1]:
            idx.append(i)
    return idx


def min30_divergence(code, lookback=120):
    """30分钟MACD背离检测。

    顶背离：价格创新高，但对应位置的 MACD 柱峰值不再新高。
    底背离：价格创新低，但对应位置的 MACD 柱谷值不再新低。
    返回 {"top": bool, "bottom": bool, "detail": str, "bars": int}。
    """
    rows = db.get_min30(code, limit=lookback)
    result = {"top": False, "bottom": False, "detail": "", "bars": len(rows)}
    if len(rows) < 40:
        result["detail"] = "30分钟数据不足，无法判断背离"
        return result

    closes = [r["close"] for r in rows if r["close"] is not None]
    highs = [r["high"] or r["close"] for r in rows if r["close"] is not None]
    lows = [r["low"] or r["close"] for r in rows if r["close"] is not None]
    _, _, hist = _macd_hist(closes)
    if not hist:
        result["detail"] = "30分钟MACD计算失败"
        return result

    # 顶背离：取最近两个价格波峰比较
    peaks = _swing_highs(highs)
    if len(peaks) >= 2:
        i1, i2 = peaks[-2], peaks[-1]
        # 波峰附近的 MACD 柱最大值（±2根容差，兼容指标滞后）
        h1 = max(hist[max(0, i1 - 2):i1 + 3])
        h2 = max(hist[max(0, i2 - 2):i2 + 3])
        if highs[i2] > highs[i1] and h2 < h1 and h2 > 0:
            result["top"] = True
            result["detail"] = (f"30分钟顶背离：价格由 {highs[i1]:.2f} 抬高至 "
                               f"{highs[i2]:.2f}，MACD柱峰由 {h1:.3f} 降至 {h2:.3f}")

    # 底背离：取最近两个价格波谷比较
    troughs = _swing_lows(lows)
    if len(troughs) >= 2:
        j1, j2 = troughs[-2], troughs[-1]
        l1 = min(hist[max(0, j1 - 2):j1 + 3])
        l2 = min(hist[max(0, j2 - 2):j2 + 3])
        if lows[j2] < lows[j1] and l2 > l1 and l2 < 0:
            result["bottom"] = True
            d = (f"30分钟底背离：价格由 {lows[j1]:.2f} 下探至 {lows[j2]:.2f}，"
                 f"MACD柱谷由 {l1:.3f} 收窄至 {l2:.3f}")
            result["detail"] = (result["detail"] + "；" + d) if result["detail"] else d

    if not result["detail"]:
        result["detail"] = "30分钟MACD未出现明确背离"
    return result


def compute_state(code):
    """计算波段判定所需的全部状态量。缺数据时返回 {"error": ...}。"""
    rows = db.get_quotes(code, limit=90)
    closes = [r["close"] for r in rows if r["close"] is not None]
    if len(closes) < 25:
        return {"error": "日线数据不足（需至少25个交易日）"}

    vols = [r["volume"] for r in rows if r["volume"] is not None]
    latest = rows[-1]
    close = latest.get("close")

    ma20_ser = _ma_series(closes, 20)
    ma5_ser = _ma_series(closes, 5)
    ma20 = ma20_ser[-1]
    ma5 = ma5_ser[-1]

    # BIAS20 = (收盘 - MA20) / MA20 × 100
    bias20 = _pct(close, ma20)
    bias20_prev = _pct(closes[-2], ma20_ser[-2]) if len(closes) > 1 else None

    # MA20 斜率：近5日与近10日的变化率，用于识别走平/拐头
    slope5 = _pct(ma20, ma20_ser[-6]) if len(ma20_ser) > 6 and ma20_ser[-6] else None
    slope10 = _pct(ma20, ma20_ser[-11]) if len(ma20_ser) > 11 and ma20_ser[-11] else None

    if slope5 is None:
        ma20_dir = "数据不足"
    elif slope5 > MA20_FLAT:
        ma20_dir = "向上"
    elif slope5 < -MA20_FLAT:
        ma20_dir = "向下"
    else:
        ma20_dir = "走平"

    # MA20 是否刚由上转平/转下（拐头）
    ma20_turning = (slope10 is not None and slope5 is not None
                    and slope10 > MA20_FLAT and slope5 <= MA20_FLAT)

    # RSI14 及其近3日变化，用于识别高位拐头
    rsi_ser = _rsi_series(closes, 14)
    rsi = rsi_ser[-1]
    rsi_max5 = max([v for v in rsi_ser[-5:] if v is not None], default=None)
    rsi_min5 = min([v for v in rsi_ser[-5:] if v is not None], default=None)
    rsi_top_turn = (rsi is not None and rsi_max5 is not None
                    and rsi_max5 >= RSI_HOT and (rsi_max5 - rsi) >= RSI_TURN)
    rsi_bottom_turn = (rsi is not None and rsi_min5 is not None
                       and rsi_min5 <= RSI_COLD and (rsi - rsi_min5) >= RSI_TURN)

    # 收盘价与 MA20 的位置关系，以及"有效跌破"（连续2日收盘在MA20下方）
    below_ma20 = close is not None and ma20 and close < ma20
    below_prev = (len(closes) > 1 and ma20_ser[-2]
                  and closes[-2] < ma20_ser[-2])
    break_ma20_confirmed = bool(below_ma20 and below_prev)
    # 收复：连续2日收盘在MA20上方
    above_ma20 = close is not None and ma20 and close >= ma20
    above_prev = (len(closes) > 1 and ma20_ser[-2]
                  and closes[-2] >= ma20_ser[-2])
    reclaim_ma20 = bool(above_ma20 and above_prev)

    # 量能
    vol_ratio = None
    if len(vols) >= 6:
        avg5 = float(np.mean(vols[-6:-1]))
        vol_ratio = round(vols[-1] / avg5, 2) if avg5 else None

    # 近20日高低点，作为区间与止损参考
    high20 = max(closes[-20:])
    low20 = min(closes[-20:])

    # K线形态：是否收出止跌信号（下影线较长或阳线吞没前日实体）
    o, h, l, c = (latest.get("open"), latest.get("high"),
                  latest.get("low"), latest.get("close"))
    lower_shadow = None
    if None not in (o, h, l, c) and h != l:
        lower_shadow = (min(o, c) - l) / (h - l) * 100
    bullish_engulf = False
    if len(rows) > 1 and None not in (o, c):
        po, pc = rows[-2].get("open"), rows[-2].get("close")
        if None not in (po, pc) and pc < po:  # 前一日阴线
            bullish_engulf = c > o and c > po and o < pc

    div = min30_divergence(code)

    return {
        "code": code, "date": latest.get("date"), "close": close,
        "pct_change": latest.get("pct_change"),
        "ma5": ma5, "ma20": ma20,
        "bias20": bias20, "bias20_prev": bias20_prev,
        "ma20_slope5": slope5, "ma20_slope10": slope10,
        "ma20_dir": ma20_dir, "ma20_turning": ma20_turning,
        "rsi14": rsi, "rsi_max5": rsi_max5, "rsi_min5": rsi_min5,
        "rsi_top_turn": rsi_top_turn, "rsi_bottom_turn": rsi_bottom_turn,
        "below_ma20": below_ma20, "break_ma20_confirmed": break_ma20_confirmed,
        "reclaim_ma20": reclaim_ma20,
        "vol_ratio": vol_ratio, "high20": high20, "low20": low20,
        "lower_shadow": lower_shadow, "bullish_engulf": bullish_engulf,
        "div_top": div["top"], "div_bottom": div["bottom"],
        "div_detail": div["detail"], "min30_bars": div["bars"],
    }


def _fmt(v, unit="", nd=2):
    if v is None:
        return "—"
    return f"{v:.{nd}f}{unit}"


def build_top_block(s):
    """阶段顶部：警戒条件 + 确认条件 + 假顶场景。"""
    warn_hits, confirm_hits = [], []

    # ---- 警戒信号（任一命中即进入减仓警戒观察）----
    if s["bias20"] is not None and s["bias20"] >= BIAS_EXTREME:
        warn_hits.append(f"BIAS20 达 {s['bias20']:+.1f}%，处于极端正乖离区"
                         f"（≥{BIAS_EXTREME:.0f}%），偏离中期成本过大")
    elif s["bias20"] is not None and s["bias20"] >= BIAS_HOT:
        warn_hits.append(f"BIAS20 达 {s['bias20']:+.1f}%，进入过热参考区"
                         f"（≥{BIAS_HOT:.0f}%），仅作过热提示")

    if s["rsi_top_turn"]:
        warn_hits.append(f"RSI14 由近5日高点 {_fmt(s['rsi_max5'], nd=0)} 回落至 "
                         f"{_fmt(s['rsi14'], nd=0)}，出现高位拐头")
    elif s["rsi14"] is not None and s["rsi14"] >= RSI_HOT:
        warn_hits.append(f"RSI14={s['rsi14']:.0f} 处于高位区，尚未拐头（主升浪允许钝化）")

    if s["ma20_turning"]:
        warn_hits.append(f"MA20 由上行转走平（近5日斜率 {_fmt(s['ma20_slope5'], '%')}，"
                         f"近10日 {_fmt(s['ma20_slope10'], '%')}）")
    elif s["ma20_dir"] == "走平":
        warn_hits.append(f"MA20 走平（近5日斜率 {_fmt(s['ma20_slope5'], '%')}）")

    if s["div_top"]:
        vol_txt = ("并伴随放量" if (s["vol_ratio"] or 0) > VOL_HEAVY else "但量能未明显放大")
        warn_hits.append(f"{s['div_detail'].split('；')[0]}，{vol_txt}")

    # ---- 趋势见顶确认（需多重共振）----
    if s["break_ma20_confirmed"] and s["ma20_dir"] in ("走平", "向下"):
        confirm_hits.append(f"收盘价连续2日跌破MA20（{_fmt(s['ma20'])}），"
                            f"且MA20已{s['ma20_dir']}")
    if s["div_top"] and (s["vol_ratio"] or 0) > VOL_HEAVY and s["rsi_top_turn"]:
        confirm_hits.append("30分钟MACD顶背离 + 放量 + RSI高位拐头三重共振")
    if s["ma20_dir"] == "向下" and s["below_ma20"]:
        confirm_hits.append(f"MA20 向下（近5日 {_fmt(s['ma20_slope5'], '%')}）"
                            f"且股价运行于MA20下方")

    if confirm_hits:
        level, label = "confirm", "趋势见顶确认信号已出现"
    elif warn_hits:
        level, label = "warn", "进入减仓警戒观察区"
    else:
        level, label = "none", "未进入减仓警戒区"

    return {
        "level": level, "label": label,
        "warn_hits": warn_hits, "confirm_hits": confirm_hits,
        "warn_rules": [
            f"BIAS20 ≥ {BIAS_HOT:.0f}% 进入过热参考（≥{BIAS_EXTREME:.0f}% 为极端），"
            "仅作过热提示，不单独构成警戒",
            "MA20 由上行转为走平或拐头向下",
            "30分钟MACD出现顶背离，且日线同步放量",
            f"RSI14 曾达 {RSI_HOT:.0f} 以上后回落 {RSI_TURN:.0f} 点以上（高位拐头）",
        ],
        "confirm_rules": [
            f"收盘价连续2个交易日跌破MA20（当前 {_fmt(s['ma20'])}），"
            "且MA20同步走平或向下",
            "30分钟MACD顶背离 + 日线放量 + RSI高位拐头，三者共振",
            "MA20 明确向下且股价持续运行于MA20下方（趋势结构转空）",
            f"跌破近20日震荡区间下沿 {_fmt(s['low20'])} 且未快速收回",
        ],
        "fake_rules": [
            f"主升浪中 BIAS20 长期高企、RSI 持续钝化在 {RSI_HOT:.0f} 上方，"
            "此时单看这两项会反复给出假顶",
            "单日放量长上影或急跌破MA20，但次日即收复且MA20仍向上，属强势洗盘而非见顶",
            "30分钟级别顶背离在上升趋势中会多次出现，未叠加日线MA20走坏时多为中途调整",
            "除权、指数系统性回调或板块轮动引起的同步下挫，个股趋势结构未破坏",
        ],
    }


def build_bottom_block(s):
    """波段底部：观察条件 + 确认条件 + 假底场景。"""
    # 前置门槛：MA20 拐头向下则放弃底部判断
    if s["ma20_dir"] == "向下":
        return {
            "level": "disabled",
            "label": "MA20 向下，趋势转弱，按规则放弃底部判断",
            "watch_hits": [], "confirm_hits": [],
            "watch_rules": ["前置条件：MA20 必须维持向上，否则不评估底部机会"],
            "confirm_rules": [], "fake_rules": [],
            "zone": None, "stop_loss": None,
        }

    watch_hits, confirm_hits = [], []

    if s["bias20"] is not None and s["bias20"] <= BIAS_DEEP:
        watch_hits.append(f"BIAS20 达 {s['bias20']:+.1f}%，深度负乖离"
                          f"（≤{BIAS_DEEP:.0f}%），只代表超卖不等于底部")
    elif s["bias20"] is not None and s["bias20"] <= BIAS_OVERSOLD:
        watch_hits.append(f"BIAS20 达 {s['bias20']:+.1f}%，进入超跌参考区"
                          f"（≤{BIAS_OVERSOLD:.0f}%）")

    if s["rsi14"] is not None and s["rsi14"] <= RSI_COLD:
        watch_hits.append(f"RSI14={s['rsi14']:.0f} 处于低位区（≤{RSI_COLD:.0f}）")

    if s["div_bottom"]:
        watch_hits.append("30分钟MACD出现底背离（单独不构成底部，需日线共振）")

    if s["ma20_dir"] == "向上" and s["bias20"] is not None and abs(s["bias20"]) <= 3:
        watch_hits.append(f"MA20 向上且股价回踩至MA20附近"
                          f"（BIAS20 {s['bias20']:+.1f}%）")

    # ---- 止跌确认：量能 / K线 / 指标 / 结构，需至少两项共振 ----
    if s["ma20_dir"] == "向上":
        if s["vol_ratio"] is not None and s["vol_ratio"] < VOL_DRY:
            confirm_hits.append(f"量能：回调过程量能萎缩（量比 {s['vol_ratio']:.2f}），"
                               f"抛压释放")
        if (s["lower_shadow"] or 0) >= 40:
            confirm_hits.append(f"K线：收出长下影线"
                               f"（下影占全幅 {s['lower_shadow']:.0f}%）")
        elif s["bullish_engulf"]:
            confirm_hits.append("K线：阳线吞没前一日阴线")
        if s["rsi_bottom_turn"]:
            confirm_hits.append(f"指标：RSI14 由近5日低点 {_fmt(s['rsi_min5'], nd=0)} "
                               f"回升至 {_fmt(s['rsi14'], nd=0)}")
        elif s["div_bottom"]:
            confirm_hits.append("指标：30分钟MACD底背离后柱体收敛转强")
        if s["reclaim_ma20"]:
            confirm_hits.append(f"结构：连续2日收盘站回MA20（{_fmt(s['ma20'])}）上方")

    # 需先进入观察区，且确认要素至少两项共振
    if s["ma20_dir"] == "数据不足":
        level, label = "none", "MA20 趋势数据不足，暂不评估"
    elif watch_hits and len(confirm_hits) >= 2:
        level, label = "confirm", "止跌确认信号已出现（多项共振）"
    elif watch_hits:
        level, label = "watch", "进入低吸观察区，止跌确认信号不足"
    else:
        # 未进入超跌/回踩观察区时，确认要素无参考意义，不展示以免误读
        level, label = "none", "未进入低吸观察区"
        confirm_hits = []

    # 观察区间：MA20 至 MA20×(1+超跌阈值)，以及近20日低点
    zone = None
    if s["ma20"]:
        zone = {
            "ma20": s["ma20"],
            "oversold_low": s["ma20"] * (1 + BIAS_DEEP / 100),
            "oversold_high": s["ma20"] * (1 + BIAS_OVERSOLD / 100),
            "low20": s["low20"],
        }
    # 止损参考：近20日低点下方3%，或MA20下方深度负乖离位，取更靠上者
    stop_loss = None
    if s["low20"]:
        stop_loss = s["low20"] * 0.97

    return {
        "level": level, "label": label,
        "watch_hits": watch_hits, "confirm_hits": confirm_hits,
        "watch_rules": [
            "前置条件：MA20 维持向上；MA20 拐头向下则直接判定趋势转弱，放弃底部判断",
            f"超跌参考区：BIAS20 ≤ {BIAS_OVERSOLD:.0f}%（≤{BIAS_DEEP:.0f}% 为深度超跌），"
            "超卖不等于底部",
            f"RSI14 进入 {RSI_COLD:.0f} 以下低位区",
            "30分钟MACD底背离（仅作观察线索，不可单独作底）",
        ],
        "confirm_rules": [
            f"量能：下跌过程量能萎缩至量比 {VOL_DRY} 以下，抛压衰减",
            "K线：收出长下影线（下影占全幅40%以上）或阳线吞没前一日阴线",
            f"指标：RSI14 自低位回升 {RSI_TURN:.0f} 点以上，或30分钟MACD底背离后柱体转正",
            f"结构：连续2个交易日收盘站回MA20（当前 {_fmt(s['ma20'])}）上方",
            "以上量能、K线、指标、结构需至少形成两项以上共振才视为止跌确认",
        ],
        "fake_rules": [
            "MA20 已经拐头向下时的任何超跌反弹，都属于下跌中继，不作底部",
            "仅有30分钟底背离、日线未止跌：分钟级背离可被连续下跌不断修正",
            "反弹时量能持续萎缩、无法站回MA20，属于缩量反抽",
            f"跌破近20日低点 {_fmt(s['low20'])} 后的第一次反弹，未收复该点位前视为中继",
            "利空未出尽（业绩预警、监管问询等）期间的技术性反弹",
        ],
        "zone": zone, "stop_loss": stop_loss,
    }


def build_state_block(s):
    """①当前指标状态：客观数值陈述。"""
    items = [
        {"k": "收盘价", "v": _fmt(s["close"]),
         "note": (f"当日 {s['pct_change']:+.2f}%" if s["pct_change"] is not None else "")},
        {"k": "MA20", "v": _fmt(s["ma20"]),
         "note": f"方向{s['ma20_dir']}（近5日斜率 {_fmt(s['ma20_slope5'], '%')}，"
                 f"近10日 {_fmt(s['ma20_slope10'], '%')}）"},
        {"k": "BIAS20", "v": (f"{s['bias20']:+.2f}%" if s["bias20"] is not None else "—"),
         "note": _bias_note(s["bias20"])},
        {"k": "RSI14", "v": _fmt(s["rsi14"], nd=0),
         "note": _rsi_note(s)},
        {"k": "30分钟MACD", "v": ("顶背离" if s["div_top"] else
                                 ("底背离" if s["div_bottom"] else "无背离")),
         "note": s["div_detail"]},
        {"k": "量能", "v": (f"量比 {s['vol_ratio']:.2f}" if s["vol_ratio"] else "—"),
         "note": technical.vol_level(s["vol_ratio"])},
        {"k": "近20日区间", "v": f"{_fmt(s['low20'])} ~ {_fmt(s['high20'])}",
         "note": "区间上下沿为结构参考位"},
    ]
    return items


def _bias_note(bias):
    if bias is None:
        return ""
    if bias >= BIAS_EXTREME:
        return f"极端正乖离（≥{BIAS_EXTREME:.0f}%），过热参考"
    if bias >= BIAS_HOT:
        return f"正乖离偏高（≥{BIAS_HOT:.0f}%），过热参考"
    if bias <= BIAS_DEEP:
        return f"深度负乖离（≤{BIAS_DEEP:.0f}%），超卖参考"
    if bias <= BIAS_OVERSOLD:
        return f"负乖离偏大（≤{BIAS_OVERSOLD:.0f}%），超卖参考"
    return "乖离处于常态区间"


def _rsi_note(s):
    if s["rsi14"] is None:
        return ""
    if s["rsi_top_turn"]:
        return f"高位拐头（近5日高点 {s['rsi_max5']:.0f}）"
    if s["rsi_bottom_turn"]:
        return f"低位回升（近5日低点 {s['rsi_min5']:.0f}）"
    if s["rsi14"] >= RSI_HOT:
        return "高位区，主升浪允许钝化"
    if s["rsi14"] <= RSI_COLD:
        return "低位区"
    return "中性区间"


def build_signal(code):
    """完整波段信号结构：①指标状态 ②阶段顶部 ③波段底部。"""
    s = compute_state(code)
    if "error" in s:
        return {"error": s["error"], "code": code}
    top = build_top_block(s)
    bottom = build_bottom_block(s)
    return {
        "code": code, "date": s["date"], "state": s,
        "state_items": build_state_block(s),
        "top": top, "bottom": bottom,
        # 供报告页顶部汇总提醒使用
        "alert": _alert_of(top, bottom),
    }


def _alert_of(top, bottom):
    """归纳为一条提醒标签，便于报告页汇总。none 表示无需提醒。"""
    if top["level"] == "confirm":
        return {"type": "top", "level": "confirm", "text": "趋势见顶确认信号"}
    if bottom["level"] == "confirm":
        return {"type": "bottom", "level": "confirm", "text": "止跌确认信号"}
    if top["level"] == "warn":
        return {"type": "top", "level": "warn", "text": "减仓警戒区"}
    if bottom["level"] == "watch":
        return {"type": "bottom", "level": "watch", "text": "低吸观察区"}
    return None
