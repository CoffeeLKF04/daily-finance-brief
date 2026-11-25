import os
import sys
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
    """确定当前是早报(am)还是晚报(pm)：北京时间 15:00 前为早报，之后为晚报"""
    if edition_arg in ("am", "pm"):
        return edition_arg
    return "am" if now.hour < 15 else "pm"


def get_edition_meta(edition):
    if edition == "pm":
        return {
            "code": "pm",
            "name": "晚报",
            "full_name": "全球财经与热点晚报",
            "icon": "🌙",
            "badge_text": "🌙 晚报版 · 日终复盘与美股前瞻",
            "rss_hours": 14,
            "us_card_title": "🇺🇸 美股最新 / 盘面",
            "a_card_title": "🇨🇳 A 股今日收盘",
        }
    return {
        "code": "am",
        "name": "早报",
        "full_name": "全球财经与热点早报",
        "icon": "☕",
        "badge_text": "☀️ 早报版 · 隔夜美股与盘前催化",
        "rss_hours": 20,
        "us_card_title": "🇺🇸 隔夜美股指数",
        "a_card_title": "🇨🇳 A 股上日收盘",
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


def format_archive_label(entry_id):
    if entry_id.endswith("-am"):
        return f"{entry_id[:-3]} ☀️早报"
    if entry_id.endswith("-pm"):
        return f"{entry_id[:-3]} 🌙晚报"
    return entry_id


def fetch_yf_group(tickers_dict):
    """批量获取 Yahoo Finance 行情及 7 日走势序列（过滤 NaN + fast_info 兜底）"""
    results = []
    for name, symbol in tickers_dict.items():
        close, prev = None, None
        closes = []
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="12d")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                closes = [
                    float(x)
                    for x in hist["Close"].dropna().tolist()
                    if math.isfinite(float(x))
                ]
                if len(closes) >= 2:
                    close, prev = closes[-1], closes[-2]

            if close is None or prev is None or prev == 0:
                info = ticker.fast_info
                lp = info.get("lastPrice") or info.get("last_price")
                pc = info.get("previousClose") or info.get("previous_close")
                if lp is not None and pc is not None:
                    lp, pc = float(lp), float(pc)
                    if math.isfinite(lp) and math.isfinite(pc) and pc != 0:
                        close, prev = lp, pc
                        if not closes:
                            closes = [pc, lp]

            if close is not None and prev is not None and prev != 0:
                pct = (close - prev) / prev * 100
                if math.isfinite(pct):
                    results.append({
                        "name": name,
                        "symbol": symbol,
                        "price": f"{close:,.2f}",
                        "pct": pct,
                        "history": closes[-7:],
                    })
                    continue
            print(f"[行情] {name}({symbol}) 未获取到有效数值")
        except Exception as e:
            print(f"[行情] {name}({symbol}) 获取失败: {e}")
    return results


def get_a_market():
    """获取 A 股核心指数（东方财富主源 + Yahoo 7日走势补全/兜底）"""
    targets = [
        ("上证指数", "000001.SS"),
        ("深证成指", "399001.SZ"),
        ("创业板指", "399006.SZ"),
        ("沪深300", "000300.SS"),
    ]
    yf_backup = {item["name"]: item for item in fetch_yf_group({n: s for n, s in targets})}

    results = []
    try:
        df = ak.stock_zh_index_spot_em()
        for name, sym in targets:
            row = df[df["名称"] == name]
            if not row.empty:
                price = float(row["最新价"].values[0])
                pct = float(row["涨跌幅"].values[0])
                if math.isfinite(price) and math.isfinite(pct):
                    hist_pts = yf_backup.get(name, {}).get("history", [])
                    if hist_pts:
                        hist_pts = list(hist_pts[:-1]) + [price]
                    results.append({
                        "name": name,
                        "symbol": sym,
                        "price": f"{price:,.2f}",
                        "pct": pct,
                        "history": hist_pts,
                    })
    except Exception as e:
        print(f"[行情] 东方财富接口异常，切换 Yahoo 备用源: {e}")

    if len(results) < 4:
        results = [yf_backup[n] for n, _ in targets if n in yf_backup]
    return results


def fetch_rss_within_hours(rss_dict, hours=20, max_per_feed=7):
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


def fetch_cls_telegraph(limit=55):
    """抓取财联社 7x24 实时电报"""
    lines = []
    try:
        df_cls = ak.stock_info_global_cls()
        for _, row in df_cls.head(limit).iterrows():
            title = str(row.get("标题", "")).strip()
            content = str(row.get("内容", "")).strip()
            pub_date = str(row.get("发布日期", "")).strip()
            pub_time = str(row.get("发布时间", "")).strip()
            ts = f"{pub_date[-5:]} {pub_time[:5]}".strip()
            lines.append(f"[财联社 | {ts}] 标题:{title} | 正文:{content[:260]}")
    except Exception as e:
        print(f"[财联社] 抓取失败: {e}")
    return "\n".join(lines)


def analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw, edition):
    """按早报/晚报差异化定位，调用大模型去重、精译与重磅评级"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    now_str = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")

    if edition == "pm":
        role_focus = (
            "当前为【晚报（22:00）】：请重点复盘今天 A 股收盘主线、白天国内政策与产业重磅消息、"
            "欧洲盘面动向，以及今晚美股盘前/刚开盘的核心催化（财报、CPI/非农等宏观数据与科技巨头动态）。"
            "尽量优先选取今天白天至晚间的新增增量消息，避免炒冷饭。"
        )
        overview_guide = "150-200字的今日晚报全景复盘。总结今天A股核心强弱与政策催化，串联欧盘与今晚美股盘前/开盘最值得盯紧的变量与风险。"
        brief_type = "晚报"
    else:
        role_focus = (
            "当前为【早报（08:30）】：请重点聚焦隔夜美股收盘主线、美联储与全球宏观动向、"
            "科技巨头盘后财报与大宗/加密异动，以及今天 A 股开盘前最值得关注的产业催化与风险。"
        )
        overview_guide = "150-200字的今日早报全景总览。串联隔夜美股核心驱动、宏观利率/政策预期变化，以及今天A股与科技线需要重点关注的催化或风险。"
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
1. 这是每日{brief_type}：us_global_news 精选 6 条、cn_a_news 精选 5 条、general_news 精选 4 条，每个列表内按 score 从高到低排列。
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


def render_ticker_tape_html(all_groups):
    chips = []
    for items, is_us in all_groups:
        for item in items:
            pct = item["pct"]
            sign = "+" if pct > 0 else ""
            if pct == 0:
                cls = "tape-flat"
            elif is_us:
                cls = "tape-green" if pct > 0 else "tape-red"
            else:
                cls = "tape-red" if pct > 0 else "tape-green"
            chips.append(
                f'<span class="tape-item">'
                f'  <span class="tape-name">{esc(item["name"])}</span>'
                f'  <span class="tape-price">{esc(item["price"])}</span>'
                f'  <span class="tape-pct {cls}">{sign}{pct:.2f}%</span>'
                f'</span>'
            )
    if not chips:
        return ""
    track = "".join(chips)
    return f'<div class="ticker-tape"><div class="ticker-track">{track}{track}</div></div>'


def render_market_card_html(title, subtitle, data, is_us=True, card_prefix="m"):
    rows_html = []
    if not data:
        rows_html.append('<div class="market-row"><span class="ticker-name" style="color:var(--text-muted)">暂无有效数据</span></div>')
    else:
        for idx, item in enumerate(data):
            pct = item["pct"]
            sign = "+" if pct > 0 else ""
            if pct == 0:
                cls = "pct-flat"
                stroke = "#94a3b8"
            elif is_us:
                cls = "pct-green" if pct > 0 else "pct-red"
                stroke = "#10b981" if pct > 0 else "#f43f5e"
            else:
                cls = "pct-red" if pct > 0 else "pct-green"
                stroke = "#f43f5e" if pct > 0 else "#10b981"

            spark_svg = render_sparkline_svg(item.get("history", []), stroke, f"sg-{card_prefix}-{idx}")
            rows_html.append(
                f'<div class="market-row">'
                f'  <div class="ticker-left">'
                f'    <span class="ticker-name">{esc(item["name"])}</span>'
                f'    <span class="ticker-sym">{esc(item.get("symbol", ""))}</span>'
                f'  </div>'
                f'  <div class="ticker-mid">{spark_svg}</div>'
                f'  <div class="ticker-right">'
                f'    <span class="ticker-price">{esc(item["price"])}</span>'
                f'    <span class="pct-badge {cls}">{sign}{pct:.2f}%</span>'
                f'  </div>'
                f'</div>'
            )
    return (
        f'<div class="market-card">'
        f'  <div class="market-card-title"><span>{esc(title)}</span><span class="market-card-sub">{esc(subtitle)}</span></div>'
        f'  {"".join(rows_html)}'
        f'</div>'
    )


def render_top_picks_html(llm_data, edition):
    sec_map = (
        [("cn_a_news", "cn", "A股/宏观"), ("us_global_news", "us", "美股/全球"), ("general_news", "gen", "国际/科技")]
        if edition == "pm"
        else [("us_global_news", "us", "美股/全球"), ("cn_a_news", "cn", "A股/宏观"), ("general_news", "gen", "国际/科技")]
    )
    candidates = []
    for key, prefix, label in sec_map:
        for i, item in enumerate(llm_data.get(key, []), 1):
            candidates.append((item["score"], f"{prefix}-{i}", label, item.get("title", "")))

    candidates.sort(key=lambda x: -x[0])
    top_items = [c for c in candidates if c[0] >= 5][:4]
    if not top_items:
        top_items = candidates[:3]
    if not top_items:
        return ""

    links = []
    for score, anchor, label, title in top_items:
        badge = "🔥 5★" if score >= 5 else f"★{score}"
        links.append(
            f'<a class="spotlight-item" href="#{anchor}">'
            f'  <span class="spotlight-badge">{badge}</span>'
            f'  <span class="spotlight-sec">{esc(label)}</span>'
            f'  <span class="spotlight-title">{esc(title)}</span>'
            f'  <span class="spotlight-arrow">↓</span>'
            f'</a>'
        )
    return (
        '<div class="spotlight-wrap">'
        '  <div class="spotlight-header">⚡ 本期必读重磅速达（按重要性排序）</div>'
        f'  <div class="spotlight-list">{"".join(links)}</div>'
        '</div>'
    )


def render_news_cards_html(items, section_prefix, with_impact=True):
    if not items:
        return '<div class="news-card"><div class="news-fact">本时段暂无重要更新</div></div>'
    cards = []
    for i, item in enumerate(items, 1):
        score = safe_score(item.get("score", 3))
        tag = esc(item.get("tag", "要闻"))
        title = esc(item.get("title", ""))
        source = esc(item.get("source", ""))
        link = str(item.get("link", "")).strip()
        anchor_id = f"{section_prefix}-{i}"

        is_breaking = score >= 5
        card_cls = "news-card is-breaking" if is_breaking else "news-card"
        stars_filled = "★" * score
        stars_empty = "☆" * (5 - score)
        score_badge = (
            f'<span class="breaking-pill">🔥 重磅头条 · {stars_filled}</span>'
            if is_breaking
            else f'<span class="score-pill" title="重磅指数 {score}/5"><span class="stars-on">{stars_filled}</span><span class="stars-off">{stars_empty}</span></span>'
        )

        sent_val = "neutral"
        sent_html = ""
        if with_impact:
            s = item.get("sentiment", "中性")
            if s == "利好":
                sent_val = "bull"
                sent_html = '<span class="sent-pill sent-bull">▲ 利好</span>'
            elif s == "利空":
                sent_val = "bear"
                sent_html = '<span class="sent-pill sent-bear">▼ 利空</span>'
            else:
                sent_val = "neutral"
                sent_html = '<span class="sent-pill sent-neu">● 中性</span>'

        body_text = esc(item.get("fact", "") if with_impact else item.get("summary", ""))
        impact_html = ""
        if with_impact and item.get("impact"):
            impact_html = (
                f'<div class="news-impact">'
                f'  <span class="impact-label">深度逻辑</span>'
                f'  <span>{esc(item.get("impact"))}</span>'
                f'</div>'
            )

        link_html = (
            f'<a class="source-link" href="{esc(link)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a>'
            if link.startswith(("http://", "https://"))
            else '<span class="source-nolink">快讯电报</span>'
        )

        cards.append(
            f'<article id="{anchor_id}" class="{card_cls}" data-score="{score}" data-sentiment="{sent_val}">'
            f'  <div class="news-top">'
            f'    <span class="news-idx">#{i:02d}</span>'
            f'    {score_badge}'
            f'    <span class="tag-pill">{tag}</span>'
            f'    {sent_html}'
            f'  </div>'
            f'  <h3 class="news-title">{title}</h3>'
            f'  <p class="news-fact">{body_text}</p>'
            f'  {impact_html}'
            f'  <div class="news-footer">'
            f'    <span class="source-meta">📰 {source}</span>'
            f'    {link_html}'
            f'  </div>'
            f'</article>'
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
        '<select class="archive-select" aria-label="往期早晚报" '
        'onchange="if(this.value) window.location.href=this.value">'
        + "".join(options)
        + "</select>"
    )


def render_news_sections_html(us_news, cn_news, gen_news, edition):
    """早报优先展示美股/全球，晚报优先展示 A 股/国内宏观"""
    us_section = (
        f'<section id="us-global" class="section" data-section="us">'
        f'  <div class="section-header">'
        f'    <h2 class="section-title">🇺🇸 {"美股盘前与全球财经" if edition == "pm" else "美股与全球财经"}</h2>'
        f'    <span class="section-count" data-count-for="us">{len(us_news)} 条精选</span>'
        f'  </div>'
        f'  <div class="news-list">{render_news_cards_html(us_news, "us", with_impact=True)}</div>'
        f'  <div class="empty-hint" data-empty-for="us">当前筛选条件下暂无美股/全球财经新闻</div>'
        f'</section>'
    )
    cn_section = (
        f'<section id="cn-a" class="section" data-section="cn">'
        f'  <div class="section-header">'
        f'    <h2 class="section-title">🇨🇳 {"A 股收盘与国内宏观" if edition == "pm" else "A 股与国内宏观"}</h2>'
        f'    <span class="section-count" data-count-for="cn">{len(cn_news)} 条精选</span>'
        f'  </div>'
        f'  <div class="news-list">{render_news_cards_html(cn_news, "cn", with_impact=True)}</div>'
        f'  <div class="empty-hint" data-empty-for="cn">当前筛选条件下暂无 A 股/宏观新闻</div>'
        f'</section>'
    )
    gen_section = (
        f'<section id="general" class="section" data-section="gen">'
        f'  <div class="section-header">'
        f'    <h2 class="section-title">🌐 国际时政与前沿科技</h2>'
        f'    <span class="section-count" data-count-for="gen">{len(gen_news)} 条精选</span>'
        f'  </div>'
        f'  <div class="news-list">{render_news_cards_html(gen_news, "gen", with_impact=False)}</div>'
        f'  <div class="empty-hint" data-empty-for="gen">当前筛选条件下暂无国际/科技新闻</div>'
        f'</section>'
    )
    if edition == "pm":
        return "\n".join([cn_section, us_section, gen_section])
    return "\n".join([us_section, cn_section, gen_section])


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark" data-edition="__EDITION_CODE__">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DAILY BRIEF · __FULL_NAME__ · __DATE_STR__</title>
  <style>
    :root {
      --bg: #070b14;
      --bg-glow-1: rgba(56, 189, 248, 0.11);
      --bg-glow-2: rgba(99, 102, 241, 0.09);
      --bg-elevated: #0f172a;
      --bg-card: rgba(17, 25, 42, 0.86);
      --bg-card-hover: rgba(23, 34, 56, 0.96);
      --bg-subtle: rgba(255, 255, 255, 0.035);
      --border: rgba(148, 163, 184, 0.13);
      --border-strong: rgba(148, 163, 184, 0.28);
      --text: #f1f5f9;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --accent: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.12);
      --edition-bg: rgba(245, 158, 11, 0.14);
      --edition-color: #fbbf24;
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.14);
      --red: #f43f5e;
      --red-bg: rgba(244, 63, 94, 0.14);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.14);
      --breaking-border: rgba(245, 158, 11, 0.45);
      --breaking-glow: 0 12px 34px -14px rgba(245, 158, 11, 0.24);
      --shadow: 0 12px 30px -14px rgba(0, 0, 0, 0.65);
      --font-sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif;
      --font-serif: "Songti SC", "STSong", "Noto Serif CJK SC", "Source Han Serif SC", Georgia, serif;
      --font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
    }

    [data-edition="pm"] {
      --bg-glow-1: rgba(139, 92, 246, 0.13);
      --bg-glow-2: rgba(56, 189, 248, 0.09);
      --accent: #818cf8;
      --accent-soft: rgba(129, 140, 248, 0.14);
      --edition-bg: rgba(139, 92, 246, 0.16);
      --edition-color: #c4b5fd;
    }

    [data-theme="light"] {
      --bg: #f4f6f9;
      --bg-glow-1: rgba(2, 132, 199, 0.07);
      --bg-glow-2: rgba(99, 102, 241, 0.05);
      --bg-elevated: #ffffff;
      --bg-card: #ffffff;
      --bg-card-hover: #ffffff;
      --bg-subtle: #f8fafc;
      --border: rgba(15, 23, 42, 0.08);
      --border-strong: rgba(15, 23, 42, 0.18);
      --text: #0f172a;
      --text-secondary: #475569;
      --text-muted: #64748b;
      --accent: #0284c7;
      --accent-soft: rgba(2, 132, 199, 0.1);
      --edition-bg: rgba(217, 119, 6, 0.12);
      --edition-color: #b45309;
      --green: #059669;
      --green-bg: rgba(5, 150, 105, 0.1);
      --red: #e11d48;
      --red-bg: rgba(225, 29, 72, 0.1);
      --amber: #d97706;
      --amber-bg: rgba(217, 119, 6, 0.12);
      --breaking-border: rgba(217, 119, 6, 0.48);
      --breaking-glow: 0 10px 26px -12px rgba(217, 119, 6, 0.18);
      --shadow: 0 6px 22px -10px rgba(15, 23, 42, 0.08);
    }

    [data-theme="light"][data-edition="pm"] {
      --accent: #4f46e5;
      --accent-soft: rgba(79, 70, 229, 0.1);
      --edition-bg: rgba(79, 70, 229, 0.12);
      --edition-color: #4338ca;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body {
      font-family: var(--font-sans);
      background-color: var(--bg);
      background-image:
        radial-gradient(900px 420px at 15% -6%, var(--bg-glow-1), transparent 60%),
        radial-gradient(760px 380px at 88% 2%, var(--bg-glow-2), transparent 55%);
      background-attachment: fixed;
      color: var(--text);
      line-height: 1.65;
      -webkit-font-smoothing: antialiased;
      padding-bottom: 80px;
    }

    .read-progress {
      position: fixed;
      top: 0;
      left: 0;
      height: 2.5px;
      width: 0%;
      background: linear-gradient(90deg, var(--accent), #818cf8, var(--amber));
      z-index: 100;
      transition: width 0.08s linear;
    }

    .ticker-tape {
      background: var(--bg-elevated);
      border-bottom: 1px solid var(--border);
      overflow: hidden;
      white-space: nowrap;
      height: 34px;
      display: flex;
      align-items: center;
      font-family: var(--font-mono);
      font-size: 12px;
    }
    .ticker-track {
      display: inline-flex;
      align-items: center;
      animation: marquee 42s linear infinite;
    }
    .ticker-tape:hover .ticker-track { animation-play-state: paused; }
    @keyframes marquee {
      0% { transform: translateX(0); }
      100% { transform: translateX(-50%); }
    }
    .tape-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 0 18px;
      border-right: 1px solid var(--border);
    }
    .tape-name { color: var(--text-secondary); font-family: var(--font-sans); font-weight: 600; }
    .tape-price { color: var(--text); font-weight: 600; }
    .tape-green { color: var(--green); font-weight: 700; }
    .tape-red { color: var(--red); font-weight: 700; }
    .tape-flat { color: var(--text-muted); }

    .container { max-width: 1040px; margin: 0 auto; padding: 0 22px; }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 60;
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      background: color-mix(in srgb, var(--bg) 80%, transparent);
      border-bottom: 1px solid var(--border);
    }
    .topbar-inner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 58px;
      gap: 12px;
    }
    .brand { display: flex; align-items: center; gap: 10px; text-decoration: none; color: var(--text); }
    .brand-mark {
      width: 26px;
      height: 26px;
      border-radius: 7px;
      background: linear-gradient(135deg, var(--accent), #6366f1);
      display: grid;
      place-items: center;
      font-family: var(--font-mono);
      font-weight: 800;
      font-size: 13px;
      color: #fff;
      box-shadow: 0 4px 12px rgba(56, 189, 248, 0.28);
    }
    .brand-title { font-weight: 800; font-size: 15px; letter-spacing: 0.6px; }
    .brand-sub { font-size: 12px; color: var(--text-muted); padding-left: 8px; border-left: 1px solid var(--border); }
    .controls { display: flex; align-items: center; gap: 8px; }
    .archive-select, .theme-btn {
      background: var(--bg-elevated);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 6px 11px;
      font-size: 12.5px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.18s;
    }
    .archive-select:hover, .theme-btn:hover { border-color: var(--border-strong); background: var(--bg-card-hover); }

    .hero { padding: 34px 0 24px; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
    .hero-top {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 14px;
    }
    .hero-meta-left {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    .edition-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      background: var(--edition-bg);
      color: var(--edition-color);
    }
    .hero-date {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-secondary);
      font-family: var(--font-mono);
    }
    .live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.18); }
    .mood-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
    }
    .mood-bullish { background: var(--red-bg); color: var(--red); border: 1px solid rgba(244, 63, 94, 0.25); }
    .mood-bearish { background: var(--green-bg); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.25); }
    .mood-neutral { background: var(--accent-soft); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }

    .hero h1 {
      font-family: var(--font-serif);
      font-size: clamp(25px, 4.2vw, 36px);
      font-weight: 800;
      letter-spacing: -0.3px;
      line-height: 1.32;
      margin-bottom: 18px;
    }

    .quick-nav { display: flex; flex-wrap: wrap; gap: 8px; }
    .quick-nav a {
      text-decoration: none;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-secondary);
      background: var(--bg-card);
      border: 1px solid var(--border);
      padding: 6px 14px;
      border-radius: 999px;
      transition: all 0.18s;
    }
    .quick-nav a:hover { color: var(--accent); border-color: var(--accent); transform: translateY(-1px); }

    .market-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-bottom: 24px;
      scroll-margin-top: 76px;
    }
    .market-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }
    .market-card-title {
      font-size: 13px;
      font-weight: 700;
      color: var(--text-secondary);
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .market-card-sub { font-size: 11px; font-weight: 500; color: var(--text-muted); background: var(--bg-subtle); padding: 2px 7px; border-radius: 5px; }
    .market-row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      align-items: center;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px dashed var(--border);
    }
    .market-row:last-child { border-bottom: none; padding-bottom: 0; }
    .ticker-left { display: flex; flex-direction: column; min-width: 0; }
    .ticker-name { font-size: 14px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .ticker-sym { font-family: var(--font-mono); font-size: 10.5px; color: var(--text-muted); }
    .ticker-mid { display: flex; align-items: center; justify-content: center; }
    .sparkline { display: block; overflow: visible; }
    .ticker-right { display: flex; align-items: center; justify-content: flex-end; gap: 9px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
    .ticker-price { font-size: 14px; font-weight: 700; color: var(--text); }
    .pct-badge { min-width: 72px; text-align: right; font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 7px; }
    .pct-green { color: var(--green); background: var(--green-bg); }
    .pct-red { color: var(--red); background: var(--red-bg); }
    .pct-flat { color: var(--text-secondary); background: var(--bg-subtle); }

    .overview-box {
      background: linear-gradient(145deg, var(--bg-card) 0%, var(--bg-elevated) 100%);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 22px 24px;
      margin-bottom: 26px;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }
    .overview-box::before {
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 4px;
      background: linear-gradient(180deg, var(--accent), #818cf8);
    }
    .overview-label { font-size: 13px; font-weight: 800; color: var(--accent); letter-spacing: 0.6px; margin-bottom: 10px; }
    .overview-text { font-size: 15.5px; color: var(--text); line-height: 1.85; }

    .spotlight-wrap { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }
    .spotlight-header { font-size: 12px; font-weight: 700; color: var(--amber); letter-spacing: 0.4px; margin-bottom: 10px; }
    .spotlight-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .spotlight-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 11px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 10px;
      text-decoration: none;
      color: var(--text);
      font-size: 13px;
      transition: all 0.16s;
      min-width: 0;
    }
    .spotlight-item:hover { border-color: var(--amber); background: var(--amber-bg); transform: translateX(2px); }
    .spotlight-badge { font-family: var(--font-mono); font-size: 11px; font-weight: 700; color: var(--amber); white-space: nowrap; }
    .spotlight-sec { font-size: 11px; color: var(--text-muted); white-space: nowrap; }
    .spotlight-title { flex: 1; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .spotlight-arrow { color: var(--text-muted); font-size: 12px; }

    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 24px;
      padding: 12px 14px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: var(--shadow);
    }
    .filter-pills { display: flex; flex-wrap: wrap; gap: 6px; }
    .filter-btn {
      background: var(--bg-subtle);
      color: var(--text-secondary);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 5px 13px;
      font-size: 12.5px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.16s;
    }
    .filter-btn:hover { color: var(--text); border-color: var(--border-strong); }
    .filter-btn.active { background: var(--accent); color: #ffffff; border-color: var(--accent); }
    .search-box { position: relative; flex: 0 1 240px; min-width: 180px; }
    .search-input {
      width: 100%;
      background: var(--bg-elevated);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 6px 14px 6px 32px;
      font-size: 12.5px;
      outline: none;
    }
    .search-input:focus { border-color: var(--accent); }
    .search-icon { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); font-size: 12px; color: var(--text-muted); pointer-events: none; }

    .section { margin-bottom: 38px; scroll-margin-top: 76px; }
    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .section-title { font-family: var(--font-serif); font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 9px; }
    .section-count { font-family: var(--font-mono); font-size: 12px; color: var(--text-muted); background: var(--bg-elevated); border: 1px solid var(--border); padding: 3px 10px; border-radius: 999px; }

    .news-list { display: grid; gap: 15px; }
    .news-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px 22px;
      transition: transform 0.2s ease, border-color 0.2s ease;
      box-shadow: var(--shadow);
      scroll-margin-top: 84px;
    }
    .news-card:hover { transform: translateY(-2px); border-color: var(--border-strong); background: var(--bg-card-hover); }
    .news-card.is-breaking {
      border-color: var(--breaking-border);
      box-shadow: var(--breaking-glow);
      background: linear-gradient(180deg, color-mix(in srgb, var(--amber) 5%, var(--bg-card)) 0%, var(--bg-card) 45%);
    }
    .news-card.is-hidden { display: none; }

    .news-top { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
    .news-idx { font-family: var(--font-mono); font-size: 12px; font-weight: 700; color: var(--text-muted); }
    .breaking-pill { font-size: 11.5px; font-weight: 800; padding: 2px 9px; border-radius: 6px; background: var(--amber-bg); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.32); }
    .score-pill { font-size: 11.5px; letter-spacing: 1px; padding: 2px 7px; border-radius: 6px; background: var(--bg-subtle); border: 1px solid var(--border); }
    .stars-on { color: var(--amber); }
    .stars-off { color: var(--text-muted); opacity: 0.45; }
    .tag-pill { font-size: 11.5px; font-weight: 600; padding: 2px 9px; border-radius: 6px; background: var(--accent-soft); color: var(--accent); }
    .sent-pill { font-size: 11.5px; font-weight: 700; padding: 2px 9px; border-radius: 6px; }
    .sent-bull { background: var(--red-bg); color: var(--red); }
    .sent-bear { background: var(--green-bg); color: var(--green); }
    .sent-neu { background: var(--bg-subtle); color: var(--text-secondary); }

    .news-title { font-size: 17.5px; font-weight: 700; line-height: 1.45; margin-bottom: 9px; color: var(--text); }
    .news-fact { font-size: 14.8px; color: var(--text-secondary); line-height: 1.8; margin-bottom: 12px; }
    .news-impact {
      display: flex;
      align-items: baseline;
      gap: 8px;
      background: var(--bg-subtle);
      border-left: 3px solid var(--accent);
      border-radius: 0 10px 10px 0;
      padding: 9px 13px;
      font-size: 13.5px;
      color: var(--text);
      margin-bottom: 13px;
      line-height: 1.65;
    }
    .impact-label { font-size: 11.5px; font-weight: 800; color: var(--accent); background: var(--accent-soft); padding: 1px 7px; border-radius: 5px; white-space: nowrap; }
    .news-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12.5px; color: var(--text-muted); padding-top: 10px; border-top: 1px solid var(--border); }
    .source-meta { font-family: var(--font-mono); font-size: 12px; }
    .source-link { color: var(--accent); text-decoration: none; font-weight: 600; padding: 3px 10px; border-radius: 999px; background: var(--accent-soft); transition: all 0.16s; }
    .source-link:hover { background: var(--accent); color: #fff; }
    .source-nolink { font-size: 11.5px; opacity: 0.65; }

    .empty-hint { display: none; text-align: center; padding: 28px 16px; color: var(--text-muted); font-size: 13.5px; border: 1px dashed var(--border); border-radius: 14px; }
    .empty-hint.show { display: block; }

    .back-top {
      position: fixed;
      right: 22px;
      bottom: 24px;
      width: 40px;
      height: 40px;
      border-radius: 50%;
      border: 1px solid var(--border-strong);
      background: var(--bg-elevated);
      color: var(--text);
      font-size: 16px;
      cursor: pointer;
      box-shadow: var(--shadow);
      opacity: 0;
      pointer-events: none;
      transition: all 0.2s;
      z-index: 70;
    }
    .back-top.visible { opacity: 1; pointer-events: auto; }
    .back-top:hover { border-color: var(--accent); color: var(--accent); transform: translateY(-2px); }

    footer { text-align: center; font-size: 12.5px; color: var(--text-muted); padding-top: 28px; border-top: 1px solid var(--border); line-height: 1.9; }

    @media (max-width: 760px) {
      .market-grid { grid-template-columns: 1fr; }
      .spotlight-list { grid-template-columns: 1fr; }
      .hero { padding: 24px 0 18px; }
      .news-card { padding: 16px; }
      .overview-box { padding: 18px; }
      .brand-sub { display: none; }
      .search-box { flex: 1 1 100%; }
      .ticker-mid { display: none; }
    }
  </style>
</head>
<body>
  <div class="read-progress" id="readProgress"></div>
  __TICKER_TAPE__

  <header class="topbar">
    <div class="container topbar-inner">
      <a class="brand" href="__HOME_LINK__">
        <span class="brand-mark">DB</span>
        <span class="brand-title">DAILY BRIEF</span>
        <span class="brand-sub">早晚报双推终端</span>
      </a>
      <div class="controls">
        __ARCHIVE_SELECT__
        <button class="theme-btn" id="themeToggle" type="button" aria-label="切换主题">☀️ 浅色</button>
      </div>
    </div>
  </header>

  <main class="container">
    <section class="hero">
      <div class="hero-top">
        <div class="hero-meta-left">
          <span class="edition-pill">__EDITION_BADGE__</span>
          <span class="hero-date">
            <span class="live-dot"></span>
            <span>__DATE_FULL__ __WEEKDAY__ · __GEN_TIME__ CST</span>
          </span>
        </div>
        <span class="mood-pill __MOOD_CLASS__">__MOOD_LABEL__</span>
      </div>
      <h1>__MOOD_HEADLINE__</h1>
      <nav class="quick-nav">
        __QUICK_NAV__
      </nav>
    </section>

    <section id="markets" class="market-grid">
      __MARKET_CARDS__
    </section>

    <section class="overview-box">
      <div class="overview-label">🧭 __OVERVIEW_TITLE__</div>
      <div class="overview-text">__OVERVIEW__</div>
      __TOP_PICKS_HTML__
    </section>

    <div class="filter-bar">
      <div class="filter-pills" role="group" aria-label="新闻筛选">
        <button class="filter-btn active" type="button" data-filter="all">全部资讯</button>
        <button class="filter-btn" type="button" data-filter="top">🔥 重磅 4-5★</button>
        <button class="filter-btn" type="button" data-filter="bull">▲ 仅看利好</button>
        <button class="filter-btn" type="button" data-filter="bear">▼ 仅看利空</button>
      </div>
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input class="search-input" id="searchInput" type="search" placeholder="搜索公司、板块或关键词..." />
      </div>
    </div>

    __NEWS_SECTIONS__

    <footer>
      <div>权威信源：Bloomberg · Financial Times · Reuters · WSJ · CNBC · 财联社 · BBC（按重磅程度排序）</div>
      <div>精准行情：Yahoo Finance &amp; 东方财富（代码直出，零模型幻觉） · 早报 08:30 / 晚报 22:00 自动推送</div>
    </footer>
  </main>

  <button class="back-top" id="backTop" type="button" aria-label="返回顶部">↑</button>

  <script>
    (function () {
      const root = document.documentElement;
      const themeBtn = document.getElementById("themeToggle");
      const saved = localStorage.getItem("brief-theme") || "dark";
      function applyTheme(t) {
        root.setAttribute("data-theme", t);
        themeBtn.textContent = t === "dark" ? "☀️ 浅色" : "🌙 深色";
      }
      applyTheme(saved);
      themeBtn.addEventListener("click", function () {
        const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
        localStorage.setItem("brief-theme", next);
        applyTheme(next);
      });

      const progressEl = document.getElementById("readProgress");
      const backTopBtn = document.getElementById("backTop");
      window.addEventListener("scroll", function () {
        const scrollTop = window.scrollY || document.documentElement.scrollTop;
        const docHeight = document.documentElement.scrollHeight - window.innerHeight;
        const pct = docHeight > 0 ? Math.min(100, (scrollTop / docHeight) * 100) : 0;
        progressEl.style.width = pct + "%";
        backTopBtn.classList.toggle("visible", scrollTop > 420);
      }, { passive: true });
      backTopBtn.addEventListener("click", function () {
        window.scrollTo({ top: 0, behavior: "smooth" });
      });

      let activeFilter = "all";
      const filterBtns = document.querySelectorAll(".filter-btn");
      const searchInput = document.getElementById("searchInput");
      const sections = document.querySelectorAll("section[data-section]");

      function applyFilters() {
        const kw = (searchInput.value || "").trim().toLowerCase();
        sections.forEach(function (sec) {
          const secKey = sec.getAttribute("data-section");
          const cards = sec.querySelectorAll(".news-card");
          let visibleCount = 0;

          cards.forEach(function (card) {
            const score = parseInt(card.getAttribute("data-score") || "3", 10);
            const sent = card.getAttribute("data-sentiment") || "neutral";
            const text = (card.textContent || "").toLowerCase();

            let matchFilter = true;
            if (activeFilter === "top") matchFilter = score >= 4;
            else if (activeFilter === "bull") matchFilter = sent === "bull";
            else if (activeFilter === "bear") matchFilter = sent === "bear";

            const matchSearch = !kw || text.includes(kw);
            const show = matchFilter && matchSearch;
            card.classList.toggle("is-hidden", !show);
            if (show) visibleCount++;
          });

          const countEl = document.querySelector('[data-count-for="' + secKey + '"]');
          const emptyEl = document.querySelector('[data-empty-for="' + secKey + '"]');
          if (countEl) countEl.textContent = visibleCount + " 条显示";
          if (emptyEl) emptyEl.classList.toggle("show", visibleCount === 0);
        });
      }

      filterBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
          filterBtns.forEach(function (b) { b.classList.remove("active"); });
          btn.classList.add("active");
          activeFilter = btn.getAttribute("data-filter") || "all";
          applyFilters();
        });
      });
      searchInput.addEventListener("input", applyFilters);
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

    if edition == "pm":
        tape_groups = [(a_idx, False), (us_idx, True), (tech_stocks, True), (macro_assets, True)]
        market_cards = "\n".join([
            render_market_card_html(meta["a_card_title"], "红涨绿跌 · 7日趋势", a_idx, is_us=False, card_prefix="cn"),
            render_market_card_html("🌍 大宗 · 加密 · 汇率", "绿涨红跌 · 7日趋势", macro_assets, is_us=True, card_prefix="mc"),
            render_market_card_html(meta["us_card_title"], "绿涨红跌 · 7日趋势", us_idx, is_us=True, card_prefix="us"),
            render_market_card_html("🚀 美股科技风向", "绿涨红跌 · 7日趋势", tech_stocks, is_us=True, card_prefix="tk"),
        ])
        quick_nav = (
            '<a href="#markets">📊 跨市场行情</a>'
            '<a href="#cn-a">🇨🇳 A股收盘与宏观</a>'
            '<a href="#us-global">🇺🇸 美股盘前与全球</a>'
            '<a href="#general">🌐 国际时政与科技</a>'
        )
        overview_title = "今日晚报复盘与美股前瞻"
    else:
        tape_groups = [(us_idx, True), (tech_stocks, True), (a_idx, False), (macro_assets, True)]
        market_cards = "\n".join([
            render_market_card_html(meta["us_card_title"], "绿涨红跌 · 7日趋势", us_idx, is_us=True, card_prefix="us"),
            render_market_card_html("🚀 美股科技风向", "绿涨红跌 · 7日趋势", tech_stocks, is_us=True, card_prefix="tk"),
            render_market_card_html(meta["a_card_title"], "红涨绿跌 · 7日趋势", a_idx, is_us=False, card_prefix="cn"),
            render_market_card_html("🌍 大宗 · 加密 · 汇率", "绿涨红跌 · 7日趋势", macro_assets, is_us=True, card_prefix="mc"),
        ])
        quick_nav = (
            '<a href="#markets">📊 跨市场行情</a>'
            '<a href="#us-global">🇺🇸 美股与全球财经</a>'
            '<a href="#cn-a">🇨🇳 A股与国内宏观</a>'
            '<a href="#general">🌐 国际时政与科技</a>'
        )
        overview_title = "今日早报主编全景总览"

    us_news = llm_data.get("us_global_news", [])
    cn_news = llm_data.get("cn_a_news", [])
    gen_news = llm_data.get("general_news", [])

    replacements = {
        "__EDITION_CODE__": edition,
        "__FULL_NAME__": esc(meta["full_name"]),
        "__EDITION_BADGE__": esc(meta["badge_text"]),
        "__DATE_STR__": esc(date_str),
        "__DATE_FULL__": esc(date_full),
        "__WEEKDAY__": esc(weekday),
        "__GEN_TIME__": esc(now.strftime("%H:%M")),
        "__MOOD_CLASS__": mood_cls,
        "__MOOD_LABEL__": esc(mood_label),
        "__MOOD_HEADLINE__": esc(llm_data.get("mood_headline") or f"{date_full} {meta['full_name']}"),
        "__HOME_LINK__": "../index.html" if is_archive else "index.html",
        "__ARCHIVE_SELECT__": build_archive_select_html(archive_ids, edition_id, is_archive=is_archive),
        "__TICKER_TAPE__": render_ticker_tape_html(tape_groups),
        "__QUICK_NAV__": quick_nav,
        "__MARKET_CARDS__": market_cards,
        "__OVERVIEW_TITLE__": esc(overview_title),
        "__OVERVIEW__": esc(llm_data.get("overview", "")),
        "__TOP_PICKS_HTML__": render_top_picks_html(llm_data, edition),
        "__NEWS_SECTIONS__": render_news_sections_html(us_news, cn_news, gen_news, edition),
    }
    page = HTML_TEMPLATE
    for k, v in replacements.items():
        page = page.replace(k, v)
    return page


def save_web_pages(now, edition, us_idx, tech_stocks, a_idx, macro_assets, llm_data):
    """生成 docs/index.html 与独立的 docs/archive/YYYY-MM-DD-am|pm.html"""
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
    # 倒序排列：同一天 pm 自动排在 am 之前，保留最近 120 期（约 60 天早晚报）
    entries = sorted(set(entries), reverse=True)[:120]
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
    """构建早/晚报专属飞书精简入口卡片（直链独立归档页，避免晚报覆盖早报链接）"""
    meta = get_edition_meta(edition)
    today_str = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood_map = {
        "bullish": ("red", "🔥 风险偏好回升"),
        "bearish": ("green", "❄️ 市场情绪偏谨慎"),
        "neutral": ("indigo" if edition == "pm" else "blue", "⚖️ 震荡分化格局"),
    }
    header_color, mood_label = mood_map.get(llm_data.get("mood", "neutral"), ("blue", f"📊 每日{meta['name']}"))

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

    if edition == "pm":
        row1_left = (f"🇨🇳 **A 股今日收盘**\n{format_market_md(a_idx, is_us=False)}")
        row1_right = (f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}")
        row2_left = (f"🇺🇸 **美股指数**\n{format_market_md(us_idx, is_us=True)}")
        row2_right = (f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}")
    else:
        row1_left = (f"🇺🇸 **隔夜美股**\n{format_market_md(us_idx, is_us=True)}")
        row1_right = (f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}")
        row2_left = (f"🇨🇳 **A 股收盘**\n{format_market_md(a_idx, is_us=False)}")
        row2_right = (f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}")

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

    # 注脚同时保留“早报/晚报”，确保飞书机器人已配置的“早报”安全关键词始终命中
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"早报/晚报双推终端 · 信源：Bloomberg / FT / Reuters / WSJ / CNBC / 财联社 / BBC | {now.strftime('%H:%M')}",
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
    cn_fin_raw = fetch_cls_telegraph(limit=55)

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
    parser.add_argument("--edition", choices=["auto", "am", "pm"], default="auto")
    args = parser.parse_args()

    if args.generate:
        run_generate(args.edition)
    elif args.notify:
        run_notify()
    else:
        run_generate(args.edition)
        run_notify()
