"""数据采集模块：行情与新闻抓取，含重试、多源降级与去重。

行情数据源（按优先级自动降级）：
  1. 腾讯财经 K线接口（前复权，稳定）
  2. 新浪财经 K线接口
  3. akshare（备用）
新闻数据源：
  1. 东方财富资讯搜索（按股票名称检索）
  2. 东方财富个股公告

设计原则：单一数据源失败不影响整体流程，全部失败时记录日志并跳过。
"""
import json
import time
import logging
from urllib.parse import quote

import requests

import config
import database as db

logger = logging.getLogger("collector")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
_TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_SINA_KLINE = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
_EM_SEARCH = "https://search-api-web.eastmoney.com/search/jsonp"
_EM_ANNOUNCE = "https://np-anotice-stock.eastmoney.com/api/security/ann"
# 分钟级K线（新浪移动端接口，支持 scale=30）
_SINA_MIN_KLINE = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketDataService.getKLineData"
)


def _retry(fn, times=3, delay=1.5, desc=""):
    """通用重试包装，全部失败返回 None 而不抛异常。"""
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("%s 失败(第%d/%d次): %s", desc, i + 1, times, e)
            if i < times - 1:
                time.sleep(delay * (i + 1))
    logger.error("%s 重试%d次后仍失败: %s", desc, times, last)
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_code(code):
    """统一为6位纯数字代码。"""
    return str(code).strip().upper().replace(".SH", "").replace(".SZ", "").replace("SH", "").replace("SZ", "")[-6:]


def _market_prefix(code):
    """判断沪深市场前缀。6/9开头为沪市，其余为深市。"""
    code = normalize_code(code)
    return "sh" if code[0] in ("6", "9") else "sz"


