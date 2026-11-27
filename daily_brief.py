import os
import re
import math
import json
import html
import time
import argparse
import calendar
import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
import akshare as ak
import yfinance as yf
import feedparser
from openai import OpenAI

# ================= 配置区（自动读取 GitHub Secrets） =================
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}

DOCS_DIR = Path("docs")
ARCHIVE_DIR = DOCS_DIR / "archive"
CARD_CACHE_FILE = Path("latest_feishu_card.json")

# ============ 信源配置（全部免费、无需 Key，GitHub Actions 海外网络直连） ============
# 一、官方一手源：政策与数据的第一发布方，权威性最高、时效最快（先于任何媒体报道）
OFFICIAL_RSS = {
    "美联储": [
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ],
    "美国SEC": [
        "https://www.sec.gov/news/pressreleases.rss",
    ],
    "欧洲央行": [
        "https://www.ecb.europa.eu/rss/press.html",
    ],
}

# 二、全球顶级财经媒体（直连官方 RSS，不经过聚合器，时效与原文一致）
GLOBAL_FINANCE_RSS = {
    "彭博市场": [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://news.google.com/rss/search?q=when:24h+site:bloomberg.com/news/articles&hl=en-US&gl=US&ceid=US:en",
    ],
    "彭博宏观": [
        "https://feeds.bloomberg.com/economics/news.rss",
    ],
    "金融时报": [
        "https://www.ft.com/rss/home",
        "https://www.ft.com/markets?format=rss",
    ],
    "华尔街日报": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    ],
    "道琼斯MarketWatch": [
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    ],
    "CNBC经济": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    ],
    "CNBC市场": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    ],
    "CNBC世界": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    ],
    "Investing商品": [
        "https://www.investing.com/rss/news_11.rss",
    ],
    "Investing经济": [
        "https://www.investing.com/rss/news_14.rss",
    ],
    "路透商业": [
        "https://news.google.com/rss/search?q=site:reuters.com/business+when:1d&hl=en-US&gl=US&ceid=US:en",
    ],
}

