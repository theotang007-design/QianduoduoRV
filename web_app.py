"""本地Web服务：首页(今日报告)/历史/个股详情/持仓管理/账户管理。"""
import logging
import mimetypes
import sqlite3
import threading
from datetime import date, timedelta

from flask import (Flask, render_template, request, redirect, url_for, jsonify,
                   session)

import config
import database as db
import technical
import signals
import collector
import ai_analyzer

# Windows 注册表里 .svg 常被写成 image/svg，浏览器不认，这里强制修正
mimetypes.add_type("image/svg+xml", ".svg")

app = Flask(__name__)
# 仅用于本机 session（记住当前选中的账户），非安全敏感场景
app.secret_key = "qianduoduo-local-session"
logger = logging.getLogger("web")

db.init_db()

# 手动复盘任务状态（单机单用户，内存态足够）
_run_lock = threading.Lock()
_run_state = {"running": False, "message": "", "date": None, "error": None}

# 行情刷新任务状态
_quote_lock = threading.Lock()
_quote_state = {"running": False, "done": 0, "total": 0, "failed": [], "message": ""}


def current_account_id():
    """当前选中的账户ID。失效或未选时回落到第一个账户。"""
    acc_id = session.get("account_id")
    if acc_id and db.get_account(acc_id):
        return acc_id
    acc_id = db.first_account_id()
    session["account_id"] = acc_id
    return acc_id


@app.context_processor
def inject_globals():
    accounts = db.list_accounts()
    acc_id = current_account_id()
    return {
        "risk_notice": config.RISK_NOTICE,
        "app_name": config.APP_NAME,
        "accounts": accounts,
        "current_account_id": acc_id,
        "current_account": next((a for a in accounts if a["id"] == acc_id), None),
    }


@app.route("/switch-account/<int:account_id>")
def switch_account(account_id):
    """切换当前账户后回到来源页面。"""
    if db.get_account(account_id):
        session["account_id"] = account_id
    return redirect(request.referrer or url_for("index"))


@app.route("/")
def index():
    acc_id = current_account_id()
    d = db.latest_report_date(acc_id)
    if not d:
        return render_template("empty.html", nav="today")
    return redirect(url_for("report_view", d=d))


@app.route("/report/<d>")
def report_view(d):
    acc_id = current_account_id()
    rec = db.get_report(acc_id, d)
    if not rec:
        return render_template("empty.html", date=d, nav="today")
    return render_template("report.html", report=rec["content"],
                           generated_at=rec["generated_at"],
                           dates=db.list_report_dates(acc_id), nav="today")


@app.route("/history")
def history():
    return render_template("history.html",
                           dates=db.list_report_dates(current_account_id(), limit=365),
                           nav="history")


@app.route("/stock/<code>")
def stock_detail(code):
    holdings = {h["code"]: h for h in db.list_holdings(current_account_id())}
    holding = holdings.get(code, {})
    name = holding.get("name", code)
    quotes = db.get_quotes(code, limit=60)
    tech = technical.build_full_tech(code)
    since = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    news = db.get_analysis_for_code(code, since=since)
    return render_template("stock.html", code=code, name=name, quotes=quotes,
                           tech=tech, news=news,
                           industry=holding.get("industry"),
                           sig=signals.build_signal(code),
                           tech_summary=technical.build_summary(code),
                           tech_commentary=technical.build_commentary(code),
                           tech_plain=technical.build_plain(code), nav="stock")


@app.route("/holdings", methods=["GET", "POST"])
def holdings_page():
    acc_id = current_account_id()
    if request.method == "POST":
        action = request.form.get("action")
        code = (request.form.get("code") or "").strip()
        if action == "delete" and code:
            db.delete_holding(acc_id, code)
        elif action == "update" and code:
            # 买卖后调仓：只改数量与成本价
            db.update_position(acc_id, code, _num(request.form.get("qty")),
                               _num(request.form.get("cost")))
        elif action == "save" and code:
            code = collector.normalize_code(code)
            name = (request.form.get("name") or "").strip()
            db.upsert_holding(acc_id, code, name or code, _num(request.form.get("qty")),
                              _num(request.form.get("cost")))
            # 新增持仓后立即补齐该股行情与消息，避免"最新价/盈亏"长期为空
            _bootstrap_stock(acc_id, code, name)
        return redirect(url_for("holdings_page"))

    return render_template("holdings.html", holdings=_holdings_with_profit(acc_id),
                           nav="holdings")


