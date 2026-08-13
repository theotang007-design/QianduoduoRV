"""SQLite 数据层：建表、持仓管理、行情/新闻/分析/报告读写。"""
import sqlite3
import json

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    broker TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id INTEGER NOT NULL DEFAULT 1,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    qty REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    industry TEXT,
    added_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (account_id, code)
);

CREATE TABLE IF NOT EXISTS daily_quotes (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_change REAL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS min30_quotes (
    code TEXT NOT NULL,
    dt TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, dt)
);

CREATE TABLE IF NOT EXISTS news_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    publish_time TEXT,
    url TEXT,
    fetched_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS news_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    relevance TEXT,
    sentiment TEXT,
    category TEXT,
    reason TEXT,
    scope TEXT DEFAULT '公司',
    analyzed_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(news_id)
);

CREATE TABLE IF NOT EXISTS daily_reports (
    account_id INTEGER NOT NULL DEFAULT 1,
    date TEXT NOT NULL,
    content TEXT,
    generated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS news_dates (
    code TEXT NOT NULL,
    news_date TEXT NOT NULL,
    PRIMARY KEY (code, news_date)
);
"""

DEFAULT_ACCOUNT_NAME = "默认账户"


def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # 兼容旧库：holdings 增加行业字段，用于抓取行业级消息
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(holdings)")}
    if "industry" not in cols:
        conn.execute("ALTER TABLE holdings ADD COLUMN industry TEXT")
    # 兼容旧库：分析结果增加影响范围字段（公司/行业）
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(news_analysis)")}
    if "scope" not in acols:
        conn.execute("ALTER TABLE news_analysis ADD COLUMN scope TEXT DEFAULT '公司'")
    conn.commit()
    _migrate_accounts(conn)
    conn.close()


def _migrate_accounts(conn):
    """把单账户结构升级为多账户：确保存在默认账户，旧数据归入该账户。

    holdings/daily_reports 的主键需要加上 account_id，
    SQLite 无法直接改主键，因此重建表并搬迁数据。
    """
    # 1) 确保默认账户存在（固定 id=1，便于旧数据归属）
    row = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO accounts(id, name, broker) VALUES(1, ?, ?)",
                     (DEFAULT_ACCOUNT_NAME, ""))
        conn.commit()
    default_id = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()["id"]

    # 2) holdings：缺少 account_id 说明是旧表，重建后搬迁
    hcols = {r["name"] for r in conn.execute("PRAGMA table_info(holdings)")}
    if "account_id" not in hcols:
        conn.executescript("""
            ALTER TABLE holdings RENAME TO holdings_old;
            CREATE TABLE holdings (
                account_id INTEGER NOT NULL DEFAULT 1,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                qty REAL DEFAULT 0,
                cost REAL DEFAULT 0,
                industry TEXT,
                added_at TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (account_id, code)
            );
        """)
        old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(holdings_old)")}
        ind = "industry" if "industry" in old_cols else "NULL"
        conn.execute(
            f"INSERT INTO holdings(account_id, code, name, qty, cost, industry, added_at) "
            f"SELECT {default_id}, code, name, qty, cost, {ind}, added_at FROM holdings_old"
        )
        conn.execute("DROP TABLE holdings_old")
        conn.commit()

    # 3) daily_reports：同上
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_reports)")}
    if "account_id" not in rcols:
        conn.executescript("""
            ALTER TABLE daily_reports RENAME TO daily_reports_old;
            CREATE TABLE daily_reports (
                account_id INTEGER NOT NULL DEFAULT 1,
                date TEXT NOT NULL,
                content TEXT,
                generated_at TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (account_id, date)
            );
        """)
        conn.execute(
            f"INSERT INTO daily_reports(account_id, date, content, generated_at) "
            f"SELECT {default_id}, date, content, generated_at FROM daily_reports_old"
        )
        conn.execute("DROP TABLE daily_reports_old")
        conn.commit()


# ---------- 账户 ----------
def list_accounts():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_account(account_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_account(name, broker=""):
    """新建账户，返回新账户id。名称重复时抛 sqlite3.IntegrityError。"""
    conn = get_conn()
    cur = conn.execute("INSERT INTO accounts(name, broker) VALUES(?,?)",
                       (name.strip(), (broker or "").strip()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def rename_account(account_id, name, broker=None):
    conn = get_conn()
    if broker is None:
        conn.execute("UPDATE accounts SET name=? WHERE id=?", (name.strip(), account_id))
    else:
        conn.execute("UPDATE accounts SET name=?, broker=? WHERE id=?",
                     (name.strip(), broker.strip(), account_id))
    conn.commit()
    conn.close()


def delete_account(account_id):
    """删除账户及其持仓与报告。至少保留一个账户。"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if total <= 1:
        conn.close()
        return False
    conn.execute("DELETE FROM holdings WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM daily_reports WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()
    conn.close()
    return True


def first_account_id():
    conn = get_conn()
    row = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row["id"] if row else None


# ---------- 持仓 ----------
def list_holdings(account_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM holdings WHERE account_id=? ORDER BY added_at", (account_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_holding_codes():
    """所有账户涉及的股票代码去重列表。行情与新闻按代码全局共享，避免重复采集。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT code, MAX(name) AS name, MAX(industry) AS industry "
        "FROM holdings GROUP BY code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_holding(account_id, code, name, qty, cost, industry=None):
    """新增或更新持仓。industry 为 None 时保留原值。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO holdings(account_id,code,name,qty,cost,industry) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(account_id,code) DO UPDATE SET name=excluded.name, qty=excluded.qty, "
        "cost=excluded.cost, industry=COALESCE(excluded.industry, holdings.industry)",
        (account_id, code, name, qty, cost, industry),
    )
    conn.commit()
    conn.close()


def set_industry(code, industry):
    """行业信息与账户无关，按代码统一更新。"""
    conn = get_conn()
    conn.execute("UPDATE holdings SET industry=? WHERE code=?", (industry, code))
    conn.commit()
    conn.close()


def delete_holding(account_id, code):
    conn = get_conn()
    conn.execute("DELETE FROM holdings WHERE account_id=? AND code=?", (account_id, code))
    conn.commit()
    conn.close()


def update_position(account_id, code, qty, cost):
    """仅更新持仓数量与成本价（买卖后调仓用），不改动代码与名称。

    返回是否命中了已有记录。
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE holdings SET qty=?, cost=? WHERE account_id=? AND code=?",
        (qty, cost, account_id, code),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


# ---------- 行情 ----------
def upsert_quote(code, d, row):
    conn = get_conn()
    conn.execute(
        "INSERT INTO daily_quotes(code,date,open,high,low,close,volume,amount,pct_change) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET open=excluded.open,high=excluded.high,"
        "low=excluded.low,close=excluded.close,volume=excluded.volume,"
        "amount=excluded.amount,pct_change=excluded.pct_change",
        (
            code, d,
            row.get("open"), row.get("high"), row.get("low"), row.get("close"),
            row.get("volume"), row.get("amount"), row.get("pct_change"),
        ),
    )
    conn.commit()
    conn.close()


def get_quotes(code, limit=60):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM daily_quotes WHERE code=? ORDER BY date DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


def upsert_min30(code, rows):
    """批量写入30分钟K线。rows: [{dt,open,high,low,close,volume,amount}]"""
    if not rows:
        return 0
    conn = get_conn()
    conn.executemany(
        "INSERT INTO min30_quotes(code,dt,open,high,low,close,volume,amount) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code,dt) DO UPDATE SET open=excluded.open,high=excluded.high,"
        "low=excluded.low,close=excluded.close,volume=excluded.volume,"
        "amount=excluded.amount",
        [(code, r.get("dt"), r.get("open"), r.get("high"), r.get("low"),
          r.get("close"), r.get("volume"), r.get("amount")) for r in rows],
    )
    conn.commit()
    conn.close()
    return len(rows)


def get_min30(code, limit=240):
    """取最近的30分钟K线，按时间正序返回。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM min30_quotes WHERE code=? ORDER BY dt DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


# ---------- 新闻 ----------
def add_news(code, title, summary, source, publish_time, url):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO news_raw(code,title,summary,source,publish_time,url) VALUES(?,?,?,?,?,?)",
        (code, title, summary, source, publish_time, url),
    )
    conn.commit()
    news_id = cur.lastrowid
    conn.close()
    return news_id


def news_exists(code, title):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM news_raw WHERE code=? AND title=?", (code, title)
    ).fetchone()
    conn.close()
    return row is not None