# 三、国际时政与前沿科技
GENERAL_NEWS_RSS = {
    "彭博科技": [
        "https://feeds.bloomberg.com/technology/news.rss",
    ],
    "CNBC科技": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    ],
    "日经亚洲": [
        "https://asia.nikkei.com/rss/feed/nar",
    ],
    "南华早报": [
        "https://www.scmp.com/rss/92/feed",
    ],
    "半岛电视台": [
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "BBC中文": [
        "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    ],
    "路透国际": [
        "https://news.google.com/rss/search?q=site:reuters.com/world+when:1d&hl=en-US&gl=US&ceid=US:en",
    ],
    "谷歌新闻中文财经": [
        "https://news.google.com/rss/search?q=when:1d+%E7%BB%8F%E6%B5%8E+OR+%E8%82%A1%E5%B8%82+OR+%E5%A4%AE%E8%A1%8C&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    ],
}
# ====================================================================


def resolve_edition(edition_arg, now):
    """按北京时间自动识别版本：00-11点早报(am)，12-18点收盘报(close)，19-23点晚报(pm)"""
    if edition_arg in ("am", "close", "pm"):
        return edition_arg
    if now.hour < 12:
        return "am"
    if now.hour < 19:
        return "close"
    return "pm"


def get_edition_meta(edition):
    if edition == "close":
        return {
            "code": "close",
            "name": "收盘报",
            "full_name": "全球财经与A股收盘报",
            "icon": "🌇",
            "badge_text": "🌇 收盘版 · A股全天复盘与日间催化",
            "rss_hours": 10,
            "cls_limit": 65,
            "us_card_title": "🇺🇸 美股上日收盘",
            "a_card_title": "🇨🇳 A 股今日收盘",
            "feishu_neutral_color": "orange",
        }
    if edition == "pm":
        return {
            "code": "pm",
            "name": "晚报",
            "full_name": "全球财经与美股晚报",
            "icon": "🌙",
            "badge_text": "🌙 晚报版 · 欧盘动向与美股前瞻",
            "rss_hours": 10,
            "cls_limit": 50,
            "us_card_title": "🇺🇸 美股最新 / 盘面",
            "a_card_title": "🇨🇳 A 股今日收盘",
            "feishu_neutral_color": "indigo",
        }
    return {
        "code": "am",
        "name": "早报",
        "full_name": "全球财经与热点早报",
        "icon": "☕",
        "badge_text": "☀️ 早报版 · 隔夜美股与盘前催化",
        "rss_hours": 16,
        "cls_limit": 55,
        "us_card_title": "🇺🇸 隔夜美股指数",
        "a_card_title": "🇨🇳 A 股上日收盘",
        "feishu_neutral_color": "blue",
    }


def get_site_url():
    """自动推算 GitHub Pages 网址（用户名自动转小写，避免 404）"""
    custom = os.environ.get("SITE_URL", "").strip()
    if custom:
        return custom.rstrip("/") + "/"
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in gh_repo:
        owner, repo = gh_repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{repo}/"
    return ""


def clean_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def esc(text):
    return html.escape(str(text or ""), quote=True)


def safe_score(val):
    try:
        s = int(round(float(val)))
        return max(1, min(5, s))
    except Exception:
        return 3


def archive_sort_key(entry_id):
    """确保同一天严格按 pm(3) > close(2) > am(1) 倒序排列"""
    if entry_id.endswith("-pm"):
        return (entry_id[:-3], 3)
    if entry_id.endswith("-close"):
        return (entry_id[:-6], 2)
    if entry_id.endswith("-am"):
        return (entry_id[:-3], 1)
    return (entry_id, 0)


def format_archive_label(entry_id):
    """压缩成 11-27 ☀️早报，保证手机端下拉框不被截断"""
    short = entry_id[5:10] if len(entry_id) >= 10 else entry_id
    if entry_id.endswith("-am"):
        return f"{short} ☀️早报"
    if entry_id.endswith("-close"):
        return f"{short} 🌇收盘"
    if entry_id.endswith("-pm"):
        return f"{short} 🌙晚报"
    return short


def _safe_float(value):
    """把任意输入转成有限浮点数，失败返回 None（所有行情数值统一在这里清洗）"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _pct_limit(symbol):
    """按标的类型给出单日涨跌幅合理上限，用于拦截脏数据"""
    if symbol.startswith("^"):
        return 12.0
    if symbol.endswith("-USD"):
        return 40.0
    if symbol.endswith("=F") or symbol.endswith("=X"):
        return 15.0
    return 25.0


def _build_quote(name, symbol, price, prev, history, as_of="", source="Yahoo"):
    """统一封装行情并做合理性护栏：价格非正、涨跌幅离谱的数据一律不入库"""
    price, prev = _safe_float(price), _safe_float(prev)
    if not price or not prev or price <= 0 or prev <= 0:
        return None
    pct = (price - prev) / prev * 100
    if not math.isfinite(pct):
        return None
    if abs(pct) > _pct_limit(symbol):
        print(f"[行情] {name}({symbol}) 涨跌幅 {pct:+.2f}% 超出合理区间，判定为脏数据并丢弃")
        return None
    hist = [v for v in (_safe_float(x) for x in (history or [])) if v]
    return {
        "name": name,
        "symbol": symbol,
        "price": f"{price:,.2f}",
        "pct": pct,
        "history": hist[-7:],
        "as_of": as_of,
        "source": source,
    }


def _yf_quote(name, symbol):
    """Yahoo 行情：日线收盘算 7 日走势，实时价与日线交叉校验后才决定口径。

    旧实现直接拿 closes[-1]/closes[-2] 相除，一是无法发现「数据停在几天前」的陈旧行情，
    二是 fast_info 兜底时 lastPrice 与 previousClose 可能来自不同交易日，导致涨跌幅算错。
    这里改成：先判断日线最后一根是不是「今天」，再决定昨收取哪一根，保证口径统一为「当前价 / 上一交易日收盘」。
    """
    closes, last_date, last_tz = [], None, None
    live, live_prev = None, None
    try:
        ticker = yf.Ticker(symbol)
    except Exception as e:
        print(f"[行情] {name}({symbol}) 初始化失败: {e}")
        return None

    try:
        hist = ticker.history(period="1mo", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty and "Close" in hist.columns:
            for ts, val in hist["Close"].items():
                num = _safe_float(val)
                if num and num > 0:
                    closes.append(num)
                    day = getattr(ts, "date", lambda: None)()
                    if day is not None:
                        last_date = day
                        last_tz = getattr(ts, "tzinfo", None)
    except Exception as e:
        print(f"[行情] {name}({symbol}) 日线获取失败: {e}")

    try:
        fast = ticker.fast_info
        live = _safe_float(fast.get("lastPrice") or fast.get("last_price"))
        live_prev = _safe_float(fast.get("previousClose") or fast.get("previous_close"))
    except Exception:
        live, live_prev = None, None

    price, prev, hist_vals = None, None, closes[:]
    as_of = f"{last_date} 收盘" if last_date else ""

    if len(closes) >= 2:
        # 先按「日线最后一根是否属于今天」判断口径：
        #   今天这根已存在 → 昨收取前一根，实时价覆盖今天这根；
        #   今天还没成交   → 沿用日线（上日收盘）口径，避免算成 0%。
        today_ex = None
        if last_tz is not None and last_date:
            try:
                today_ex = datetime.datetime.now(datetime.timezone.utc).astimezone(last_tz).date()
            except Exception:
                today_ex = None
        has_today_bar = today_ex is not None and last_date >= today_ex

        if live and live_prev:
            if abs(live - closes[-1]) <= closes[-1] * 1e-4:
                price, prev = closes[-1], closes[-2]
            elif has_today_bar:
                price, prev = live, closes[-2]
                hist_vals = closes[:-1] + [live]
                as_of = datetime.datetime.now(BJ_TZ).strftime("%m-%d %H:%M 实时")
            else:
                price, prev = live, closes[-1]
                hist_vals = closes + [live]
                as_of = datetime.datetime.now(BJ_TZ).strftime("%m-%d %H:%M 实时")
        else:
            price, prev = closes[-1], closes[-2]
    elif live and live_prev:
        price, prev = live, live_prev
        hist_vals = [live_prev, live]
        as_of = datetime.datetime.now(BJ_TZ).strftime("%m-%d %H:%M 实时")

    if price is None:
        return None
    if last_date:
        stale_days = (datetime.datetime.now(BJ_TZ).date() - last_date).days
        if stale_days > 5:
            print(f"[行情] {name}({symbol}) 数据最后更新于 {last_date}，已陈旧 {stale_days} 天")
    return _build_quote(name, symbol, price, prev, hist_vals, as_of)


def fetch_yf_group(tickers_dict, workers=8):
    """并发获取 Yahoo 行情：标的变多后串行太慢，单只失败仍不影响其余标的"""
    items = list(tickers_dict.items())

    def _work(kv):
        name, symbol = kv
        try:
            return _yf_quote(name, symbol)
        except Exception as e:
            print(f"[行情] {name}({symbol}) 异常: {type(e).__name__}: {str(e)[:60]}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        quotes = list(pool.map(_work, items))

    results = []
    for (name, symbol), quote in zip(items, quotes):
        if quote:
            results.append(quote)
        else:
            print(f"[行情] {name}({symbol}) 未获取到有效数值")
    return results


# A 股/港股核心指数：(显示名, 国内源代码, Yahoo 代码, 快照别名)
# 前 4 个会额外拉日线画走势线，其余只出数值，避免请求数暴涨
A_INDEX_TARGETS = [
    ("上证指数", "sh000001", "000001.SS", ["上证指数", "上证综指"]),
    ("深证成指", "sz399001", "399001.SZ", ["深证成指"]),
    ("创业板指", "sz399006", "399006.SZ", ["创业板指"]),
    ("科创50", "sh000688", "000688.SS", ["科创50", "科创板50"]),
    ("沪深300", "sh000300", "000300.SS", ["沪深300"]),
    ("中证500", "sh000905", "000905.SS", ["中证500"]),
    ("中证1000", "sh000852", "000852.SS", ["中证1000"]),
    ("上证50", "sh000016", "000016.SS", ["上证50"]),
    ("北证50", "bj899050", "", ["北证50"]),
    ("恒生指数", "hkHSI", "^HSI", ["恒生指数"]),
    ("恒生科技", "hkHSTECH", "^HSTECH", ["恒生科技指数", "恒生科技"]),
]

# 美股核心指数
US_INDEX_TARGETS = {
    "纳斯达克": "^IXIC",
    "标普500": "^GSPC",
    "道琼斯": "^DJI",
    "费城半导体": "^SOX",
    "纳斯达克100": "^NDX",
    "罗素2000": "^RUT",
    "VIX恐慌": "^VIX",
}

# 美股重点个股（七巨头 + AI 算力链 + 高关注度标的）
US_STOCK_TARGETS = {
    "英伟达": "NVDA",
    "苹果": "AAPL",
    "微软": "MSFT",
    "特斯拉": "TSLA",
    "谷歌": "GOOGL",
    "亚马逊": "AMZN",
    "Meta": "META",
    "博通": "AVGO",
    "台积电": "TSM",
    "AMD": "AMD",
    "美光": "MU",
    "Palantir": "PLTR",
}

# 大宗 · 加密 · 汇率 · 利率
MACRO_TARGETS = {
    "黄金": "GC=F",
    "WTI原油": "CL=F",
    "布伦特原油": "BZ=F",
    "白银": "SI=F",
    "铜": "HG=F",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "离岸人民币": "CNH=X",
    "美元指数": "DX-Y.NYB",
    "美债10Y": "^TNX",
}


def _a_as_of(last_day=""):
    """A 股行情时点：盘中标实时，休市用日线的真实交易日标注，避免标题重复出现「收盘」"""
    now = datetime.datetime.now(BJ_TZ)
    trading = now.weekday() < 5 and (9, 25) <= (now.hour, now.minute) <= (15, 5)
    if trading:
        return now.strftime("%m-%d %H:%M 实时")
    return f"{last_day} 收盘" if last_day else "收盘"


# 指数快照候选源：东财数据最好但经常对云端 IP 限流，新浪作为第二主力
_INDEX_SNAPSHOT_CALLS = (
    ("东财", "index_zh_spot_em", {"symbol": "沪深重要指数"}),
    ("东财", "stock_zh_index_spot_em", {}),
    ("新浪", "index_zh_spot_sina", {}),
    ("新浪", "stock_zh_index_spot_sina", {}),
)


def _load_index_frames(max_sources=2):
    """按可靠性顺序加载指数快照，最多取两个源用于交叉校验"""
    frames = []
    for source, fname, kwargs in _INDEX_SNAPSHOT_CALLS:
        fn = getattr(ak, fname, None)
        if fn is None:
            continue
        try:
            df = fn(**kwargs)
        except Exception as e:
            print(f"[行情] {source} {fname} 不可用: {type(e).__name__}")
            continue
        if df is None or df.empty:
            continue
        frames.append((source, df))
        print(f"[行情] 指数快照来源: {source} {fname}（{len(df)} 行）")
        if len(frames) >= max_sources:
            break
    return frames


def _index_history(em_symbol):
    """指数日线用于 7 日走势；东财失败时退到新浪日线，避免走势图直接消失"""
    for fname in ("stock_zh_index_daily_em", "index_zh_daily_sina"):
        fn = getattr(ak, fname, None)
        if fn is None:
            continue
        try:
            df = fn(symbol=em_symbol)
            if df is None or df.empty:
                continue
            col = "close" if "close" in df.columns else df.columns[-1]
            vals = [_safe_float(v) for v in df[col].tolist()]
            vals = [v for v in vals if v]
            last_day = ""
            if "date" in df.columns and len(df):
                last_day = str(df["date"].iloc[-1])[:10]
            elif len(df.index):
                last_day = str(df.index[-1])[:10]
            if len(vals) >= 2:
                return vals[-8:], last_day
        except Exception as e:
            print(f"[行情] {fname}({em_symbol}) 失败: {type(e).__name__}")
    return [], ""


def _match_index_row(df, aliases, code6):
    """优先按名称（含别名）精确匹配，失败再按代码匹配，兼容列名或命名变化"""
    if "名称" in df.columns:
        names = df["名称"].astype(str).str.strip()
        for alias in aliases:
            hit = df[names == alias]
            if not hit.empty:
                return hit.iloc[0]
        # 名称里包含别名（如「恒生科技指数」含「恒生科技」），放在最后兜底
        for alias in aliases:
            if len(alias) < 3:
                continue
            hit = df[names.str.contains(alias, regex=False, na=False)]
            if not hit.empty:
                return hit.iloc[0]
    for code_col in ("代码", "指数代码", "symbol"):
        if code_col in df.columns:
            codes = df[code_col].astype(str).str.strip().str.replace(r"^(sh|sz|bj|hk)", "", regex=True)
            hit = df[codes == code6]
            if not hit.empty:
                return hit.iloc[0]
    return None


def _quote_from_index_row(name, yf_symbol, row, em_symbol, want_history=True):
    """从指数快照行构造行情：展示口径以官方涨跌幅为准，再由它反推昨收保证自洽"""
    price = _safe_float(row.get("最新价"))
    pct_field = _safe_float(row.get("涨跌幅"))
    prev_raw = _safe_float(row.get("昨收"))

    prev = None
    if price and pct_field is not None and pct_field > -100:
        prev = price / (1 + pct_field / 100)
        if prev_raw and abs((price - prev_raw) / prev_raw * 100 - pct_field) > 0.15:
            print(
                f"[行情] {name} 昨收字段({prev_raw:.2f})与涨跌幅({pct_field:+.2f}%)不一致，按涨跌幅反推"
            )
    else:
        prev = prev_raw
        chg = _safe_float(row.get("涨跌额"))
        if not prev and price and chg is not None:
            prev = price - chg

    hist, last_day = ([], "")
    if want_history:
        hist, last_day = _index_history(em_symbol)
        if hist and price:
            hist = hist[:-1] + [price]
    return _build_quote(name, yf_symbol, price, prev, hist, _a_as_of(last_day), "东财·新浪")


def get_a_market():
    """A 股/港股核心指数：东财 → 新浪 → Yahoo 三级源，逐只补齐并做双源交叉校验。

    指数数量增加后，只为靠前的几个标的拉日线画走势线，其余只取快照数值，
    避免单个快照源 + 每只一条日线的请求量把运行时间拉长。
    """
    quotes = {}
    frames = _load_index_frames()
    if not frames:
        print("[行情] 东财/新浪指数接口均不可用，改用 Yahoo 备用源")

    for idx, (name, em_sym, yf_sym, aliases) in enumerate(A_INDEX_TARGETS):
        code6 = re.sub(r"^(sh|sz|bj|hk)", "", em_sym)
        row, src = None, ""
        for label, df in frames:
            try:
                hit = _match_index_row(df, aliases, code6)
            except Exception as e:
                print(f"[行情] {name} 行匹配异常: {e}")
                hit = None
            if hit is not None:
                row, src = hit, label
                break
        if row is None:
            print(f"[行情] 快照源未返回 {name}({code6})，稍后用 Yahoo 补齐")
            continue

        # 双源交叉校验：另一个源也返回这只指数时，比较涨跌幅是否一致
        for label, df in frames:
            if label == src:
                continue
            try:
                other = _match_index_row(df, aliases, code6)
            except Exception:
                other = None
            if other is None:
                continue
            p1 = _safe_float(row.get("涨跌幅"))
            p2 = _safe_float(other.get("涨跌幅"))
            if p1 is not None and p2 is not None and abs(p1 - p2) > 0.3:
                print(
                    f"[行情] {name} 两源涨跌幅不一致：{src} {p1:+.2f}% / {label} {p2:+.2f}%，采用 {src}"
                )

        quote = _quote_from_index_row(name, yf_sym, row, em_sym, want_history=(idx < 6))
        if quote:
            quotes[name] = quote
        else:
            print(f"[行情] {name} 快照数值不合理，稍后用 Yahoo 补齐")

    if len(quotes) < len(A_INDEX_TARGETS):
        for name, _em_sym, yf_sym, _aliases in A_INDEX_TARGETS:
            if name in quotes or not yf_sym:
                continue
            quote = _yf_quote(name, yf_sym)
            if quote:
                quote["source"] = "Yahoo 备用"
                quotes[name] = quote

    return [quotes[n] for n, _e, _y, _a in A_INDEX_TARGETS if n in quotes]


def fetch_rss_within_hours(rss_dict, hours=12, max_per_feed=7):
    """抓取指定时间窗口内的 RSS 信源并按时间倒序排列（条目过少时自动放宽到 24h）。

    信源扩展到二十多组后逐条串行请求会明显拖慢任务，这里每组一个线程并发拉取，
    总耗时取决于最慢的那个源，单个源失败只影响自己。
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _fetch_one(pair):
        source_name, urls = pair
        if isinstance(urls, str):
            urls = [urls]
        parsed = []
        for url in urls:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=12)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                for entry in feed.entries:
                    ts_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                    ts = calendar.timegm(ts_struct) if ts_struct else None
                    parsed.append((
                        ts,
                        clean_html(entry.get("title", "")),
                        clean_html(entry.get("summary", entry.get("description", "")))[:240],
                        (entry.get("link") or "").strip(),
                    ))
                if parsed:
                    break
            except Exception as e:
                print(f"[RSS] {source_name} 抓取失败: {type(e).__name__}: {str(e)[:60]}")
        return source_name, parsed

    def _collect(window_hours):
        cutoff_ts = (now_utc - datetime.timedelta(hours=window_hours)).timestamp()
        pairs = list(rss_dict.items())
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(_fetch_one, pairs))

        lines = []
        for source_name, parsed in batches:
            if not parsed:
                print(f"[RSS] {source_name} 无可用条目")
                continue
            kept = []
            for ts, title, summary, link in parsed:
                if not title:
                    continue
                # 没有发布时间戳的源（如某些付费墙 RSS）按窗口内处理，避免被整体丢掉
                if ts is not None and ts < cutoff_ts:
                    continue
                kept.append((ts or (now_utc - datetime.timedelta(hours=window_hours / 2)).timestamp(),
                             title, summary, link))
            kept.sort(key=lambda x: x[0], reverse=True)
            print(f"[RSS] {source_name} 命中 {len(kept)} 条")
            for ts, title, summary, link in kept[:max_per_feed]:
                pub_str = datetime.datetime.fromtimestamp(ts, tz=BJ_TZ).strftime("%m-%d %H:%M")
                lines.append(f"[{source_name} | {pub_str}] 标题:{title} | 摘要:{summary} | 链接:{link}")
        return lines

    lines = _collect(hours)
    if len(lines) < 8 and hours < 24:
        print(f"[RSS] {hours}h 内条目较少({len(lines)}条)，自动放宽至 24h 补足...")
        lines = _collect(24)
    print(f"[RSS] 本次共注入 {len(lines)} 条")
    return "\n".join(lines)


