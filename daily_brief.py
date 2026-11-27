import os
import re
import math
import json
import html
import argparse
import calendar
import datetime
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

# 顶级全球财经一手信源
GLOBAL_FINANCE_RSS = {
    "彭博市场": [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://news.google.com/rss/search?q=when:24h+site:bloomberg.com/news/articles&hl=en-US&gl=US&ceid=US:en",
    ],
    "彭博宏观": [
        "https://feeds.bloomberg.com/economics/news.rss",
    ],
    "金融时报FT": [
        "https://www.ft.com/markets?format=rss",
        "https://www.ft.com/global-economy?format=rss",
    ],
    "路透商业": [
        "https://news.google.com/rss/search?q=site:reuters.com/business+when:1d&hl=en-US&gl=US&ceid=US:en",
    ],
    "WSJ市场": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    ],
    "CNBC财经": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    ],
}

# 顶级科技与国际时政信源
GENERAL_NEWS_RSS = {
    "彭博科技": [
        "https://feeds.bloomberg.com/technology/news.rss",
    ],
    "路透国际": [
        "https://news.google.com/rss/search?q=site:reuters.com/world+when:1d&hl=en-US&gl=US&ceid=US:en",
    ],
    "BBC中文": [
        "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    ],
    "CNBC科技": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
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
    这里改成：日线与实时价各算一次涨跌幅，两者一致才用实时价，否则回退日线并打日志。
    """
    closes, last_date = [], None
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
                    last_date = getattr(ts, "date", lambda: None)()
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
        price, prev = closes[-1], closes[-2]
        series_pct = (price - prev) / prev * 100
        if live and live_prev:
            live_pct = (live - live_prev) / live_prev * 100
            if abs(live_pct - series_pct) <= 0.35:
                price, prev = live, live_prev
                hist_vals = closes + [live]
                as_of = datetime.datetime.now(BJ_TZ).strftime("%m-%d %H:%M 实时")
            else:
                print(
                    f"[行情] {name}({symbol}) 实时价与日线偏离过大"
                    f"({live_pct:+.2f}% vs {series_pct:+.2f}%)，采用日线收盘口径"
                )
    elif live and live_prev:
        price, prev = live, live_prev
        hist_vals = [live_prev, live]
        as_of = datetime.datetime.now(BJ_TZ).strftime("%m-%d %H:%M 实时")

    if price is None:
        return None
    stale_days = 0
    if last_date:
        stale_days = (datetime.datetime.now(BJ_TZ).date() - last_date).days
    if stale_days > 5:
        print(f"[行情] {name}({symbol}) 数据最后更新于 {last_date}，已陈旧 {stale_days} 天")
    return _build_quote(name, symbol, price, prev, hist_vals, as_of)


def fetch_yf_group(tickers_dict):
    """批量获取 Yahoo 行情，单只失败不影响其余标的"""
    results = []
    for name, symbol in tickers_dict.items():
        quote = _yf_quote(name, symbol)
        if quote:
            results.append(quote)
        else:
            print(f"[行情] {name}({symbol}) 未获取到有效数值")
    return results


A_INDEX_TARGETS = [
    ("上证指数", "sh000001", "000001.SS"),
    ("深证成指", "sz399001", "399001.SZ"),
    ("创业板指", "sz399006", "399006.SZ"),
    ("沪深300", "sh000300", "000300.SS"),
]


def _a_session_label():
    """区分 A 股是盘中实时还是收盘快照，避免早报把昨收标成「实时」"""
    now = datetime.datetime.now(BJ_TZ)
    trading = now.weekday() < 5 and (9, 25) <= (now.hour, now.minute) <= (15, 5)
    return "东财实时" if trading else "东财收盘"


def _em_index_history(symbol):
    """东方财富指数日线：与实时价同源，避免用 Yahoo 走势拼东财价格造成曲线失真"""
    try:
        df = ak.stock_zh_index_daily_em(symbol=symbol)
        if df is None or df.empty:
            return []
        col = "close" if "close" in df.columns else df.columns[-1]
        vals = [_safe_float(v) for v in df[col].tolist()]
        return [v for v in vals if v][-8:]
    except Exception as e:
        print(f"[行情] 东财日线 {symbol} 获取失败: {e}")
        return []


def _em_index_spot_df():
    """东方财富沪深指数快照，兼容 akshare 1.20.44 的接口改名。

    旧名 stock_zh_index_spot_em 已被移除，继续调用会抛异常；旧代码把异常吞掉后
    整体切到 Yahoo，导致 A 股指数长期用的是延迟/隔夜的 Yahoo 数据。
    """
    attempts = []
    if hasattr(ak, "index_zh_spot_em"):
        attempts += [
            ("index_zh_spot_em(symbol=沪深重要指数)", lambda: ak.index_zh_spot_em(symbol="沪深重要指数")),
            ("index_zh_spot_em()", lambda: ak.index_zh_spot_em()),
        ]
    if hasattr(ak, "stock_zh_index_spot_em"):
        attempts.append(("stock_zh_index_spot_em()", lambda: ak.stock_zh_index_spot_em()))

    for label, call in attempts:
        try:
            df = call()
            if df is not None and not df.empty:
                print(f"[行情] 指数快照来源: {label}（{len(df)} 行）")
                return df
        except Exception as e:
            print(f"[行情] {label} 调用失败: {type(e).__name__}: {e}")
    return None


def _match_index_row(df, name, code6):
    """优先按名称精确匹配，失败再按 6 位代码匹配，兼容列名或命名变化"""
    if "名称" in df.columns:
        hit = df[df["名称"].astype(str).str.strip() == name]
        if not hit.empty:
            return hit.iloc[0]
    for code_col in ("代码", "指数代码", "symbol"):
        if code_col in df.columns:
            codes = df[code_col].astype(str).str.strip()
            hit = df[codes.str.replace(r"^(sh|sz)", "", regex=True) == code6]
            if not hit.empty:
                return hit.iloc[0]
    return None


def get_a_market():
    """A 股核心指数：东方财富为主源，涨跌幅与昨收交叉校验，缺失时逐只补 Yahoo。

    修掉旧实现的三个问题：
    1. 接口改名后异常被吞、整体换成 Yahoo → 改为兼容新旧接口名；
    2. 只要有一只缺失就丢弃全部东财结果 → 改为逐只补齐；
    3. 用 Yahoo 走势拼接东财价格 → 改为走势与价格同源（东财日线）。
    """
    quotes = {}
    df = _em_index_spot_df()

    if df is not None:
        for name, em_sym, yf_sym in A_INDEX_TARGETS:
            code6 = re.sub(r"^(sh|sz)", "", em_sym)
            try:
                row = _match_index_row(df, name, code6)
            except Exception as e:
                print(f"[行情] {name} 行匹配异常: {e}")
                row = None
            if row is None:
                print(f"[行情] 东方财富未返回 {name}({code6})，稍后用备用源补齐")
                continue

            price = _safe_float(row.get("最新价"))
            pct_field = _safe_float(row.get("涨跌幅"))
            prev_raw = _safe_float(row.get("昨收"))

            # 展示口径以东财官方涨跌幅为准，再由它反推昨收，保证价格与涨跌幅自洽
            prev = None
            if price and pct_field is not None and pct_field > -100:
                prev = price / (1 + pct_field / 100)
                if prev_raw and abs((price - prev_raw) / prev_raw * 100 - pct_field) > 0.15:
                    print(
                        f"[行情] {name} 昨收字段({prev_raw:.2f})与涨跌幅({pct_field:+.2f}%)"
                        f"不一致，已按涨跌幅反推昨收"
                    )
            else:
                prev = prev_raw
                chg = _safe_float(row.get("涨跌额"))
                if not prev and price and chg is not None:
                    prev = price - chg

            hist = _em_index_history(em_sym)
            if hist and price:
                hist = hist[:-1] + [price]

            quote = _build_quote(name, yf_sym, price, prev, hist, _a_session_label(), "东方财富")
            if quote:
                quotes[name] = quote
            else:
                print(f"[行情] {name} 东财数值不合理，稍后用备用源补齐")
    else:
        print("[行情] 东方财富指数接口不可用，改用 Yahoo 备用源")

    if len(quotes) < len(A_INDEX_TARGETS):
        for name, _em_sym, yf_sym in A_INDEX_TARGETS:
            if name in quotes:
                continue
            quote = _yf_quote(name, yf_sym)
            if quote:
                quote["source"] = "Yahoo 备用"
                quotes[name] = quote

    return [quotes[n] for n, _e, _y in A_INDEX_TARGETS if n in quotes]



def fetch_rss_within_hours(rss_dict, hours=12, max_per_feed=7):
    """抓取指定时间窗口内的顶级 RSS 信源并按时间倒序排列（若条目过少自动放宽至 24h）"""
    def _collect(window_hours):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now_utc - datetime.timedelta(hours=window_hours)
        all_lines = []

        for source_name, urls in rss_dict.items():
            if isinstance(urls, str):
                urls = [urls]

            parsed_items = []
            for url in urls:
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=12)
                    resp.raise_for_status()
                    feed = feedparser.parse(resp.content)
                    for entry in feed.entries:
                        time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                        if time_struct:
                            pub_dt = datetime.datetime.fromtimestamp(
                                calendar.timegm(time_struct), tz=datetime.timezone.utc
                            )
                            if pub_dt < cutoff:
                                continue
                            pub_str = pub_dt.astimezone(BJ_TZ).strftime("%m-%d %H:%M")
                        else:
                            pub_dt = now_utc - datetime.timedelta(hours=window_hours / 2)
                            pub_str = f"{window_hours}h内"

                        title = clean_html(entry.get("title", ""))
                        summary = clean_html(entry.get("summary", entry.get("description", "")))[:240]
                        link = entry.get("link", "").strip()
                        if title:
                            parsed_items.append((pub_dt, pub_str, title, summary, link))

                    if parsed_items:
                        break
                except Exception as e:
                    print(f"[RSS] {source_name} 抓取失败: {e}")

            parsed_items.sort(key=lambda x: x[0], reverse=True)
            for _, pub_str, title, summary, link in parsed_items[:max_per_feed]:
                all_lines.append(
                    f"[{source_name} | {pub_str}] 标题:{title} | 摘要:{summary} | 链接:{link}"
                )
        return all_lines

    lines = _collect(hours)
    if len(lines) < 8 and hours < 24:
        print(f"[RSS] {hours}h 内条目较少({len(lines)}条)，自动放宽至 24h 补足...")
        lines = _collect(24)
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
    """抓取财联社 7x24 实时电报（接口名与列名都做兼容映射，避免改名后静默变成空数据）"""
    df_cls = _call_cls_api()
    if df_cls is None:
        print("[财联社] 无可用接口，本时段国内快讯缺省")
        return ""

    title_col = _pick_col(df_cls, ["标题", "title"])
    content_col = _pick_col(df_cls, ["内容", "正文", "摘要", "content"])
    date_col = _pick_col(df_cls, ["发布日期", "日期", "date"])
    time_col = _pick_col(df_cls, ["发布时间", "时间", "time"])

    lines = []
    for _, row in df_cls.head(limit).iterrows():
        title = _cell(row, title_col)
        content = _cell(row, content_col)
        if not (title or content):
            continue
        ts = f"{_cell(row, date_col)[-5:]} {_cell(row, time_col)[:5]}".strip()
        lines.append(f"[财联社 | {ts}] 标题:{title} | 正文:{content[:260]}")
    if not lines:
        print(f"[财联社] 接口返回 0 条有效快讯（列名: {list(df_cls.columns)[:8]}）")
    return "\n".join(lines)


def analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw, edition):
    """按早报/收盘报/晚报差异化定位，调用大模型去重、精译与重磅评级"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
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

请基于以下顶级权威信源（彭博社、金融时报、路透社、WSJ、CNBC、财联社、BBC），合并去重同类事件，将英文报道精准翻译为专业中文，并按【重磅程度】严格评分排序。

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
      "source": "来源与时间（如 财联社 · 15:10）"
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

【全球与美股顶级财经信源】：
{global_fin_raw if global_fin_raw else "（暂无数据）"}

【国内财联社 7x24 实时电报】：
{cn_fin_raw if cn_fin_raw else "（暂无数据）"}

【国际时政与前沿科技信源】：
{general_raw if general_raw else "（暂无数据）"}
"""
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    content = resp.choices[0].message.content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    data = json.loads(content)
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


def render_market_group_html(title, data, is_us=True):
    """紧凑行情组卡：每行「名称 + 价格 / 涨跌幅」，7 日走势线作为右侧淡背景"""
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
        rows.append('<div class="mrow"><span class="mname" style="color:var(--text-muted)">暂无数据</span></div>')
    note = _as_of_label(data)
    note_html = f'<span class="gtime">{esc(note)}</span>' if note else ""
    return (
        f'<div class="gcard">'
        f'  <div class="gcap"><span>{esc(title)}</span>{note_html}</div>'
        f'  {"".join(rows)}'
        f'</div>'
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
        if eid == entry_ids[0]:
            label += " (最新)"
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
    --bg: #070b14;
    --bg-card: #0e1626;
    --bg-subtle: rgba(255, 255, 255, 0.04);
    --border: rgba(148, 163, 184, 0.14);
    --text: #f1f5f9;
    --text2: #94a3b8;
    --text3: #64748b;
    --accent: #38bdf8;
    --accent-soft: rgba(56, 189, 248, 0.12);
    --edition-bg: rgba(56, 189, 248, 0.15);
    --edition-color: #7dd3fc;
    --up-red: #f43f5e;
    --up-green: #10b981;
    --amber: #f59e0b;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
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
    --bg: #f2f4f8;
    --bg-card: #ffffff;
    --bg-subtle: #f5f7fa;
    --border: rgba(15, 23, 42, 0.09);
    --text: #0f172a;
    --text2: #475569;
    --text3: #7b8a9e;
    --accent: #0284c7;
    --accent-soft: rgba(2, 132, 199, 0.1);
    --edition-bg: rgba(2, 132, 199, 0.11);
    --edition-color: #0369a1;
    --up-red: #e11d48;
    --up-green: #059669;
    --amber: #d97706;
  }
  [data-theme="light"][data-edition="close"] {
    --accent: #d97706;
    --accent-soft: rgba(217, 119, 6, 0.11);
    --edition-bg: rgba(217, 119, 6, 0.12);
    --edition-color: #b45309;
  }
  [data-theme="light"][data-edition="pm"] {
    --accent: #4f46e5;
    --accent-soft: rgba(79, 70, 229, 0.1);
    --edition-bg: rgba(79, 70, 229, 0.12);
    --edition-color: #4338ca;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
    -webkit-text-size-adjust: 100%;
    padding-bottom: 8px;
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 0 14px; }
  .pg {
    position: fixed; top: 0; left: 0;
    height: 2px; width: 0;
    background: linear-gradient(90deg, var(--accent), #818cf8);
    z-index: 99; transition: width 0.1s linear;
  }

  /* ---------- 顶栏 ---------- */
  .hdr {
    position: sticky; top: 0; z-index: 60;
    background: color-mix(in srgb, var(--bg) 84%, transparent);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--border);
  }
  .hdrin { height: 46px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .hl { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex: 0 0 auto; }
  .hdate { font-family: var(--mono); font-size: 12px; color: var(--text2); white-space: nowrap; }
  .epill {
    font-size: 11px; font-weight: 800; padding: 2px 8px; border-radius: 999px;
    background: var(--edition-bg); color: var(--edition-color); white-space: nowrap;
  }
  .hr { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
  select.arch {
    max-width: 104px; height: 26px;
    background: var(--bg-card); color: var(--text2);
    border: 1px solid var(--border); border-radius: 7px;
    font-size: 11px; padding: 0 2px;
  }
  button.icon {
    width: 26px; height: 26px; border-radius: 7px; cursor: pointer;
    background: var(--bg-card); color: var(--text2);
    border: 1px solid var(--border); font-size: 12px; line-height: 1;
  }

  /* ---------- 头版 ---------- */
  .hero { padding: 14px 0 10px; }
  .hero h1 {
    font-size: 19px; font-weight: 800; line-height: 1.45; letter-spacing: -0.2px;
    margin-bottom: 7px;
  }
  .hmeta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-family: var(--mono); font-size: 11px; color: var(--text3); }
  .mood { font-family: -apple-system, sans-serif; font-size: 11px; font-weight: 800; padding: 2px 8px; border-radius: 999px; }
  .mood-bullish { background: rgba(244, 63, 94, 0.14); color: var(--up-red); }
  .mood-bearish { background: rgba(16, 185, 129, 0.14); color: var(--up-green); }
  .mood-neutral { background: var(--accent-soft); color: var(--accent); }

  /* ---------- 行情 2×2 ---------- */
  .mkt { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 6px 0 10px; }
  .gcard {
    position: relative; overflow: hidden;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 11px; padding: 8px 9px 9px;
  }
  .gcap {
    display: flex; align-items: center; justify-content: space-between; gap: 4px;
    font-size: 10.5px; font-weight: 700; color: var(--text3);
    margin-bottom: 4px; letter-spacing: 0.2px;
  }
  .gcap > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gtime { font-family: var(--mono); font-weight: 500; font-size: 9.5px; opacity: 0.75; flex: 0 0 auto; }
  .mrow {
    position: relative;
    display: flex; align-items: center; justify-content: space-between; gap: 4px;
    padding: 3.5px 0; border-bottom: 1px dashed var(--border);
  }
  .mrow:last-child { border-bottom: none; padding-bottom: 0; }
  .mrow .sparkline {
    position: absolute; right: -2px; bottom: 1px;
    width: 44px; height: 15px; opacity: 0.3; pointer-events: none;
  }
  .mname {
    display: flex; flex-direction: column; min-width: 0;
    font-size: 12px; font-weight: 600; line-height: 1.25;
  }
  .mname em {
    font-style: normal; font-family: var(--mono); font-size: 10px;
    color: var(--text3); font-variant-numeric: tabular-nums;
  }
  .mpct {
    position: relative; z-index: 1; flex: 0 0 auto;
    font-family: var(--mono); font-size: 12px; font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .pos-green, .neg-green { color: var(--up-green); }
  .pos-red, .neg-red { color: var(--up-red); }
  .flat { color: var(--text2); }

  /* ---------- 必读 chips ---------- */
  .chips { display: flex; gap: 6px; overflow-x: auto; padding: 2px 0 10px; scrollbar-width: none; }
  .chips::-webkit-scrollbar { display: none; }
  .chip {
    flex: 0 0 auto; max-width: 82vw;
    display: flex; align-items: center; gap: 6px;
    padding: 6px 9px; border-radius: 9px;
    background: var(--bg-card); border: 1px solid var(--border);
    color: var(--text); text-decoration: none; font-size: 12px; line-height: 1.4;
  }
  .chip b { flex: 0 0 auto; font-family: var(--mono); font-size: 10px; color: var(--amber); }
  .chip span { display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }
  .chip i { font-style: normal; color: var(--text3); font-size: 13px; }

  /* ---------- 综述 ---------- */
  details.ov {
    background: var(--bg-card); border: 1px solid var(--border);
    border-left: 3px solid var(--accent); border-radius: 10px;
    padding: 9px 11px; margin-bottom: 12px;
  }
  details.ov > summary {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    cursor: pointer; list-style: none;
    font-size: 11px; font-weight: 800; color: var(--accent); letter-spacing: 0.3px;
  }
  details.ov > summary::-webkit-details-marker { display: none; }
  .ovh { font-weight: 500; color: var(--text3); font-size: 10px; }
  .ovh::after { content: "展开"; }
  .ov[open] .ovh::after { content: "收起"; }
  .ovtext { font-size: 13px; line-height: 1.72; color: var(--text); margin-top: 7px; }
  .ov:not([open]) .ovtext { display: none; }

  /* ---------- Tab ---------- */
  .tabs {
    position: sticky; top: 46px; z-index: 50;
    display: flex; gap: 4px; padding: 6px 0;
    background: color-mix(in srgb, var(--bg) 90%, transparent);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
  }
  .tab {
    flex: 1; display: flex; align-items: center; justify-content: center; gap: 4px;
    padding: 6px 2px; border-radius: 8px; cursor: pointer;
    background: transparent; border: 1px solid transparent;
    color: var(--text2); font-size: 12.5px; font-weight: 700;
    font-family: inherit;
  }
  .tab em { font-style: normal; font-family: var(--mono); font-size: 10px; opacity: 0.7; }
  .tab.on { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 30%, transparent); }

  /* ---------- 面板 / 新闻条目 ---------- */
  .panel { display: none; padding-top: 4px; }
  .panel.on { display: block; }
  .phead { font-size: 11px; font-weight: 700; color: var(--text3); padding: 8px 2px 4px; }
  .nitem { border-bottom: 1px solid var(--border); }
  .nitem > summary {
    display: grid; grid-template-columns: 20px 1fr auto; gap: 6px; align-items: start;
    padding: 10px 2px; cursor: pointer; list-style: none;
  }
  .nitem > summary::-webkit-details-marker { display: none; }
  .nidx { font-family: var(--mono); font-size: 10px; color: var(--text3); padding-top: 3px; }
  .ntitle { font-size: 14px; font-weight: 600; line-height: 1.5; }
  .nitem[open] .ntitle { color: var(--accent); }
  .nside { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; padding-top: 3px; }
  .nstars { font-size: 8.5px; letter-spacing: -0.5px; color: var(--amber); }
  .nsent { font-size: 10px; font-weight: 800; }
  .bull { color: var(--up-red); }
  .bear { color: var(--up-green); }
  .neu { color: var(--text3); }
  .nbody { padding: 0 2px 12px 26px; }
  .ntag { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 6px; }
  .ntag span {
    font-size: 10px; padding: 1px 7px; border-radius: 5px;
    background: var(--bg-subtle); color: var(--text2);
  }
  .nfact { font-size: 13px; line-height: 1.72; color: var(--text2); margin-bottom: 7px; }
  .nimpact {
    font-size: 12.5px; line-height: 1.65; color: var(--text);
    background: var(--bg-subtle); border-left: 2px solid var(--accent);
    padding: 6px 9px; border-radius: 0 7px 7px 0; margin-bottom: 8px;
  }
  .nimpact b { color: var(--accent); font-size: 10px; font-weight: 800; margin-right: 5px; }
  .nfoot { display: flex; justify-content: flex-end; font-size: 11px; }
  .nfoot a { color: var(--accent); font-weight: 700; text-decoration: none; }
  .nfoot .nolink { color: var(--text3); }
  .empty { padding: 22px 8px; text-align: center; font-size: 12.5px; color: var(--text3); }

  footer {
    margin-top: 18px; padding: 14px 0 6px;
    border-top: 1px solid var(--border);
    text-align: center; font-size: 10.5px; line-height: 1.85; color: var(--text3);
  }

  @media (min-width: 700px) {
    .hero h1 { font-size: 24px; }
    .mkt { grid-template-columns: 1fr 1fr; gap: 10px; }
    .ntitle { font-size: 15px; }
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
    <div>Bloomberg · FT · Reuters · WSJ · CNBC · 财联社 · BBC</div>
    <div>行情 __DATA_NOTE__ · 早 08:30 / 收 15:30 / 晚 22:00</div>
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
            (meta["a_card_title"], a_idx, False),
            ("🌍 大宗 · 加密 · 汇率", macro_assets, True),
            (meta["us_card_title"], us_idx, True),
            ("🚀 美股科技", tech_stocks, True),
        ]
        overview_title = "A 股收盘全景复盘"
    elif edition == "pm":
        groups = [
            (meta["us_card_title"], us_idx, True),
            ("🚀 美股科技", tech_stocks, True),
            ("🌍 大宗 · 加密 · 汇率", macro_assets, True),
            (meta["a_card_title"], a_idx, False),
        ]
        overview_title = "晚间美股前瞻与跨市场总览"
    else:
        groups = [
            (meta["us_card_title"], us_idx, True),
            ("🚀 美股科技", tech_stocks, True),
            (meta["a_card_title"], a_idx, False),
            ("🌍 大宗 · 加密 · 汇率", macro_assets, True),
        ]
        overview_title = "今日早报主编全景总览"

    market_tiles = "".join(
        render_market_group_html(title, data, is_us=is_us) for title, data, is_us in groups
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

    index_html = render_page_html(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data, entries, is_archive=False)
    archive_html = render_page_html(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data, entries, is_archive=True)

    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")
    (ARCHIVE_DIR / f"{edition_id}.html").write_text(archive_html, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"已生成网页：docs/index.html 与 docs/archive/{edition_id}.html")
    return edition_id


# ================= 飞书精简入口卡片 =================
def format_market_md(data, is_us=False):
    if not data:
        return "<font color='grey'>暂无数据</font>"
    lines = []
    for item in data:
        pct = item["pct"]
        sign = "+" if pct > 0 else ""
        if is_us:
            color = "green" if pct > 0 else ("red" if pct < 0 else "grey")
        else:
            color = "red" if pct > 0 else ("green" if pct < 0 else "grey")
        lines.append(f"**{item['name']}** `{item['price']}` <font color='{color}'>{sign}{pct:.2f}%</font>")
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
                "content": f"早报/收盘报/晚报三推终端 · 信源：Bloomberg / FT / Reuters / WSJ / CNBC / 财联社 / BBC | {now.strftime('%H:%M')}",
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
    us_idx = fetch_yf_group({"纳斯达克": "^IXIC", "标普500": "^GSPC", "道琼斯": "^DJI", "费城半导体": "^SOX"})
    tech_stocks = fetch_yf_group({"英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT", "特斯拉": "TSLA"})
    macro_assets = fetch_yf_group({"黄金": "GC=F", "WTI原油": "CL=F", "BTC": "BTC-USD", "离岸RMB": "CNH=X"})
    a_idx = get_a_market()

    print(f"2/3 正在抓取过去 {meta['rss_hours']} 小时机构级信源...")
    global_fin_raw = fetch_rss_within_hours(GLOBAL_FINANCE_RSS, hours=meta["rss_hours"], max_per_feed=7)
    general_raw = fetch_rss_within_hours(GENERAL_NEWS_RSS, hours=meta["rss_hours"], max_per_feed=5)
    cn_fin_raw = fetch_cls_telegraph(limit=meta["cls_limit"])

    print("3/3 正在调用大模型评级排序并渲染网页...")
    llm_data = analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw, edition)
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