def _bootstrap_stock(account_id, code, name=""):
    """为新增持仓补齐数据：拉取行情、回填真实股票名称、后台采集当日消息。

    行情同步执行（很快，且页面立刻要用最新价）；
    消息采集与AI分析耗时较长，放后台线程，避免阻塞表单提交。
    """
    industry = None
    try:
        real_name = collector.fetch_stock_name(code)
        if real_name:
            # 名称以交易所数据为准，避免手填错误影响新闻检索
            holding = next((h for h in db.list_holdings(account_id)
                            if h["code"] == code), None)
            if holding and real_name != holding["name"]:
                db.upsert_holding(account_id, code, real_name,
                                  holding["qty"], holding["cost"])
            name = real_name
        industry = collector.fetch_industry(code)
        if industry:
            db.set_industry(code, industry)
        collector.fetch_quotes(code, name or code)
    except Exception as e:  # noqa: BLE001
        logger.error("新增持仓 %s 行情初始化失败: %s", code, e)

    def _task():
        try:
            day = date.today().strftime("%Y-%m-%d")
            collector.collect_news(code, name or code, day, industry=industry)
            ai_analyzer.analyze_code(code, name or code, day, industry=industry)
        except Exception as e:  # noqa: BLE001
            logger.error("新增持仓 %s 消息采集失败: %s", code, e)

    threading.Thread(target=_task, daemon=True).start()


def _holdings_with_profit(account_id):
    """附加最新价、持仓市值、仓位占比与浮动盈亏，按市值从大到小排列。"""
    rows = []
    for h in db.list_holdings(account_id):
        quotes = db.get_quotes(h["code"], limit=1)
        last_close = quotes[-1]["close"] if quotes else None
        qty, cost = h.get("qty") or 0, h.get("cost") or 0

        market_value = last_close * qty if last_close and qty > 0 else None
        profit = profit_pct = None
        if last_close and qty > 0 and cost > 0:
            profit = (last_close - cost) * qty
            profit_pct = (last_close - cost) / cost * 100

        rows.append({**h, "last_close": last_close, "market_value": market_value,
                     "profit": profit, "profit_pct": profit_pct})

    # 仓位占比以总市值为分母
    total_mv = sum(r["market_value"] for r in rows if r["market_value"])
    for r in rows:
        r["weight"] = (r["market_value"] / total_mv * 100) if (total_mv and r["market_value"]) else None

    # 市值降序；无市值的（未采集行情）排在最后
    rows.sort(key=lambda r: r["market_value"] or -1, reverse=True)
    return rows