def _cell(row, col):
    """安全读取单元格，把 None / NaN / NaT 统一成空串"""
    if not col:
        return ""
    try:
        val = row.get(col)
    except Exception:
        return ""
    if val is None:
        return ""
    text = str(val).strip()
    return "" if text.lower() in ("nan", "none", "nat") else text


def _pick_col(df, names):
    for name in names:
        if name in df.columns:
            return name
    return None


def _call_cls_api():
    """财联社电报接口：akshare 1.20.41 把 stock_info_global_cls 改名为 stock_news_global_cls"""
    attempts = []
    if hasattr(ak, "stock_news_global_cls"):
        attempts += [
            ("stock_news_global_cls(symbol=全部)", lambda: ak.stock_news_global_cls(symbol="全部")),
            ("stock_news_global_cls()", lambda: ak.stock_news_global_cls()),
        ]
    if hasattr(ak, "stock_info_global_cls"):
        attempts.append(("stock_info_global_cls()", lambda: ak.stock_info_global_cls()))
    for label, call in attempts:
        try:
            df = call()
            if df is not None and not df.empty:
                print(f"[财联社] 数据来源: {label}（{len(df)} 条）")
                return df
        except Exception as e:
            print(f"[财联社] {label} 调用失败: {type(e).__name__}: {e}")
    return None


def fetch_cls_telegraph(limit=55):
    """财联社 7x24 实时电报（接口名与列名都做兼容映射，避免改名后静默变成空数据）"""
    df_cls = _call_cls_api()
    if df_cls is None:
        print("[财联社] 无可用接口")
        return []

    title_col = _pick_col(df_cls, ["标题", "title"])
    content_col = _pick_col(df_cls, ["内容", "正文", "摘要", "content"])
    date_col = _pick_col(df_cls, ["发布日期", "日期", "date"])
    time_col = _pick_col(df_cls, ["发布时间", "时间", "time"])

    items = []
    for _, row in df_cls.head(limit).iterrows():
        title = _cell(row, title_col)
        content = _cell(row, content_col)
        text = title or content
        if not text:
            continue
        if title and content and title not in content:
            text = f"{title}｜{content}"
        ts = f"{_cell(row, date_col)[-5:]} {_cell(row, time_col)[:5]}".strip()
        items.append({"source": "财联社", "ts": ts, "text": text, "link": ""})
    print(f"[财联社] 有效快讯 {len(items)} 条")
    return items


def _norm_ts(raw):
    """把各家五花八门的时间统一成 MM-DD HH:MM，便于跨源混排"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{10}", text):
        try:
            return datetime.datetime.fromtimestamp(int(text), BJ_TZ).strftime("%m-%d %H:%M")
        except Exception:
            return ""
    m = re.search(r"(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})", text)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d} {int(m.group(3)):02d}:{m.group(4)}"
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        return f"{datetime.datetime.now(BJ_TZ).strftime('%m-%d')} {int(m.group(1)):02d}:{m.group(2)}"
    return ""


def fetch_sina_7x24(limit=30):
    """新浪财经 7x24 全球直播：国内宏观与市场快讯的第二主力源"""
    url = "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        payload = r.json()
        feed = (((payload.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
    except Exception as e:
        print(f"[新浪7x24] 抓取失败: {type(e).__name__}: {str(e)[:60]}")
        return []
    items = []
    for row in feed[:limit]:
        text = clean_html(row.get("rich_text") or row.get("content") or "")
        if not text:
            continue
        items.append({"source": "新浪财经", "ts": _norm_ts(row.get("create_time")), "text": text, "link": ""})
    print(f"[新浪7x24] 有效快讯 {len(items)} 条")
    return items


def fetch_em_flash(limit=25):
    """东方财富 7x24 快讯（老版 JSONP 接口，对云端 IP 限流较少）"""
    url = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        body = r.text
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("返回体不是 JSONP")
        payload = json.loads(body[start : end + 1])
        rows = payload.get("LivesList") or []
    except Exception as e:
        print(f"[东财快讯] 抓取失败: {type(e).__name__}: {str(e)[:60]}")
        return []
    items = []
    for row in rows[:limit]:
        title = (row.get("title") or "").strip()
        digest = clean_html(row.get("digest") or "")
        text = f"{title}｜{digest}" if title and digest and title not in digest else (title or digest)
        if not text:
            continue
        items.append({
            "source": "东方财富",
            "ts": _norm_ts(row.get("showtime")),
            "text": text,
            "link": row.get("url_unique") or "",
        })
    print(f"[东财快讯] 有效快讯 {len(items)} 条")
    return items


def fetch_ths_flash(limit=25):
    """同花顺 7x24 快讯"""
    url = "https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=40"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        payload = r.json()
        rows = ((payload.get("data") or {}).get("list")) or []
    except Exception as e:
        print(f"[同花顺] 抓取失败: {type(e).__name__}: {str(e)[:60]}")
        return []
    items = []
    for row in rows[:limit]:
        title = (row.get("title") or "").strip()
        digest = clean_html(row.get("digest") or "")
        text = f"{title}｜{digest}" if title and digest and title not in digest else (title or digest)
        if not text:
            continue
        items.append({
            "source": "同花顺",
            "ts": _norm_ts(row.get("rtime") or row.get("ctime")),
            "text": text,
            "link": row.get("url") or "",
        })
    print(f"[同花顺] 有效快讯 {len(items)} 条")
    return items


def _dedup_key(text):
    """去重指纹：去掉标点空格后取前 34 字，能吃掉「同一事件不同源」的转载"""
    core = re.sub(r"[\s\W_]+", "", text or "")
    return core[:34]


def fetch_cn_flash(limit=90):
    """国内快讯多源聚合：财联社 / 新浪 / 东财 / 同花顺，去重后按时间混排。

    单一信源一旦接口变动或限流就会整段空白，多源交叉后既补足覆盖面，也能相互印证。
    """
    collectors = (
        ("财联社", lambda: fetch_cls_telegraph(limit=60)),
        ("新浪", lambda: fetch_sina_7x24(limit=35)),
        ("东财", lambda: fetch_em_flash(limit=30)),
        ("同花顺", lambda: fetch_ths_flash(limit=30)),
    )
    merged, seen, stat = [], set(), {}
    for label, fn in collectors:
        try:
            batch = fn() or []
        except Exception as e:
            print(f"[国内快讯] {label} 异常: {type(e).__name__}: {str(e)[:60]}")
            batch = []
        stat[label] = len(batch)
        for item in batch:
            key = _dedup_key(item["text"])
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)

    # 按时间倒序，缺少时间的排在后面，保证「刚刚发生」的排前面
    def _sort_key(item):
        ts = item.get("ts") or ""
        return (ts != "", ts)

    merged.sort(key=_sort_key, reverse=True)
    merged = merged[:limit]

    print(f"[国内快讯] 各源命中 {stat} → 去重后 {len(merged)} 条")
    lines = []
    for item in merged:
        link = f" | 链接:{item['link']}" if item.get("link") else ""
        lines.append(f"[{item['source']} | {item['ts']}] {item['text'][:280]}{link}")
    return "\n".join(lines)


def _extract_json(text):
    """从模型输出里抠出 JSON 对象，容忍 ```json 围栏、前后寒暄、以及尾部的多余字符。

    旧实现是 json.loads(content) 一把梭，模型偶尔多写一句「以上是本期内容」就整轮任务失败。
    """
    if not text:
        return None
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*", "", body)
    body = re.sub(r"\s*```$", "", body).strip()
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(body[start : end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _call_llm_json(client, prompt, attempts=3):
    """调用大模型并解析 JSON：网络抖动/输出不合格自动重试，全失败返回 None 由上层降级。"""
    last_err = ""
    for i in range(attempts):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            data = _extract_json(resp.choices[0].message.content)
            if data:
                return data
            last_err = "返回内容不是合法 JSON 对象"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"[LLM] 第 {i + 1}/{attempts} 次调用失败：{last_err}")
        if i + 1 < attempts:
            time.sleep(2 * (i + 1))
    return None


def _fallback_llm_data(edition):
    """大模型不可用时的降级数据：行情与页面结构照常，只把新闻区标注为暂不可用。

    好处是任务不会整轮红叉——行情卡和往期入口仍然正常发布，飞书卡片也照发。
    """
    meta = get_edition_meta(edition)
    return {
        "mood": "neutral",
        "mood_headline": f"{meta['name']} · 本期智能评级暂不可用",
        "overview": "本期大模型评级服务暂时不可用，已保留跨市场行情数据；新闻板块请稍后重试，或从右上角下拉查看往期。",
        "us_global_news": [],
        "cn_a_news": [],
        "general_news": [],
        "_degraded": True,
    }


def analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw, edition, official_raw=""):
    """按早报/收盘报/晚报差异化定位，调用大模型去重、精译与重磅评级"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=180.0, max_retries=0)
    now_str = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")

    if edition == "close":
        role_focus = (
            "当前为【A股收盘报（15:30）】：请重点复盘今天 A 股与港股全天盘面主线、领涨领跌逻辑、"
            "白天国内部委政策/产业突发催化，以及亚太市场与欧盘早段动态。请把 A 股与国内产业内容做深做实，"
            "海外部分优先提炼今天白天新增的增量消息，避免重复早上 8:30 已报过的隔夜旧闻。"
        )
        overview_guide = "150-200字的A股收盘全景复盘。梳理今天国内市场核心强弱、成交与板块轮动逻辑、白天重磅政策或产业催化，以及晚间欧美市场前瞻看点。"
        count_req = "cn_a_news 精选 6 条最具价值的国内A股/政策/产业新闻；us_global_news 精选 4 条今天白天新增的全球/美股盘前动态；general_news 精选 4 条高价值国际时政或前沿科技新闻。"
        brief_type = "收盘报"
    elif edition == "pm":
        role_focus = (
            "当前为【晚报（22:00）】：请重点聚焦欧洲盘面走势、今晚美股盘前及刚开盘的核心催化"
            "（如美国 CPI/非农/PMI 等晚间宏观数据、美联储官员最新表态、科技巨头盘前财报与异动），"
            "同时补充下午 15:30 收盘后国内发布的重磅政策或龙头公司盘后公告。务必优先抓取晚间新鲜增量消息。"
        )
        overview_guide = "150-200字的晚报全景前瞻。串联今晚美股盘前/开盘最核心的宏观数据与科技股催化、大宗与加密异动，并补充国内盘后值得关注的新增变量。"
        count_req = "us_global_news 精选 6 条最重要的晚间美股/欧美宏观与科技巨头新闻；cn_a_news 精选 4 条国内盘后新增重磅政策或公司公告；general_news 精选 4 条高价值国际时政或前沿科技新闻。"
        brief_type = "晚报"
    else:
        role_focus = (
            "当前为【早报（08:30）】：请重点聚焦隔夜美股收盘主线、美联储与全球宏观动向、"
            "科技巨头盘后财报与大宗/加密异动，以及今天 A 股开盘前最值得关注的产业催化与风险。"
        )
        overview_guide = "150-200字的早报全景总览。串联隔夜美股核心驱动、宏观利率/政策预期变化，以及今天A股与科技线需要重点关注的催化或风险。"
        count_req = "us_global_news 精选 6 条最重要的隔夜美股与全球财经新闻；cn_a_news 精选 5 条最具影响力的国内宏观/A股盘前产业新闻；general_news 精选 4 条高价值国际时政或前沿科技新闻。"
        brief_type = "早报"

    prompt = f"""你是一位华尔街见闻与彭博风格的资深财经总编。当前北京时间：{now_str}。
{role_focus}

