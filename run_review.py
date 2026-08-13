"""每日复盘主流程：采集 -> AI分析 -> 生成报告 -> 存库。

手动触发：
    python run_review.py              # 跑当日（非交易日会跳过）
    python run_review.py --force      # 忽略交易日判断强制运行
    python run_review.py --date 2026-08-07   # 补跑指定日期
"""
import argparse
import logging
import sys
from datetime import date, datetime

import config
import database as db
import collector
import ai_analyzer
import report as report_mod


def setup_logging():
    log_file = config.LOG_DIR / f"review_{date.today().strftime('%Y%m')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


logger = logging.getLogger("main")


def is_trading_day(d):
    """用 akshare 交易日历判断是否交易日；接口失败时按工作日粗判。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = set(str(x) for x in df["trade_date"].astype(str).tolist())
        return d in days
    except Exception as e:  # noqa: BLE001
        logger.warning("交易日历获取失败(%s)，退化为工作日判断", e)
        return datetime.strptime(d, "%Y-%m-%d").weekday() < 5


def run(target_date=None, force=False, account_id=None):
    """执行复盘。account_id 为 None 时对所有账户生成报告。

    行情与新闻按股票代码全局采集一次，多个账户持有同一只股票不会重复抓取，
    也不会重复消耗AI额度。
    """
    setup_logging()
    db.init_db()

    d = target_date or date.today().strftime("%Y-%m-%d")
    if not force and not is_trading_day(d):
        logger.info("%s 非交易日，跳过本次复盘", d)
        return None

    accounts = db.list_accounts()
    if account_id is not None:
        accounts = [a for a in accounts if a["id"] == account_id]
    if not accounts:
        logger.warning("没有账户，请先在网页「账户管理」中创建")
        return None

    # 1) 采集与AI分析：按代码去重，全局只做一次
    all_codes = db.list_all_holding_codes()
    if not all_codes:
        logger.warning("所有账户的持仓均为空，请先在网页「持仓管理」中添加股票")
        return None

    logger.info("=== 开始复盘 %s，涉及 %d 只股票、%d 个账户 ===",
                d, len(all_codes), len(accounts))

    for h in all_codes:
        code, name = h["code"], h["name"]
        industry = h.get("industry")
        # 行业信息缺失时补齐一次，后续复用
        if not industry:
            industry = collector.fetch_industry(code)
            if industry:
                db.set_industry(code, industry)
                logger.info("%s(%s) 所属行业: %s", name, code, industry)
        try:
            logger.info("--- 处理 %s(%s) ---", name, code)
            collector.collect_for_stock(code, name, d, industry=industry)
        except Exception as e:  # noqa: BLE001
            logger.error("采集 %s(%s) 出错，已跳过: %s", name, code, e)

        try:
            ai_analyzer.analyze_code(code, name, d, industry=industry)
        except Exception as e:  # noqa: BLE001
            logger.error("AI分析 %s(%s) 出错，已跳过: %s", name, code, e)

    # 2) 每个账户各生成一份报告
    reports = []
    for acc in accounts:
        if not db.list_holdings(acc["id"]):
            logger.info("账户「%s」无持仓，跳过报告生成", acc["name"])
            continue
        rep = report_mod.generate_and_save(acc["id"], d)
        reports.append(rep)
        logger.info("=== 账户「%s」报告已生成: %s ===", acc["name"], d)
        print("\n" + report_mod.render_text(rep))

    return reports[0] if len(reports) == 1 else reports


def main():
    parser = argparse.ArgumentParser(description=f"{config.APP_NAME} - 每日流程")
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--force", action="store_true", help="忽略交易日判断强制运行")
    parser.add_argument("--account", type=int,
                        help="只跑指定账户ID，默认所有账户")
    args = parser.parse_args()
    run(target_date=args.date, force=args.force, account_id=args.account)


if __name__ == "__main__":
    main()