def _num(v):
    """表单数值解析，空值或非法输入按0处理，避免提交异常。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@app.route("/api/refresh-quotes", methods=["POST"])
def api_refresh_quotes():
    """一键刷新全部持仓的最新行情（后台执行，前端轮询进度）。"""
    holdings = db.list_holdings(current_account_id())
    if not holdings:
        return jsonify({"started": False, "reason": "当前没有持仓"}), 400

    with _quote_lock:
        if _quote_state["running"]:
            return jsonify({"started": False, "reason": "行情刷新正在进行中"}), 409
        _quote_state.update(running=True, done=0, total=len(holdings),
                            failed=[], message="正在刷新行情…")

    def _task():
        failed = []
        for i, h in enumerate(holdings, 1):
            try:
                if not collector.fetch_quotes(h["code"], h["name"]):
                    failed.append(h["name"])
            except Exception as e:  # noqa: BLE001
                logger.error("刷新 %s(%s) 行情失败: %s", h["name"], h["code"], e)
                failed.append(h["name"])
            with _quote_lock:
                _quote_state.update(done=i, failed=list(failed))
        with _quote_lock:
            ok = len(holdings) - len(failed)
            _quote_state.update(
                running=False,
                message=(f"已更新 {ok} 只" + (f"，{len(failed)} 只失败" if failed else "")),
            )
        logger.info("行情刷新完成：成功 %d，失败 %d", len(holdings) - len(failed), len(failed))

    threading.Thread(target=_task, daemon=True).start()
    return jsonify({"started": True, "total": len(holdings)})


@app.route("/api/refresh-status")
def api_refresh_status():
    with _quote_lock:
        return jsonify(dict(_quote_state))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    """AI 服务设置：API Key 等敏感信息仅写入本地 .env，页面不回显明文。"""
    saved = False
    if request.method == "POST":
        config.save_ai_settings({
            "AI_PROVIDER": request.form.get("ai_provider"),
            "DEEPSEEK_API_KEY": request.form.get("deepseek_key"),
            "DEEPSEEK_MODEL": request.form.get("deepseek_model"),
            "VOLC_API_KEY": request.form.get("volc_key"),
            "VOLC_MODEL": request.form.get("volc_model"),
            "MAX_NEWS_PER_STOCK_PER_DAY": request.form.get("max_news"),
        })
        logger.info("AI 设置已更新，当前服务商: %s", config.AI_PROVIDER)
        saved = True
    else:
        # 每次进入页面重新读取，反映手工编辑 .env 或删除文件的情况
        config.reload_settings()

    return render_template(
        "settings.html", nav="settings", saved=saved,
        provider=config.AI_PROVIDER,
        deepseek_masked=config.mask_key(config.DEEPSEEK_API_KEY),
        deepseek_model=config.DEEPSEEK_MODEL,
        volc_masked=config.mask_key(config.VOLC_API_KEY),
        volc_model=config.VOLC_MODEL,
        max_news=config.MAX_NEWS_PER_STOCK_PER_DAY,
        ai_ready=ai_analyzer.api_key_ready(),
        pending_news=_count_pending_news(),
    )


@app.route("/api/test-ai", methods=["POST"])
def api_test_ai():
    """用一条示例新闻实测AI连通性，便于用户确认Key是否有效。"""
    ok, detail = ai_analyzer.test_connection()
    return jsonify({"ok": ok, "detail": detail,
                    "provider": config.AI_PROVIDER})


def _count_pending_news():
    """统计已抓取但未完成AI分析的消息总数。"""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM news_raw n "
        "LEFT JOIN news_analysis a ON n.id=a.news_id WHERE a.news_id IS NULL"
    ).fetchone()
    conn.close()
    return row[0] if row else 0


@app.route("/api/kline/<code>")
def api_kline(code):
    quotes = db.get_quotes(code, limit=60)
    closes = [q["close"] for q in quotes if q["close"] is not None]

    def ma(n, i):
        if i + 1 < n:
            return None
        return round(sum(closes[i + 1 - n:i + 1]) / n, 2)

    data = {
        "dates": [q["date"] for q in quotes],
        "kline": [[q["open"], q["close"], q["low"], q["high"]] for q in quotes],
        "volume": [q["volume"] for q in quotes],
        "ma5": [ma(5, i) for i in range(len(quotes))],
        "ma10": [ma(10, i) for i in range(len(quotes))],
        "ma20": [ma(20, i) for i in range(len(quotes))],
    }
    return jsonify(data)


@app.route("/run", methods=["POST"])
def run_now():
    """手动触发一次复盘（后台线程执行，避免请求超时）。

    对所有账户生成报告：行情与新闻按代码采集一次，多账户不重复消耗AI额度。
    """
    d = request.form.get("date") or None

    with _run_lock:
        if _run_state["running"]:
            return jsonify({"started": False, "reason": "已有复盘任务在运行"}), 409
        _run_state.update(running=True, message="正在采集行情与消息…", date=d, error=None)

    def _task():
        try:
            import run_review
            run_review.run(target_date=d, force=True)
            with _run_lock:
                _run_state.update(message="复盘完成")
        except Exception as e:  # noqa: BLE001
            logger.error("手动触发复盘失败: %s", e)
            with _run_lock:
                _run_state.update(message="复盘失败", error=str(e))
        finally:
            with _run_lock:
                _run_state["running"] = False

    threading.Thread(target=_task, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/run-status")
def api_run_status():
    """供前端轮询复盘任务状态，驱动按钮运行态与完成后自动刷新。"""
    with _run_lock:
        state = dict(_run_state)
    state["latest_report"] = db.latest_report_date(current_account_id())
    return jsonify(state)


@app.route("/accounts", methods=["GET", "POST"])
def accounts_page():
    """账户管理：新建、重命名、删除，并可切换当前账户。"""
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        name = (request.form.get("name") or "").strip()
        broker = (request.form.get("broker") or "").strip()
        acc_id = request.form.get("account_id", type=int)

        if action == "add":
            if not name:
                error = "账户名称不能为空"
            else:
                try:
                    new_id = db.add_account(name, broker)
                    session["account_id"] = new_id      # 新建后自动切换过去
                except sqlite3.IntegrityError:
                    error = f"账户名称「{name}」已存在"
        elif action == "rename" and acc_id:
            if not name:
                error = "账户名称不能为空"
            else:
                try:
                    db.rename_account(acc_id, name, broker)
                except sqlite3.IntegrityError:
                    error = f"账户名称「{name}」已存在"
        elif action == "delete" and acc_id:
            if not db.delete_account(acc_id):
                error = "至少需要保留一个账户"
            elif session.get("account_id") == acc_id:
                session["account_id"] = db.first_account_id()

        if not error:
            return redirect(url_for("accounts_page"))

    # 附带每个账户的持仓数与市值，便于识别
    rows = []
    for a in db.list_accounts():
        holdings = _holdings_with_profit(a["id"])
        mv = sum(h["market_value"] for h in holdings if h["market_value"])
        rows.append({**a, "count": len(holdings), "market_value": mv})

    return render_template("accounts.html", accounts_info=rows,
                           error=error, nav="accounts")


if __name__ == "__main__":
    print(f"{config.APP_NAME} 已启动: http://{config.WEB_HOST}:{config.WEB_PORT}")
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)