def fetch_stock_name(code):
    """按代码查询股票名称（新浪实时行情接口）。查不到返回 None。

    用于新增持仓时校正手填名称，名称准确才能保证新闻检索命中。
    """
    symbol = _market_prefix(code) + normalize_code(code)

    def _do():
        r = requests.get(f"https://hq.sinajs.cn/list={symbol}", timeout=12,
                         headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"})
        r.encoding = "gbk"
        r.raise_for_status()
        # 形如: var hq_str_sh600519="贵州茅台,1325.000,...";
        text = r.text
        start, end = text.find('"'), text.rfind('"')
        if start == -1 or end <= start:
            return None
        fields = text[start + 1:end].split(",")
        name = fields[0].strip() if fields else ""
        return name or None

    return _retry(_do, times=2, desc=f"查询股票名称 {code}")


def fetch_industry(code):
    """查询股票所属行业板块（东方财富，多域名降级）。查不到返回 None。

    行业名用于抓取行业级消息——很多利好/利空并不点名公司，
    例如"朱雀三号发射延期"对商业航天板块个股都有影响。
    """
    code = normalize_code(code)
    secid = ("1." if _market_prefix(code) == "sh" else "0.") + code
    # push2 主域名存在间歇性风控，push2delay 更稳定，依次尝试
    hosts = ("push2delay.eastmoney.com", "push2.eastmoney.com")

    for host in hosts:
        try:
            r = requests.get(f"https://{host}/api/qt/stock/get",
                             params={"secid": secid, "fields": "f58,f127"},
                             timeout=12,
                             headers={"User-Agent": _UA,
                                      "Referer": "https://quote.eastmoney.com/"})
            r.raise_for_status()
            data = r.json().get("data") or {}
            industry = (data.get("f127") or "").strip()
            # 去掉申万行业名的罗马数字后缀，如"航天装备Ⅱ" -> "航天装备"
            industry = industry.rstrip("ⅠⅡⅢⅣ").strip()
            if industry:
                return industry
        except Exception as e:  # noqa: BLE001
            logger.debug("查询行业 %s 失败(%s): %s", code, host, e)
    logger.warning("查询行业 %s 全部数据源失败", code)
    return None


# ================= 行情采集 =================
def _kline_tencent(code):
    """腾讯财经前复权日K，返回 [{date,open,close,high,low,volume}]。"""
    symbol = _market_prefix(code) + normalize_code(code)
    params = {"param": f"{symbol},day,,,70,qfq"}
    r = requests.get(_TX_KLINE, params=params, timeout=15,
                     headers={"User-Agent": _UA})
    r.raise_for_status()
    data = r.json()["data"][symbol]
    klines = data.get("qfqday") or data.get("day") or []
    out = []
    for k in klines:
        # [日期, 开, 收, 高, 低, 成交量(手)]
        out.append({
            "date": k[0], "open": _f(k[1]), "close": _f(k[2]),
            "high": _f(k[3]), "low": _f(k[4]),
            "volume": (_f(k[5]) or 0) * 100, "amount": None,
        })
    return out


def _kline_sina(code):
    """新浪财经日K（备用源）。"""
    symbol = _market_prefix(code) + normalize_code(code)
    params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": 70}
    r = requests.get(_SINA_KLINE, params=params, timeout=15,
                     headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    out = []
    for k in r.json():
        out.append({
            "date": k["day"], "open": _f(k["open"]), "close": _f(k["close"]),
            "high": _f(k["high"]), "low": _f(k["low"]),
            "volume": _f(k["volume"]), "amount": None,
        })
    return out


def _kline_akshare(code):
    """akshare（最后备用源）。"""
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=normalize_code(code), period="daily", adjust="qfq")
    out = []
    for _, r in df.tail(70).iterrows():
        out.append({
            "date": str(r["日期"]), "open": _f(r["开盘"]), "close": _f(r["收盘"]),
            "high": _f(r["最高"]), "low": _f(r["最低"]),
            "volume": _f(r["成交量"]), "amount": _f(r["成交额"]),
        })
    return out


def _min30_sina(code, datalen=200):
    """新浪30分钟K线，返回 [{dt,open,high,low,close,volume,amount}]（时间正序）。"""
    symbol = _market_prefix(code) + normalize_code(code)
    r = requests.get(_SINA_MIN_KLINE,
                     params={"symbol": symbol, "scale": 30, "ma": "no",
                             "datalen": datalen}, timeout=15,
                     headers={"User-Agent": _UA,
                              "Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    data = r.json() or []
    out = []
    for k in data:
        out.append({
            "dt": k.get("day"), "open": _f(k.get("open")),
            "high": _f(k.get("high")), "low": _f(k.get("low")),
            "close": _f(k.get("close")), "volume": _f(k.get("volume")),
            "amount": _f(k.get("amount")),
        })
    return [r for r in out if r["dt"] and r["close"] is not None]


def fetch_min30(code, name=""):
    """采集并入库30分钟K线，供30分钟MACD背离判断使用。失败返回0。"""
    code = normalize_code(code)
    rows = _retry(lambda: _min30_sina(code), times=2,
                  desc=f"30分钟K线[新浪] {name}({code})")
    if not rows:
        logger.warning("30分钟K线采集失败: %s(%s)", name, code)
        return 0
    n = db.upsert_min30(code, rows)
    logger.info("30分钟K线入库 %s(%s) 共%d条", name, code, n)
    return n


def fetch_quotes(code, name):
    """按优先级尝试多个行情源，入库并返回最新一条行情。"""
    code = normalize_code(code)
    sources = [
        ("腾讯财经", lambda: _kline_tencent(code)),
        ("新浪财经", lambda: _kline_sina(code)),
        ("akshare", lambda: _kline_akshare(code)),
    ]
    rows = None
    for src_name, fn in sources:
        rows = _retry(fn, times=2, desc=f"行情采集[{src_name}] {name}({code})")
        if rows:
            logger.info("行情采集成功[%s] %s(%s) 共%d条", src_name, name, code, len(rows))
            break
    if not rows:
        logger.error("全部行情源均失败: %s(%s)", name, code)
        return None

    # 计算涨跌幅并入库
    prev_close = None
    latest = None
    for row in rows:
        pct = None
        if prev_close and row["close"] is not None and prev_close != 0:
            pct = round((row["close"] - prev_close) / prev_close * 100, 2)
        row["pct_change"] = pct
        prev_close = row["close"] if row["close"] is not None else prev_close
        db.upsert_quote(code, row["date"], row)
        latest = row

    # 顺带更新30分钟K线（失败不影响日线结果）
    fetch_min30(code, name)
    return latest


# ================= 新闻采集 =================
def _em_news(keyword, limit=None):
    """东方财富资讯搜索（需带 Referer 才能通过风控）。keyword 可为公司名或行业名。"""
    param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default", "pageIndex": 1,
            "pageSize": limit or config.MAX_NEWS_PER_SOURCE,
            "preTag": "", "postTag": "",
        }},
    }
    url = f"{_EM_SEARCH}?cb=jQuery&param=" + quote(json.dumps(param, ensure_ascii=False))
    r = requests.get(url, timeout=15, headers={
        "User-Agent": _UA, "Referer": "https://so.eastmoney.com/",
    })
    r.raise_for_status()
    text = r.text
    start, end = text.find("("), text.rfind(")")
    if start == -1 or end == -1:
        raise ValueError("响应格式异常，非JSONP")
    data = json.loads(text[start + 1:end])

    items = []
    for a in data.get("result", {}).get("cmsArticleWebOld", []) or []:
        title = _clean(a.get("title", ""))
        if not title:
            continue
        items.append({
            "title": title,
            "summary": _clean(a.get("content", ""))[:200],
            "source": a.get("mediaName") or "东方财富",
            "publish_time": (a.get("date") or "")[:19],
            "url": a.get("url") or "",
        })
    return items