def get_news(code=None, since=None):
    conn = get_conn()
    sql = "SELECT * FROM news_raw WHERE 1=1"
    args = []
    if code:
        sql += " AND code=?"
        args.append(code)
    if since:
        sql += " AND date(substr(publish_time,1,10))>=?"
        args.append(since)
    sql += " ORDER BY publish_time DESC, id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_news_date(code, news_date):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO news_dates(code,news_date) VALUES(?,?)",
        (code, news_date),
    )
    conn.commit()
    conn.close()


def is_news_date_done(code, news_date):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM news_dates WHERE code=? AND news_date=?", (code, news_date)
    ).fetchone()
    conn.close()
    return row is not None


# ---------- 分析 ----------
def add_analysis(news_id, code, relevance, sentiment, category, reason, scope="公司"):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO news_analysis"
        "(news_id,code,relevance,sentiment,category,reason,scope) "
        "VALUES(?,?,?,?,?,?,?)",
        (news_id, code, relevance, sentiment, category, reason, scope),
    )
    conn.commit()
    conn.close()


def get_analysis_for_code(code, since=None):
    """查询个股已分析且相关的消息（排除"无关"），用于个股详情页。"""
    conn = get_conn()
    sql = (
        "SELECT n.*, a.relevance, a.sentiment, a.category, a.reason, a.scope "
        "FROM news_raw n LEFT JOIN news_analysis a ON n.id=a.news_id "
        "WHERE n.code=? AND (a.relevance IS NULL OR a.relevance != '无关') "
    )
    args = [code]
    if since:
        sql += " AND date(COALESCE(NULLIF(substr(n.publish_time,1,10),''), substr(n.fetched_at,1,10)))>=?"
        args.append(since)
    sql += " ORDER BY n.publish_time DESC, n.id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_news_for_date(code, day):
    """查询个股在指定日期的消息及AI分析结果（按发布日期，缺失则按抓取日期）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT n.*, a.relevance, a.sentiment, a.category, a.reason, a.scope "
        "FROM news_raw n LEFT JOIN news_analysis a ON n.id=a.news_id "
        "WHERE n.code=? AND date(COALESCE(NULLIF(substr(n.publish_time,1,10),''), "
        "substr(n.fetched_at,1,10)))=? "
        "ORDER BY n.publish_time DESC, n.id DESC",
        (code, day),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unanalyzed_news(code, day=None, limit=None):
    """查询尚未进行AI分析的消息，可限定日期与条数（用于控制API成本）。"""
    conn = get_conn()
    sql = (
        "SELECT n.* FROM news_raw n LEFT JOIN news_analysis a ON n.id=a.news_id "
        "WHERE n.code=? AND a.news_id IS NULL "
    )
    args = [code]
    if day:
        sql += (" AND date(COALESCE(NULLIF(substr(n.publish_time,1,10),''), "
                "substr(n.fetched_at,1,10)))=?")
        args.append(day)
    sql += " ORDER BY n.publish_time DESC, n.id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 报告 ----------
def save_report(account_id, d, content):
    conn = get_conn()
    conn.execute(
        "INSERT INTO daily_reports(account_id,date,content) VALUES(?,?,?) "
        "ON CONFLICT(account_id,date) DO UPDATE SET content=excluded.content, "
        "generated_at=datetime('now','localtime')",
        (account_id, d, json.dumps(content, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_report(account_id, d):
    conn = get_conn()
    row = conn.execute("SELECT * FROM daily_reports WHERE account_id=? AND date=?",
                       (account_id, d)).fetchone()
    conn.close()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["content"] = json.loads(rec["content"])
    except Exception:
        pass
    return rec


def list_report_dates(account_id, limit=90):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, generated_at FROM daily_reports WHERE account_id=? "
        "ORDER BY date DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_report_date(account_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT date FROM daily_reports WHERE account_id=? ORDER BY date DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    conn.close()
    return row["date"] if row else None