请基于以下信源（美联储/SEC/欧洲央行官方公告、彭博、金融时报、路透、WSJ、MarketWatch、CNBC、Investing、日经亚洲、南华早报、财联社、新浪财经、东方财富、同花顺）合并去重同类事件，将英文报道精准翻译为专业中文，并按【重磅程度】严格评分排序。

【信源权重规则（重要）】
- 官方一手源（美联储、SEC、欧洲央行）的公告是所有媒体转述的源头，遇到同一事件的官方版本与媒体报道版本，以官方口径为准，并在 source 里标注为「美联储」等官方名称。
- 多家媒体同日报道同一件事时，合并为一条，取信息量最大的版本，不要拆成多条凑数。
- 国内快讯源（财联社/新浪/东方财富/同花顺）常有「一句话电报」，若无实质新增信息则不要单独立条；只挑出真正有政策、数据、公司公告含量的事件。

【重磅程度评分标准（score，1~5 整数，务必拉开区分度，不要全部打 5 分）】：
- 5 分（核弹/头版级）：美联储或主要央行重磅政策、CPI/非农/GDP等关键宏观数据、万亿美元科技巨头（英伟达/苹果/微软等）财报或重大突发、关税/地缘重大冲击、或同时被多家权威媒体交叉报道的超级事件（全篇通常 2~4 条）。
- 4 分（主线催化级）：重要产业政策、头部企业并购/重磅新品发布、引发板块集体异动的核心新闻。
- 3 分（常规参考级）：单一公司常规动态、大行评级观点、局部行业进展。