def _clean(s):
    """去掉搜索结果的高亮标签与多余空白。"""
    if not s:
        return ""
    for tag in ("<em>", "</em>", "<em class='keyword'>", "&nbsp;"):
        s = s.replace(tag, "")
    return " ".join(str(s).split())


def _em_announce(code, name):
    """东方财富个股公告。"""
    code = normalize_code(code)
    params = {
        "sr": -1, "page_size": 30, "page_index": 1, "ann_type": "A",
        "client_source": "web", "stock_list": code,
    }
    r = requests.get(_EM_ANNOUNCE, params=params, timeout=15,
                     headers={"User-Agent": _UA, "Referer": "https://data.eastmoney.com/"})
    r.raise_for_status()
    items = []
    for a in (r.json().get("data") or {}).get("list") or []:
        title = _clean(a.get("title", ""))
        if not title:
            continue
        art = a.get("art_code")
        items.append({
            "title": title, "summary": "", "source": "交易所公告",
            "publish_time": (a.get("notice_date") or "")[:19],
            "url": f"https://data.eastmoney.com/notices/detail/{code}/{art}.html" if art else "",
        })
    return items


def _dedup_key(title):
    """去重键：取标题前24字并剔除标点，抵御多家媒体转载的标题微调。"""
    t = "".join(ch for ch in str(title) if ch.isalnum())
    return t[:24]


def collect_news(code, name, news_date, industry=None):
    """采集并入库当日新闻（公司消息 + 行业消息，多源合并去重）。

    industry 为行业板块名，用于额外检索行业级消息——这类消息往往不点名公司，
    但同样会影响股价（例如"朱雀三号发射延期"影响商业航天板块）。
    """
    code = normalize_code(code)
    if db.is_news_date_done(code, news_date):
        logger.info("%s(%s) %s 新闻已采集，跳过", name, code, news_date)
        return []

    sources = [
        ("东财资讯", lambda: _em_news(name)),
        ("东财公告", lambda: _em_announce(code, name)),
    ]
    if industry:
        # 行业消息条数少取一些，避免淹没个股自身消息
        sources.append((f"行业[{industry}]",
                        lambda: _em_news(industry, limit=8)))

    all_items = []
    for src_name, fn in sources:
        items = _retry(fn, times=2, desc=f"新闻采集[{src_name}] {name}({code})")
        if items:
            all_items.extend(items)
            logger.info("新闻采集[%s] %s 获得%d条", src_name, name, len(items))

    seen = set()
    added = []
    for it in all_items:
        key = _dedup_key(it["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        if db.news_exists(code, it["title"]):
            continue
        news_id = db.add_news(code, it["title"], it["summary"], it["source"],
                              it["publish_time"], it["url"])
        added.append({**it, "id": news_id})

    db.mark_news_date(code, news_date)
    if not added:
        logger.info("%s(%s) 当日无新增消息", name, code)
    else:
        logger.info("%s(%s) 新增消息 %d 条", name, code, len(added))
    return added


def collect_for_stock(code, name, news_date, industry=None):
    """采集单只持仓的行情与新闻（含行业消息）。"""
    return {
        "code": normalize_code(code), "name": name,
        "quotes": fetch_quotes(code, name),
        "news_added": collect_news(code, name, news_date, industry=industry),
    }