严格按以下 JSON 格式输出（不要输出任何 Markdown 代码块标记或多余说明）：
{{
  "mood": "bullish" 或 "bearish" 或 "neutral",
  "mood_headline": "一句话定调本时段最核心的主题（22字以内，有刊物头版感）",
  "overview": "{overview_guide}",
  "us_global_news": [
    {{
      "score": 5,
      "tag": "美股/美联储/科技/宏观/财报",
      "sentiment": "利好/利空/中性",
      "title": "高信息量中文标题（26字以内）",
      "fact": "核心事实与具体数据（70-110字，保留关键金额、百分比、公司与人物）",
      "impact": "市场逻辑与关联资产/板块影响（35-55字）",
      "source": "来源与时间（如 彭博市场 · 20:30）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ],
  "cn_a_news": [
    {{
      "score": 4,
      "tag": "A股/政策/产业/资金/港股",
      "sentiment": "利好/利空/中性",
      "title": "高信息量中文标题（26字以内）",
      "fact": "核心事实与具体数据（70-110字）",
      "impact": "对A股对应板块或个股的催化逻辑（35-55字）",
      "source": "来源与时间（如 财联社 · 15:10）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ],
  "general_news": [
    {{
      "score": 4,
      "tag": "国际/AI科技/商业/地缘",
      "title": "高信息量中文标题（26字以内）",
      "summary": "完整的事件脉络与核心看点（70-110字，不要只复述标题）",
      "source": "来源与时间（如 彭博科技 · 19:20）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ]
}}

严格要求：
1. 这是每日{brief_type}：{count_req} 每个列表内按 score 从高到低排列。
2. 严禁编造数据或链接，所有事实必须来自下方原始素材。

【官方一手公告（美联储 / SEC / 欧洲央行，最高权威，优先采用）】：
{official_raw if official_raw else "（本时段无新公告）"}

【全球与美股顶级财经信源】：
{global_fin_raw if global_fin_raw else "（暂无数据）"}

【国内 7x24 快讯（财联社 / 新浪财经 / 东方财富 / 同花顺 多源去重混排）】：
{cn_fin_raw if cn_fin_raw else "（暂无数据）"}

【国际时政与前沿科技信源】：
{general_raw if general_raw else "（暂无数据）"}
"""
    data = _call_llm_json(client, prompt)
    if not data:
        print("[LLM] 多次重试仍失败，降级为「仅行情」模式继续发布")
        return _fallback_llm_data(edition)
    return normalize_and_sort_llm_data(data)


def normalize_and_sort_llm_data(data):
    """代码层强制清洗 score 并按重磅程度降序排列"""
    for key in ("us_global_news", "cn_a_news", "general_news"):
        items = data.get(key) or []
        for idx, item in enumerate(items):
            item["score"] = safe_score(item.get("score", 3))
            item["_orig_idx"] = idx
        items.sort(key=lambda x: (-x["score"], x["_orig_idx"]))
        for item in items:
            item.pop("_orig_idx", None)
        data[key] = items
    return data


# ================= SVG 走势图与网页渲染引擎 =================
def render_sparkline_svg(values, stroke_color, grad_id):
    if not values or len(values) < 2:
        return ""
    w, h = 68, 22
    pad = 2.5
    vmin, vmax = min(values), max(values)
    span = vmax - vmin
    n = len(values)

    coords = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * w
        y = h / 2 if span == 0 else h - pad - ((v - vmin) / span) * (h - pad * 2)
        coords.append((round(x, 1), round(y, 1)))

    poly_pts = " ".join(f"{x},{y}" for x, y in coords)
    area_pts = f"0,{h} {poly_pts} {w},{h}"
    last_x, last_y = coords[-1]

    return (
        f'<svg class="sparkline" width="{w}" height="{h}" viewBox="0 0 {w} {h}" fill="none" aria-hidden="true">'
        f'  <defs>'
        f'    <linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
        f'      <stop offset="0%" stop-color="{stroke_color}" stop-opacity="0.32"/>'
        f'      <stop offset="100%" stop-color="{stroke_color}" stop-opacity="0"/>'
        f'    </linearGradient>'
        f'  </defs>'
        f'  <polygon points="{area_pts}" fill="url(#{grad_id})"/>'
        f'  <polyline points="{poly_pts}" stroke="{stroke_color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
        f'  <circle cx="{last_x}" cy="{last_y}" r="2.2" fill="{stroke_color}"/>'
        f'</svg>'
    )


def _as_of_label(items):
    """把行情时间戳压成极短标签，方便在卡片角上标注数据时点"""
    if not items:
        return ""
    raw = items[0].get("as_of") or ""
    if not raw:
        return ""
    if "实时" in raw:
        return "实时"
    if "收盘" in raw:
        # Yahoo 口径形如「2026-11-26 收盘」，东财口径只有「东财收盘」
        if len(raw) >= 10 and raw[4] == "-":
            return f"{raw[5:10]} 收"
        return "收盘"
    return raw[:6]


def render_market_group_html(title, data, is_us=True, preview=4):
    """行情组卡：可整组折叠；默认只露前 preview 行，其余收进「展开其余 N 项」。

    标的变多后（A股/港股 11、美股指数 7、重点个股 12、大宗 10）如果一次性铺开，
    手机端会重新变成长页面。这里用两层折叠保住首屏密度，同时保留看全部的能力。
    """
    pos_cls, neg_cls = ("pos-green", "neg-red") if is_us else ("pos-red", "neg-green")
    stroke_map = {
        "pos-green": "#10b981",
        "neg-red": "#f43f5e",
        "pos-red": "#f43f5e",
        "neg-green": "#10b981",
        "flat": "#94a3b8",
    }
    rows = []
    for item in data:
        pct = item["pct"]
        cls = "flat" if abs(pct) < 0.005 else (pos_cls if pct > 0 else neg_cls)
        grad_id = "sp-" + re.sub(r"[^A-Za-z0-9]", "", item.get("symbol", "x"))
        spark = render_sparkline_svg(item.get("history", []), stroke_map[cls], grad_id)
        rows.append(
            f'<div class="mrow">'
            f'{spark}'
            f'<span class="mname">{esc(item["name"])}<em>{esc(item["price"])}</em></span>'
            f'<span class="mpct {cls}">{pct:+.2f}%</span>'
            f'</div>'
        )
    if not rows:
        rows.append('<div class="mrow"><span class="mname" style="color:var(--text3)">暂无数据</span></div>')

    head_rows, rest_rows = rows[:preview], rows[preview:]
    up = sum(1 for i in data if i["pct"] > 0.005)
    down = sum(1 for i in data if i["pct"] < -0.005)
    tally = f'<span class="gtally"><b class="tu">{up}↑</b><b class="td">{down}↓</b></span>' if data else ""
    note = _as_of_label(data)
    note_html = f'<span class="gtime">{esc(note)}</span>' if note else ""

    more = ""
    if rest_rows:
        more = (
            f'<details class="gmore"><summary>展开其余 {len(rest_rows)} 项</summary>'
            f'<div class="gmore-body">{"".join(rest_rows)}</div></details>'
        )
    tail = f'<div class="gend">{note_html}</div>' if note_html else ""
    return (
        f'<details class="gcard {"us" if is_us else "cn"}" open>'
        f'  <summary class="gcap">'
        f'    <span class="gtitle">{esc(title)}</span>'
        f'    <span class="gmeta">{tally}<i class="gcaret"></i></span>'
        f'  </summary>'
        f'  <div class="grows">{"".join(head_rows)}{more}{tail}</div>'
        f'</details>'
    )


def render_must_read_html(llm_data, edition):
    """首屏「必读」横向 chip：替代原先占据大半屏的 spotlight 列表"""
    order = (
        ["cn_a_news", "us_global_news", "general_news"]
        if edition == "close"
        else ["us_global_news", "cn_a_news", "general_news"]
    )
    prefix_map = {"us_global_news": "us", "cn_a_news": "cn", "general_news": "gen"}
    cands = []
    for key in order:
        for i, item in enumerate(llm_data.get(key) or [], 1):
            cands.append((safe_score(item.get("score", 3)), f"{prefix_map[key]}-{i}", item.get("title", "")))
    cands.sort(key=lambda x: -x[0])
    picks = [c for c in cands[:3] if c[2]]
    if not picks:
        return ""
    chips = []
    for score, anchor, title in picks:
        badge = "🔥" if score >= 5 else f"★{score}"
        chips.append(
            f'<a class="chip" href="#{anchor}"><b>{badge}</b><span>{esc(title)}</span><i>›</i></a>'
        )
    return f'<div class="chips">{"".join(chips)}</div>'


def render_news_cards_html(items, section_prefix, with_impact=True):
    """每条新闻渲染成原生 <details>：默认只占一行，点开才展开正文"""
    if not items:
        return '<div class="empty">本时段暂无重要更新</div>'
    cards = []
    for i, item in enumerate(items, 1):
        score = safe_score(item.get("score", 3))
        anchor_id = f"{section_prefix}-{i}"
        title = esc(item.get("title", ""))

        if with_impact:
            sent = item.get("sentiment", "中性")
            sent_cls, sent_mark = {"利好": ("bull", "▲"), "利空": ("bear", "▼")}.get(sent, ("neu", "●"))
            body = esc(item.get("fact", ""))
            impact = item.get("impact", "")
        else:
            sent_cls, sent_mark = "neu", ""
            body = esc(item.get("summary", ""))
            impact = ""

        impact_html = (
            f'<div class="nimpact"><b>逻辑</b>{esc(impact)}</div>' if impact else ""
        )
        link = str(item.get("link", "")).strip()
        link_html = (
            f'<a href="{esc(link)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a>'
            if link.startswith(("http://", "https://"))
            else "<span class=\"nolink\">快讯</span>"
        )
        # 只让首条重磅默认展开，其余折叠，保证首屏不被正文占满
        open_attr = " open" if (score >= 5 and i == 1) else ""

        cards.append(
            f'<details class="nitem" id="{anchor_id}" data-score="{score}" data-sentiment="{sent_cls}"{open_attr}>'
            f'<summary>'
            f'<span class="nidx">{i:02d}</span>'
            f'<span class="ntitle">{title}</span>'
            f'<span class="nside"><span class="nstars">{"★" * score}</span>'
            f'<span class="nsent {sent_cls}">{sent_mark}</span></span>'
            f'</summary>'
            f'<div class="nbody">'
            f'<div class="ntag"><span>{esc(item.get("tag", "要闻"))}</span><span>{esc(item.get("source", ""))}</span></div>'
            f'<p class="nfact">{body}</p>'
            f'{impact_html}'
            f'<div class="nfoot">{link_html}</div>'
            f'</div>'
            f'</details>'
        )
    return "\n".join(cards)


def build_archive_select_html(entry_ids, current_id, is_archive=False):
    if not entry_ids:
        return ""
    prefix = "" if is_archive else "archive/"
    options = []
    for eid in entry_ids:
        selected = " selected" if eid == current_id else ""
        label = format_archive_label(eid)
        target = f"{prefix}{eid}.html"
        options.append(f'<option value="{esc(target)}"{selected}>{esc(label)}</option>')
    return (
        '<select class="arch" aria-label="往期早中晚报" '
        'onchange="if(this.value) window.location.href=this.value">'
        + "".join(options)
        + "</select>"
    )


def render_news_sections_html(us_news, cn_news, gen_news, edition):
    """三个板块改成 Tab 面板：返回（tab 按钮 HTML, 面板 HTML），避免纵向堆叠三块长列表"""
    us_title = {
        "am": "隔夜美股与全球财经",
        "close": "亚欧盘面与美股日间动态",
        "pm": "美股盘前/开盘与欧美宏观",
    }.get(edition, "美股与全球财经")

    cn_title = {
        "am": "A 股盘前与国内宏观",
        "close": "A 股全天收盘与产业催化",
        "pm": "国内盘后公告与宏观新增",
    }.get(edition, "A 股与国内宏观")

    blocks = [
        ("us", "美股", f"🇺🇸 {us_title}", us_news, True),
        ("cn", "A股", f"🇨🇳 {cn_title}", cn_news, True),
        ("gen", "国际", "🌐 国际时政与前沿科技", gen_news, False),
    ]
    if edition == "close":
        blocks = [blocks[1], blocks[0], blocks[2]]

    tabs, panels = [], []
    for idx, (key, short, title, items, with_impact) in enumerate(blocks):
        on = " on" if idx == 0 else ""
        tabs.append(
            f'<button class="tab{on}" type="button" data-tab="{key}" role="tab">'
            f'{short}<em>{len(items)}</em></button>'
        )
        panels.append(
            f'<section class="panel{on}" data-panel="{key}">'
            f'<div class="phead">{esc(title)}</div>'
            f'{render_news_cards_html(items, key, with_impact=with_impact)}'
            f'</section>'
        )
    return "".join(tabs), "\n".join(panels)


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark" data-edition="__EDITION_CODE__">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
<meta name="theme-color" content="#070b14" />
<title>__FULL_NAME__ · __DATE_STR__</title>
<style>
  :root {
    --bg: #080b13;
    --bg-card: #101623;
    --card-hi: rgba(255, 255, 255, 0.045);
    --bg-subtle: rgba(255, 255, 255, 0.045);
    --border: rgba(148, 163, 184, 0.12);
    --border-strong: rgba(148, 163, 184, 0.2);
    --text: #eef2f8;
    --text2: #a3b0c2;
    --text3: #6b7a8f;
    --accent: #38bdf8;
    --accent-soft: rgba(56, 189, 248, 0.12);
    --edition-bg: rgba(56, 189, 248, 0.15);
    --edition-color: #7dd3fc;
    --up-red: #f43f5e;
    --up-green: #10b981;
    --amber: #f59e0b;
    --mono: ui-monospace, "SF Mono", "Roboto Mono", Menlo, Consolas, monospace;
  }
  [data-edition="close"] {
    --accent: #f59e0b;
    --accent-soft: rgba(245, 158, 11, 0.13);
    --edition-bg: rgba(245, 158, 11, 0.16);
    --edition-color: #fcd34d;
  }
  [data-edition="pm"] {
    --accent: #818cf8;
    --accent-soft: rgba(129, 140, 248, 0.13);
    --edition-bg: rgba(139, 92, 246, 0.16);
    --edition-color: #c4b5fd;
  }
  [data-theme="light"] {
    --bg: #f4f6fa;
    --bg-card: #ffffff;
    --card-hi: rgba(255, 255, 255, 0.9);
    --bg-subtle: #f3f6fa;
    --border: rgba(15, 23, 42, 0.085);
    --border-strong: rgba(15, 23, 42, 0.16);
    --text: #0d1626;
    --text2: #47546a;
    --text3: #77869b;
    --accent: #0284c7;
    --accent-soft: rgba(2, 132, 199, 0.09);
    --edition-bg: rgba(2, 132, 199, 0.11);
    --edition-color: #0369a1;
    --up-red: #dc2626;
    --up-green: #059669;
    --amber: #c2740a;
  }
  [data-theme="light"][data-edition="close"] {
    --accent: #b45309;
    --accent-soft: rgba(180, 83, 9, 0.1);
    --edition-bg: rgba(180, 83, 9, 0.12);
    --edition-color: #92400e;
  }
  [data-theme="light"][data-edition="pm"] {
    --accent: #4f46e5;
    --accent-soft: rgba(79, 70, 229, 0.09);
    --edition-bg: rgba(79, 70, 229, 0.12);
    --edition-color: #4338ca;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
      "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.62;
    -webkit-font-smoothing: antialiased;
    padding-bottom: 10px;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 0 14px; }
  .pg {
    position: fixed; top: 0; left: 0;
    height: 2px; width: 0;
    background: linear-gradient(90deg, var(--accent), #a78bfa);
    z-index: 99; transition: width 0.12s linear;
  }

  /* ---------- 顶栏 ---------- */
  .hdr {
    position: sticky; top: 0; z-index: 60;
    background: color-mix(in srgb, var(--bg) 86%, transparent);
    backdrop-filter: blur(16px) saturate(1.4);
    -webkit-backdrop-filter: blur(16px) saturate(1.4);
    border-bottom: 1px solid var(--border);
  }
  .hdrin { height: 46px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .hl { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .dot {
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--accent); flex: 0 0 auto;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
  }
  .hdate { font-family: var(--mono); font-size: 11.5px; color: var(--text2); white-space: nowrap; letter-spacing: -0.01em; }
  .epill {
    font-size: 10.5px; font-weight: 800; padding: 2px 8px; border-radius: 999px;
    background: var(--edition-bg); color: var(--edition-color);
    white-space: nowrap; letter-spacing: 0.02em;
  }
  .hr { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
  select.arch {
    max-width: 104px; height: 27px;
    background: var(--bg-card); color: var(--text2);
    border: 1px solid var(--border); border-radius: 8px;
    font-size: 11px; padding: 0 2px; font-family: inherit;
  }
  button.icon {
    width: 27px; height: 27px; border-radius: 8px; cursor: pointer;
    background: var(--bg-card); color: var(--text2);
    border: 1px solid var(--border); font-size: 12px; line-height: 1;
    transition: border-color 0.15s ease;
  }
  button.icon:active { border-color: var(--accent); }

  /* ---------- 头版 ---------- */
  .hero { padding: 16px 0 12px; }
  .kicker {
    display: flex; align-items: center; gap: 7px;
    font-family: var(--mono); font-size: 9.5px; font-weight: 700;
    letter-spacing: 0.16em; color: var(--text3); text-transform: uppercase;
    margin-bottom: 8px;
  }
  .kicker::after {
    content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, var(--border-strong), transparent);
  }
  .hero h1 {
    font-size: 20px; font-weight: 800; line-height: 1.42;
    letter-spacing: -0.015em; margin-bottom: 9px;
    text-wrap: pretty;
  }
  .hmeta {
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
    font-family: var(--mono); font-size: 10.5px; color: var(--text3);
  }
  .mood {
    font-family: inherit; font-size: 10.5px; font-weight: 800;
    padding: 2.5px 9px; border-radius: 999px; letter-spacing: 0.01em;
  }
  .mood-bullish { background: color-mix(in srgb, var(--up-red) 14%, transparent); color: var(--up-red); }
  .mood-bearish { background: color-mix(in srgb, var(--up-green) 14%, transparent); color: var(--up-green); }
  .mood-neutral { background: var(--accent-soft); color: var(--accent); }

  /* ---------- 行情 2×2（可折叠） ---------- */
  .mkt { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 4px 0 12px; align-items: start; }
  .gcard {
    position: relative; overflow: hidden;
    background: linear-gradient(180deg, var(--card-hi), transparent 46%), var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 13px;
  }
  .gcard::after {
    content: ""; position: absolute; inset: 0; border-radius: 13px;
    pointer-events: none;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  }
  [data-theme="light"] .gcard { box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 6px 16px -12px rgba(15, 23, 42, 0.2); }
  [data-theme="light"] .gcard::after { box-shadow: none; }
  .gcard > summary.gcap {
    display: flex; align-items: center; justify-content: space-between; gap: 5px;
    padding: 8px 10px 6px; cursor: pointer; list-style: none;
    font-size: 10.5px; font-weight: 800; color: var(--text2);
    letter-spacing: 0.02em; user-select: none;
  }
  .gcard > summary.gcap::-webkit-details-marker { display: none; }
  .gtitle { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gmeta { display: flex; align-items: center; gap: 4px; flex: 0 0 auto; }
  .gtally { display: flex; gap: 3.5px; font-family: var(--mono); font-size: 9px; font-weight: 700; }
  .gtally .tu { font-weight: 700; }
  .gtally .td { font-weight: 700; }
  .gcard.us .tu { color: var(--up-green); }
  .gcard.us .td { color: var(--up-red); }
  .gcard.cn .tu { color: var(--up-red); }
  .gcard.cn .td { color: var(--up-green); }
  .gtime { font-family: var(--mono); font-weight: 500; font-size: 9px; color: var(--text3); flex: 0 0 auto; }
  .gcaret {
    width: 0; height: 0; flex: 0 0 auto;
    border-left: 3.5px solid transparent; border-right: 3.5px solid transparent;
    border-top: 4px solid var(--text3);
    transition: transform 0.22s ease;
  }
  .gcard[open] .gcaret { transform: rotate(180deg); }
  .grows { padding: 0 10px 7px; }
  .gcard:not([open]) .grows { display: none; }

  .mrow {
    position: relative;
    display: flex; align-items: center; justify-content: space-between; gap: 4px;
    padding: 4px 0; border-bottom: 1px solid var(--border);
  }
  .mrow:last-child { border-bottom: none; padding-bottom: 1px; }
  .mrow .sparkline {
    position: absolute; right: -1px; bottom: 2px;
    width: 42px; height: 14px; opacity: 0.28; pointer-events: none;
  }
  .mname {
    display: flex; flex-direction: column; min-width: 0;
    font-size: 11.5px; font-weight: 600; line-height: 1.22; letter-spacing: -0.01em;
  }
  .mname em {
    font-style: normal; font-family: var(--mono); font-size: 10px;
    color: var(--text3); font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
  }
  .mpct {
    position: relative; z-index: 1; flex: 0 0 auto;
    font-family: var(--mono); font-size: 10.5px; font-weight: 800;
    font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
    padding: 1px 4.5px; border-radius: 5px;
    background: var(--bg-subtle);
  }
  .pos-green, .neg-green { color: var(--up-green); }
  .pos-red, .neg-red { color: var(--up-red); }
  .pos-green, .neg-green { background: color-mix(in srgb, var(--up-green) 13%, transparent); }
  .pos-red, .neg-red { background: color-mix(in srgb, var(--up-red) 13%, transparent); }
  .flat { color: var(--text2); }

  .gmore { margin-top: 1px; }
  .gmore > summary {
    list-style: none; cursor: pointer; user-select: none;
    display: flex; align-items: center; gap: 4px;
    padding: 5px 0 1px; font-size: 9.5px; font-weight: 600; color: var(--text3);
  }
  .gmore > summary::-webkit-details-marker { display: none; }
  .gmore > summary::after {
    content: "▾"; font-size: 8px; transition: transform 0.2s ease;
  }
  .gmore[open] > summary::after { transform: rotate(180deg); }
  .gmore[open] > summary { color: var(--accent); }
  .gmore-body .mrow:first-child { border-top: 1px solid var(--border); margin-top: 3px; }
  .gend { padding-top: 5px; text-align: right; }
  .gend .gtime { font-size: 8.5px; letter-spacing: 0.02em; }

  /* ---------- 必读 chips ---------- */
  .chips {
    display: flex; gap: 6px; overflow-x: auto;
    padding: 1px 0 12px; scrollbar-width: none;
    scroll-snap-type: x proximity;
  }
  .chips::-webkit-scrollbar { display: none; }
  .chip {
    flex: 0 0 auto; max-width: 80vw; scroll-snap-align: start;
    display: flex; align-items: center; gap: 6px;
    padding: 7px 10px; border-radius: 10px;
    background: linear-gradient(135deg, var(--accent-soft), transparent 70%), var(--bg-card);
    border: 1px solid color-mix(in srgb, var(--accent) 22%, var(--border));
    color: var(--text); text-decoration: none; font-size: 12px; line-height: 1.4;
  }
  .chip:active { border-color: var(--accent); }
  .chip b {
    flex: 0 0 auto; font-family: var(--mono); font-size: 9.5px; font-weight: 800;
    color: var(--amber); letter-spacing: -0.02em;
  }
  .chip span {
    display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
    overflow: hidden; font-weight: 500;
  }
  .chip i { font-style: normal; color: var(--text3); font-size: 12px; }

  /* ---------- 综述 ---------- */
  details.ov {
    position: relative;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 10px 12px 11px; margin-bottom: 13px;
    overflow: hidden;
  }
  details.ov::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2.5px;
    background: linear-gradient(180deg, var(--accent), transparent);
  }
  details.ov > summary {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    cursor: pointer; list-style: none;
    font-size: 10.5px; font-weight: 800; color: var(--accent); letter-spacing: 0.03em;
  }
  details.ov > summary::-webkit-details-marker { display: none; }
  .ovh { font-weight: 500; color: var(--text3); font-size: 9.5px; letter-spacing: 0; }
  .ovh::after { content: "展开"; }
  .ov[open] .ovh::after { content: "收起"; }
  .ovtext { font-size: 12.5px; line-height: 1.78; color: var(--text); margin-top: 8px; letter-spacing: 0.005em; }
  .ov:not([open]) .ovtext { display: none; }

  /* ---------- Tab ---------- */
  .tabs {
    position: sticky; top: 46px; z-index: 50;
    display: flex; gap: 2px; padding: 5px 0 0;
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--border);
  }
  .tab {
    position: relative; flex: 1;
    display: flex; align-items: center; justify-content: center; gap: 4px;
    padding: 8px 2px 9px; border-radius: 0; cursor: pointer;
    background: transparent; border: none;
    color: var(--text3); font-size: 12.5px; font-weight: 700;
    font-family: inherit; letter-spacing: 0.01em;
    transition: color 0.18s ease;
  }
  .tab em {
    font-style: normal; font-family: var(--mono); font-size: 9.5px;
    opacity: 0.8; font-weight: 600;
  }
  .tab.on { color: var(--accent); }
  .tab::after {
    content: ""; position: absolute; left: 22%; right: 22%; bottom: 0; height: 2px;
    border-radius: 2px 2px 0 0; background: var(--accent);
    transform: scaleX(0); transform-origin: center;
    transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .tab.on::after { transform: scaleX(1); }

  /* ---------- 面板 / 新闻条目 ---------- */
  .panel { display: none; padding-top: 2px; }
  .panel.on { display: block; animation: fadeUp 0.28s ease both; }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(5px); }
    to { opacity: 1; transform: none; }
  }
  .phead {
    font-size: 10.5px; font-weight: 700; color: var(--text3);
    padding: 11px 2px 5px; letter-spacing: 0.02em;
  }
  .nitem { border-bottom: 1px solid var(--border); }
  .nitem > summary {
    display: grid; grid-template-columns: 22px 1fr auto; gap: 7px; align-items: start;
    padding: 11px 2px; cursor: pointer; list-style: none;
    transition: opacity 0.15s ease;
  }
  .nitem > summary::-webkit-details-marker { display: none; }
  .nitem > summary:active { opacity: 0.7; }
  .nidx {
    font-family: var(--mono); font-size: 9px; font-weight: 700; color: var(--text3);
    text-align: center; line-height: 16px; height: 16px; margin-top: 2px;
    border-radius: 5px; background: var(--bg-subtle);
    font-variant-numeric: tabular-nums;
  }
  .nitem[data-score="5"] .nidx { color: var(--amber); background: color-mix(in srgb, var(--amber) 14%, transparent); }
  .ntitle {
    font-size: 13.5px; font-weight: 600; line-height: 1.52; letter-spacing: -0.005em;
    text-wrap: pretty;
  }
  .nitem[open] .ntitle { color: var(--accent); }
  .nside { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; padding-top: 3px; }
  .nstars { font-size: 8px; letter-spacing: -0.4px; color: var(--amber); opacity: 0.9; }
  .nsent { font-size: 9.5px; font-weight: 800; line-height: 1; }
  .bull { color: var(--up-red); }
  .bear { color: var(--up-green); }
  .neu { color: var(--text3); }
  .nbody { padding: 0 2px 13px 29px; animation: fadeUp 0.24s ease both; }
  .ntag { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 7px; }
  .ntag span {
    font-size: 9.5px; font-weight: 600; padding: 1.5px 7px; border-radius: 5px;
    background: var(--bg-subtle); color: var(--text2); letter-spacing: 0.01em;
  }
  .nfact { font-size: 12.5px; line-height: 1.76; color: var(--text2); margin-bottom: 8px; }
  .nimpact {
    font-size: 12px; line-height: 1.68; color: var(--text);
    background: var(--bg-subtle); border-left: 2px solid var(--accent);
    padding: 7px 10px; border-radius: 0 8px 8px 0; margin-bottom: 9px;
  }
  .nimpact b {
    color: var(--accent); font-size: 9.5px; font-weight: 800;
    margin-right: 6px; letter-spacing: 0.04em;
  }
  .nfoot { display: flex; justify-content: flex-end; font-size: 11px; }
  .nfoot a { color: var(--accent); font-weight: 700; text-decoration: none; }
  .nfoot .nolink { color: var(--text3); }
  .empty { padding: 24px 8px; text-align: center; font-size: 12px; color: var(--text3); }

  footer {
    margin-top: 20px; padding: 14px 0 8px;
    border-top: 1px solid var(--border);
    text-align: center; font-size: 10px; line-height: 1.9; color: var(--text3);
  }
  footer b { color: var(--text2); font-weight: 600; }

  @media (min-width: 700px) {
    .hero h1 { font-size: 25px; }
    .mkt { gap: 10px; }
    .mrow { padding: 5px 0; }
    .mname { font-size: 12.5px; }
    .mpct { font-size: 11.5px; }
    .ntitle { font-size: 14.5px; }
    .nfact { font-size: 13px; }
  }
  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }
</style>
</head>
<body>
<div class="pg" id="pg"></div>

<header class="hdr">
  <div class="wrap hdrin">
    <div class="hl">
      <span class="dot"></span>
      <span class="hdate">__DATE_SHORT__</span>
      <span class="epill">__EDITION_BADGE_SHORT__</span>
    </div>
    <div class="hr">
      __ARCHIVE_SELECT__
      <button class="icon" id="tg" type="button" aria-label="切换深浅主题">☀️</button>
    </div>
  </div>
</header>

<main class="wrap">
  <section class="hero">
    <div class="kicker">__KICKER__</div>
    <h1>__MOOD_HEADLINE__</h1>
    <div class="hmeta">
      <span class="mood __MOOD_CLASS__">__MOOD_LABEL__</span>
      <span>__WEEKDAY__ __GEN_TIME__ CST</span>
    </div>
  </section>

  <section class="mkt">__MARKET_TILES__</section>
  __MUST_READ__

  <details class="ov" open>
    <summary><span>🧭 __OVERVIEW_TITLE__</span><span class="ovh"></span></summary>
    <div class="ovtext">__OVERVIEW__</div>
  </details>

  <div class="tabs" role="tablist">__TABS__</div>
  __PANELS__

  <footer>
    <div>官方一手 · <b>美联储 / SEC / 欧洲央行</b></div>
    <div><b>彭博 · FT · 路透 · WSJ · MarketWatch · CNBC · Investing · 日经亚洲 · 南华早报</b></div>
    <div>国内快讯 · <b>财联社 · 新浪财经 · 东方财富 · 同花顺</b></div>
    <div style="margin-top:5px">行情 __DATA_NOTE__ · 早 08:28 / 收 15:28 / 晚 21:58</div>
  </footer>
</main>

<script>
  (function () {
    var root = document.documentElement;
    var tg = document.getElementById("tg");
    function apply(t) {
      root.setAttribute("data-theme", t);
      tg.textContent = t === "dark" ? "☀️" : "🌙";
    }
    apply(localStorage.getItem("brief-theme") || "dark");
    tg.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      localStorage.setItem("brief-theme", next);
      apply(next);
    });

    var pg = document.getElementById("pg");
    window.addEventListener("scroll", function () {
      var top = window.scrollY || document.documentElement.scrollTop;
      var h = document.documentElement.scrollHeight - window.innerHeight;
      pg.style.width = (h > 0 ? Math.min(100, (top / h) * 100) : 0) + "%";
    }, { passive: true });

    var tabs = [].slice.call(document.querySelectorAll(".tab"));
    var panels = [].slice.call(document.querySelectorAll(".panel"));
    function pick(key) {
      tabs.forEach(function (t) { t.classList.toggle("on", t.dataset.tab === key); });
      panels.forEach(function (p) { p.classList.toggle("on", p.dataset.panel === key); });
    }
    tabs.forEach(function (t) {
      t.addEventListener("click", function () { pick(t.dataset.tab); });
    });

    // 必读 chip / 外部锚点：自动切到对应 Tab 并展开该条
    function jumpToHash() {
      var hash = window.location.hash;
      if (!hash || hash.length < 2) return;
      var el;
      try { el = document.querySelector(hash); } catch (e) { return; }
      if (!el) return;
      var panel = el.closest(".panel");
      if (panel) pick(panel.dataset.panel);
      if (el.tagName === "DETAILS") el.setAttribute("open", "");
      el.scrollIntoView({ block: "center" });
    }
    window.addEventListener("hashchange", jumpToHash);
    jumpToHash();
  })();
</script>
</body>
</html>
"""


def render_page_html(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data, archive_ids, is_archive=False):
    meta = get_edition_meta(edition)
    date_str = now.strftime("%Y-%m-%d")
    edition_id = f"{date_str}-{edition}"
    date_full = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood = llm_data.get("mood", "neutral")
    mood_map = {
        "bullish": ("mood-bullish", "🔥 风险偏好回升"),
        "bearish": ("mood-bearish", "❄️ 市场情绪偏谨慎"),
        "neutral": ("mood-neutral", "⚖️ 震荡分化格局"),
    }
    mood_cls, mood_label = mood_map.get(mood, ("mood-neutral", f"📊 每日{meta['name']}"))

    us_news = llm_data.get("us_global_news", [])
    cn_news = llm_data.get("cn_a_news", [])
    gen_news = llm_data.get("general_news", [])

    if edition == "close":
        groups = [
            ("🇨🇳 A股/港股", a_idx, False, 4),
            ("🌍 大宗外汇", macro_assets, True, 4),
            ("🇺🇸 美股指数", us_idx, True, 4),
            ("🚀 科技龙头", tech_stocks, True, 4),
        ]
        overview_title = "A 股收盘全景复盘"
        kicker = "CLOSING BELL · 收盘复盘"
    elif edition == "pm":
        groups = [
            ("🇺🇸 美股指数", us_idx, True, 4),
            ("🚀 科技龙头", tech_stocks, True, 4),
            ("🌍 大宗外汇", macro_assets, True, 4),
            ("🇨🇳 A股/港股", a_idx, False, 4),
        ]
        overview_title = "晚间美股前瞻与跨市场总览"
        kicker = "EVENING BRIEF · 晚报前瞻"
    else:
        groups = [
            ("🇺🇸 美股指数", us_idx, True, 4),
            ("🚀 科技龙头", tech_stocks, True, 4),
            ("🇨🇳 A股/港股", a_idx, False, 4),
            ("🌍 大宗外汇", macro_assets, True, 4),
        ]
        overview_title = "今日早报主编全景总览"
        kicker = "MORNING BRIEF · 早报总览"

    market_tiles = "".join(
        render_market_group_html(title, data, is_us=is_us, preview=preview)
        for title, data, is_us, preview in groups
    )
    tabs_html, panels_html = render_news_sections_html(us_news, cn_news, gen_news, edition)

    # 把实际使用的行情源写进页脚，便于核对数据来源
    sources = sorted({
        item.get("source", "")
        for group in (us_idx, tech_stocks, a_idx, macro_assets)
        for item in group
        if item.get("source")
    })
    data_note = " / ".join(sources) if sources else "多源交叉校验"

    replacements = {
        "__EDITION_CODE__": edition,
        "__FULL_NAME__": esc(meta["full_name"]),
        "__EDITION_BADGE_SHORT__": esc(f"{meta['icon']}{meta['name']}"),
        "__DATE_STR__": esc(date_str),
        "__DATE_SHORT__": esc(now.strftime("%m-%d")) + " " + esc(weekday),
        "__WEEKDAY__": esc(weekday),
        "__GEN_TIME__": esc(now.strftime("%H:%M")),
        "__MOOD_CLASS__": mood_cls,
        "__MOOD_LABEL__": esc(mood_label),
        "__MOOD_HEADLINE__": esc(llm_data.get("mood_headline") or f"{date_full} {meta['full_name']}"),
        "__ARCHIVE_SELECT__": build_archive_select_html(archive_ids, edition_id, is_archive=is_archive),
        "__MARKET_TILES__": market_tiles,
        "__MUST_READ__": render_must_read_html(llm_data, edition),
        "__OVERVIEW_TITLE__": esc(overview_title),
        "__OVERVIEW__": esc(llm_data.get("overview", "")),
        "__TABS__": tabs_html,
        "__PANELS__": panels_html,
        "__DATA_NOTE__": esc(data_note),
        "__KICKER__": esc(kicker),
    }
    page = HTML_TEMPLATE
    for k, v in replacements.items():
        page = page.replace(k, v)
    return page


def save_web_pages(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data):
    """生成 docs/index.html 与独立的 docs/archive/YYYY-MM-DD-am|close|pm.html"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    edition_id = f"{now.strftime('%Y-%m-%d')}-{edition}"
    manifest_file = ARCHIVE_DIR / "manifest.json"
    entries = []
    if manifest_file.exists():
        try:
            entries = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            entries = []

    if edition_id not in entries:
        entries.append(edition_id)
    # 严格按日期 + (pm > close > am) 倒序排列，保留最近 180 期（约 60 天）
    entries = sorted(set(entries), key=archive_sort_key, reverse=True)[:180]
    manifest_file.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    # manifest 只保留 180 期，但归档 HTML 以前只增不删，会攒下一堆没人链接的孤儿文件。
    # 这里顺带清理；keep 太小时（manifest 损坏/首次运行）不执行，避免误删整目录。
    keep = set(entries)
    if len(keep) >= 5:
        removed = 0
        for old in ARCHIVE_DIR.glob("*.html"):
            if old.stem not in keep:
                try:
                    old.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"[归档] 已清理 {removed} 个超出保留期的旧页面（保留 {len(keep)} 期）")

    index_html = render_page_html(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data, entries, is_archive=False)
    archive_html = render_page_html(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data, entries, is_archive=True)

    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")
    (ARCHIVE_DIR / f"{edition_id}.html").write_text(archive_html, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"已生成网页：docs/index.html 与 docs/archive/{edition_id}.html")
    return edition_id


# ================= 飞书精简入口卡片 =================
def format_market_md(data, is_us=False, limit=4):
    """飞书卡片内的行情小表：标的变多后只展示前 limit 项，完整版看网页"""
    if not data:
        return "<font color='grey'>暂无数据</font>"
    lines = []
    for item in data[:limit]:
        pct = item["pct"]
        sign = "+" if pct > 0 else ""
        if is_us:
            color = "green" if pct > 0 else ("red" if pct < 0 else "grey")
        else:
            color = "red" if pct > 0 else ("green" if pct < 0 else "grey")
        lines.append(f"**{item['name']}** `{item['price']}` <font color='{color}'>{sign}{pct:.2f}%</font>")
    if len(data) > limit:
        lines.append(f"<font color='grey'>其余 {len(data) - limit} 项见网页 ↗</font>")
    return "\n".join(lines)


def build_feishu_summary_card(now, edition, edition_id, us_idx, tech_stocks, a_idx, macro_assets, llm_data, site_url):
    """构建早/收盘/晚报专属飞书精简入口卡片（直链独立归档页）"""
    meta = get_edition_meta(edition)
    today_str = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood_map = {
        "bullish": ("red", "🔥 风险偏好回升"),
        "bearish": ("green", "❄️ 市场情绪偏谨慎"),
        "neutral": (meta["feishu_neutral_color"], "⚖️ 震荡分化格局"),
    }
    header_color, mood_label = mood_map.get(llm_data.get("mood", "neutral"), (meta["feishu_neutral_color"], f"📊 每日{meta['name']}"))

    merged = []
    for key in ("us_global_news", "cn_a_news", "general_news"):
        for item in llm_data.get(key, []):
            merged.append(item)
    merged.sort(key=lambda x: -safe_score(x.get("score", 3)))

    preview_lines = []
    for item in merged[:5]:
        sc = safe_score(item.get("score", 3))
        prefix = "🔥" if sc >= 5 else "•"
        preview_lines.append(f"{prefix} **[{item.get('tag', '要闻')}]** {item.get('title', '')}")
    preview_md = "\n".join(preview_lines)

    if edition == "close":
        row1_left = f"🇨🇳 **A 股今日收盘**\n{format_market_md(a_idx, is_us=False)}"
        row1_right = f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}"
        row2_left = f"🇺🇸 **美股上日收盘**\n{format_market_md(us_idx, is_us=True)}"
        row2_right = f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}"
    elif edition == "pm":
        row1_left = f"🇺🇸 **美股指数**\n{format_market_md(us_idx, is_us=True)}"
        row1_right = f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}"
        row2_left = f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}"
        row2_right = f"🇨🇳 **A 股今日收盘**\n{format_market_md(a_idx, is_us=False)}"
    else:
        row1_left = f"🇺🇸 **隔夜美股**\n{format_market_md(us_idx, is_us=True)}"
        row1_right = f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}"
        row2_left = f"🇨🇳 **A 股收盘**\n{format_market_md(a_idx, is_us=False)}"
        row2_right = f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}"

    elements = [
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{"tag": "markdown", "content": row1_left}]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{"tag": "markdown", "content": row1_right}]},
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{"tag": "markdown", "content": row2_left}]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{"tag": "markdown", "content": row2_right}]},
            ],
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"🧭 **{llm_data.get('mood_headline', '本期核心总览')}**  ·  {mood_label}\n\n{llm_data.get('overview', '')}",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"📌 **本期重磅 Top 5 抢先看**\n{preview_md}",
        },
    ]

    if site_url:
        target_url = f"{site_url}archive/{edition_id}.html"
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"📖 打开旗舰网页版{meta['name']}（含走势图与筛选）"},
                    "type": "primary",
                    "url": target_url,
                }
            ],
        })

    # 注脚始终包含“早报”，兼容飞书机器人已配置的“早报”安全关键词
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"官方一手：美联储/SEC/欧洲央行 · 财经：彭博/FT/路透/WSJ/CNBC · 国内：财联社/新浪/东财/同花顺 | {now.strftime('%H:%M')}",
            }
        ],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_color,
                "title": {"tag": "plain_text", "content": f"{meta['icon']} {meta['full_name']} | {today_str} {weekday}"},
            },
            "elements": elements,
        },
    }


def run_generate(edition_arg="auto"):
    now = datetime.datetime.now(BJ_TZ)
    edition = resolve_edition(edition_arg, now)
    meta = get_edition_meta(edition)
    print(f"=== 开始生成【{meta['full_name']}】({edition}) ===")

    print("1/3 正在抓取跨市场精准行情与 7 日走势...")
    us_idx = fetch_yf_group(US_INDEX_TARGETS)
    tech_stocks = fetch_yf_group(US_STOCK_TARGETS)
    macro_assets = fetch_yf_group(MACRO_TARGETS)
    a_idx = get_a_market()
    print(
        f"[行情] 指数 {len(a_idx)}/{len(A_INDEX_TARGETS)} · 美股 {len(us_idx)}/{len(US_INDEX_TARGETS)}"
        f" · 个股 {len(tech_stocks)}/{len(US_STOCK_TARGETS)} · 大宗 {len(macro_assets)}/{len(MACRO_TARGETS)}"
    )

    print(f"2/3 正在抓取过去 {meta['rss_hours']} 小时机构级信源...")
    official_raw = fetch_rss_within_hours(OFFICIAL_RSS, hours=max(48, meta["rss_hours"]), max_per_feed=3)
    global_fin_raw = fetch_rss_within_hours(GLOBAL_FINANCE_RSS, hours=meta["rss_hours"], max_per_feed=5)
    general_raw = fetch_rss_within_hours(GENERAL_NEWS_RSS, hours=meta["rss_hours"], max_per_feed=4)
    cn_fin_raw = fetch_cn_flash(limit=90)

    print("3/3 正在调用大模型评级排序并渲染网页...")
    llm_data = analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw, edition, official_raw)
    edition_id = save_web_pages(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data)

    site_url = get_site_url()
    card_payload = build_feishu_summary_card(
        now, edition, edition_id, us_idx, tech_stocks, a_idx, macro_assets, llm_data, site_url
    )
    CARD_CACHE_FILE.write_text(json.dumps(card_payload, ensure_ascii=False), encoding="utf-8")
    print(f"网页永久链接已绑定为: {site_url}archive/{edition_id}.html")


def run_notify():
    if not FEISHU_WEBHOOK:
        raise RuntimeError("缺少 FEISHU_WEBHOOK 环境变量")
    if not CARD_CACHE_FILE.exists():
        raise RuntimeError("未找到待发送的卡片缓存文件")
    card_payload = json.loads(CARD_CACHE_FILE.read_text(encoding="utf-8"))
    resp = requests.post(FEISHU_WEBHOOK, json=card_payload, timeout=15)
    print("飞书响应:", resp.text)
    res_json = resp.json()
    if resp.status_code != 200 or (res_json.get("code", 0) != 0 and res_json.get("StatusCode", 0) != 0):
        raise RuntimeError(f"飞书推送失败: {resp.text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--edition", choices=["auto", "am", "close", "pm"], default="auto")
    args = parser.parse_args()

    if args.generate:
        run_generate(args.edition)
    elif args.notify:
        run_notify()
    else:
        run_generate(args.edition)
        run_notify()
