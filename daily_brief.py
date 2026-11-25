import os
import sys
import re
import math
import json
import html
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


def get_site_url():
    """自动推算 GitHub Pages 网址，也支持通过 SITE_URL 环境变量自定义域名"""
    custom = os.environ.get("SITE_URL", "").strip()
    if custom:
        return custom.rstrip("/") + "/"
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in gh_repo:
        owner, repo = gh_repo.split("/", 1)
        return f"https://{owner}.github.io/{repo}/"
    return ""


def clean_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def esc(text):
    return html.escape(str(text or ""), quote=True)


def fetch_yf_group(tickers_dict):
    """批量获取 Yahoo Finance 行情（自动过滤 NaN + fast_info 兜底）"""
    results = []
    for name, symbol in tickers_dict.items():
        close, prev = None, None
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="10d")
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

            if close is not None and prev is not None and prev != 0:
                pct = (close - prev) / prev * 100
                if math.isfinite(pct):
                    results.append({"name": name, "symbol": symbol, "price": f"{close:,.2f}", "pct": pct})
                    continue
            print(f"[行情] {name}({symbol}) 未获取到有效数值")
        except Exception as e:
            print(f"[行情] {name}({symbol}) 获取失败: {e}")
    return results


def get_a_market():
    """获取 A 股核心指数（东方财富主源 + Yahoo 兜底）"""
    targets = [
        ("上证指数", "000001.SS"),
        ("深证成指", "399001.SZ"),
        ("创业板指", "399006.SZ"),
        ("沪深300", "000300.SS"),
    ]
    results = []
    try:
        df = ak.stock_zh_index_spot_em()
        for name, sym in targets:
            row = df[df["名称"] == name]
            if not row.empty:
                price = float(row["最新价"].values[0])
                pct = float(row["涨跌幅"].values[0])
                if math.isfinite(price) and math.isfinite(pct):
                    results.append({"name": name, "symbol": sym, "price": f"{price:,.2f}", "pct": pct})
    except Exception as e:
        print(f"[行情] 东方财富接口异常，切换备用源: {e}")

    if len(results) < 4:
        results = fetch_yf_group({name: sym for name, sym in targets})
    return results


def fetch_rss_within_24h(rss_dict, max_per_feed=6):
    """抓取过去 24 小时内的顶级 RSS 信源并按时间倒序排列"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(hours=24)
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
                        pub_dt = now_utc - datetime.timedelta(hours=12)
                        pub_str = "24h内"

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
    return "\n".join(all_lines)


def fetch_cls_telegraph(limit=50):
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


def analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw):
    """调用大模型去重、精译并输出严格结构化 JSON"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    now_str = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")

    prompt = f"""你是一位华尔街见闻与彭博风格的资深财经总编。当前北京时间：{now_str}。
请基于以下过去 24 小时内的顶级权威信源（彭博社、金融时报、路透社、WSJ、CNBC、财联社、BBC），去重合并同类事件，将英文报道精准翻译为专业中文，生成一份高密度、有深度的每日早报。

严格按以下 JSON 格式输出（不要输出任何 Markdown 代码块标记或多余说明）：
{{
  "mood": "bullish" 或 "bearish" 或 "neutral",
  "mood_headline": "一句话定调今日市场核心主题（20字以内）",
  "overview": "150-200字的今日全景总览。串联隔夜美股核心驱动、宏观利率/政策预期变化，以及今天A股与科技线需要重点关注的催化或风险。",
  "us_global_news": [
    {{
      "tag": "美股/美联储/科技/宏观/财报",
      "sentiment": "利好/利空/中性",
      "title": "高信息量中文标题（26字以内）",
      "fact": "核心事实与具体数据（70-110字，保留关键金额、百分比、公司与人物）",
      "impact": "市场逻辑与关联资产/板块影响（35-55字）",
      "source": "来源与时间（如 彭博市场 · 06:30）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ],
  "cn_a_news": [
    {{
      "tag": "A股/政策/产业/资金/港股",
      "sentiment": "利好/利空/中性",
      "title": "高信息量中文标题（26字以内）",
      "fact": "核心事实与具体数据（70-110字）",
      "impact": "对A股对应板块或个股的催化逻辑（35-55字）",
      "source": "来源与时间（如 财联社 · 08:05）"
    }}
  ],
  "general_news": [
    {{
      "tag": "国际/AI科技/商业/地缘",
      "title": "高信息量中文标题（26字以内）",
      "summary": "完整的事件脉络与核心看点（70-110字，不要只复述标题）",
      "source": "来源与时间（如 彭博科技 · 05:20）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ]
}}

严格要求：
1. us_global_news 精选 6 条最重要的美股与全球财经新闻（优先采用彭博、路透、FT、WSJ 报道）；cn_a_news 精选 5 条最具影响力的国内宏观/A股产业新闻；general_news 精选 4 条高价值国际时政或前沿科技新闻。
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
        temperature=0.25,
    )
    content = resp.choices[0].message.content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return json.loads(content)


# ================= 网页渲染引擎 =================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>全球财经与热点早报 · __DATE_STR__</title>
  <style>
    :root {
      --bg: #0a0e17;
      --bg-elevated: #111827;
      --bg-card: #131c2e;
      --bg-subtle: rgba(255, 255, 255, 0.035);
      --border: rgba(148, 163, 184, 0.14);
      --border-strong: rgba(148, 163, 184, 0.25);
      --text: #f1f5f9;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --accent: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.12);
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.14);
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.14);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.14);
      --shadow: 0 10px 30px -12px rgba(0, 0, 0, 0.55);
    }

    [data-theme="light"] {
      --bg: #f5f7fb;
      --bg-elevated: #ffffff;
      --bg-card: #ffffff;
      --bg-subtle: #f8fafc;
      --border: rgba(15, 23, 42, 0.09);
      --border-strong: rgba(15, 23, 42, 0.16);
      --text: #0f172a;
      --text-secondary: #475569;
      --text-muted: #64748b;
      --accent: #0284c7;
      --accent-soft: rgba(2, 132, 199, 0.1);
      --green: #059669;
      --green-bg: rgba(5, 150, 105, 0.1);
      --red: #dc2626;
      --red-bg: rgba(220, 38, 38, 0.1);
      --amber: #d97706;
      --amber-bg: rgba(217, 119, 6, 0.1);
      --shadow: 0 8px 24px -10px rgba(15, 23, 42, 0.08);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.65;
      -webkit-font-smoothing: antialiased;
      padding-bottom: 64px;
    }

    .container {
      max-width: 980px;
      margin: 0 auto;
      padding: 0 20px;
    }

    /* 顶部栏 */
    .topbar {
      position: sticky;
      top: 0;
      z-index: 50;
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      background: color-mix(in srgb, var(--bg) 82%, transparent);
      border-bottom: 1px solid var(--border);
    }
    .topbar-inner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 58px;
      gap: 12px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 15px;
      letter-spacing: 0.2px;
      color: var(--text);
      text-decoration: none;
    }
    .brand-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 12px var(--accent);
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .archive-select, .theme-btn {
      background: var(--bg-elevated);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 13px;
      cursor: pointer;
      transition: border-color 0.2s, background 0.2s;
    }
    .archive-select:hover, .theme-btn:hover {
      border-color: var(--border-strong);
    }

    /* 报头 Hero */
    .hero {
      padding: 32px 0 22px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .hero-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
      font-size: 13px;
      color: var(--text-secondary);
    }
    .mood-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
    }
    .mood-bullish { background: var(--red-bg); color: var(--red); }
    .mood-bearish { background: var(--green-bg); color: var(--green); }
    .mood-neutral { background: var(--accent-soft); color: var(--accent); }

    .hero h1 {
      font-size: clamp(22px, 4vw, 30px);
      font-weight: 800;
      letter-spacing: -0.4px;
      line-height: 1.3;
      margin-bottom: 14px;
    }

    /* 快捷导航 */
    .quick-nav {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .quick-nav a {
      text-decoration: none;
      font-size: 13px;
      color: var(--text-secondary);
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      padding: 5px 12px;
      border-radius: 999px;
      transition: all 0.18s;
    }
    .quick-nav a:hover {
      color: var(--accent);
      border-color: var(--accent);
    }

    /* 2x2 行情看板 */
    .market-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }
    .market-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px 18px;
      box-shadow: var(--shadow);
    }
    .market-card-title {
      font-size: 13px;
      font-weight: 700;
      color: var(--text-secondary);
      text-transform: uppercase;
      letter-spacing: 0.6px;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .market-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 7px 0;
      border-bottom: 1px dashed var(--border);
      gap: 10px;
    }
    .market-row:last-child { border-bottom: none; padding-bottom: 0; }
    .ticker-name {
      font-size: 14px;
      font-weight: 600;
      color: var(--text);
    }
    .ticker-right {
      display: flex;
      align-items: center;
      gap: 10px;
      font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }
    .ticker-price {
      font-size: 14px;
      font-weight: 600;
      color: var(--text);
    }
    .pct-badge {
      min-width: 72px;
      text-align: right;
      font-size: 12.5px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 6px;
    }
    .pct-green { color: var(--green); background: var(--green-bg); }
    .pct-red { color: var(--red); background: var(--red-bg); }
    .pct-flat { color: var(--text-secondary); background: var(--bg-subtle); }

    /* 今日全景总览 */
    .overview-box {
      background: linear-gradient(135deg, var(--bg-card) 0%, var(--bg-elevated) 100%);
      border: 1px solid var(--border);
      border-left: 4px solid var(--accent);
      border-radius: 14px;
      padding: 20px 22px;
      margin-bottom: 30px;
      box-shadow: var(--shadow);
    }
    .overview-label {
      font-size: 13px;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 8px;
      letter-spacing: 0.4px;
    }
    .overview-text {
      font-size: 15px;
      color: var(--text);
      line-height: 1.8;
    }

    /* 新闻分区 */
    .section {
      margin-bottom: 34px;
      scroll-margin-top: 74px;
    }
    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .section-title {
      font-size: 18px;
      font-weight: 800;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .section-count {
      font-size: 12px;
      color: var(--text-muted);
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      padding: 2px 9px;
      border-radius: 999px;
    }

    .news-list {
      display: grid;
      gap: 14px;
    }
    .news-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
      transition: transform 0.18s ease, border-color 0.18s ease;
      box-shadow: var(--shadow);
    }
    .news-card:hover {
      transform: translateY(-2px);
      border-color: var(--border-strong);
    }
    .news-top {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
    }
    .news-idx {
      font-family: ui-monospace, monospace;
      font-size: 12px;
      font-weight: 700;
      color: var(--text-muted);
    }
    .tag-pill {
      font-size: 11.5px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 6px;
      background: var(--accent-soft);
      color: var(--accent);
    }
    .sent-pill {
      font-size: 11.5px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 6px;
    }
    .sent-bull { background: var(--red-bg); color: var(--red); }
    .sent-bear { background: var(--green-bg); color: var(--green); }
    .sent-neu { background: var(--bg-subtle); color: var(--text-secondary); }

    .news-title {
      font-size: 16.5px;
      font-weight: 700;
      line-height: 1.45;
      margin-bottom: 8px;
      color: var(--text);
    }
    .news-fact {
      font-size: 14.5px;
      color: var(--text-secondary);
      line-height: 1.75;
      margin-bottom: 10px;
    }
    .news-impact {
      background: var(--bg-subtle);
      border-left: 3px solid var(--amber);
      border-radius: 0 8px 8px 0;
      padding: 8px 12px;
      font-size: 13.5px;
      color: var(--text);
      margin-bottom: 12px;
    }
    .news-impact span {
      font-weight: 700;
      color: var(--amber);
      margin-right: 4px;
    }
    .news-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 12.5px;
      color: var(--text-muted);
      padding-top: 8px;
      border-top: 1px solid var(--border);
    }
    .source-link {
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }
    .source-link:hover { text-decoration: underline; }

    footer {
      text-align: center;
      font-size: 12.5px;
      color: var(--text-muted);
      padding-top: 24px;
      border-top: 1px solid var(--border);
      line-height: 1.8;
    }

    @media (max-width: 720px) {
      .market-grid { grid-template-columns: 1fr; }
      .hero { padding: 22px 0 18px; }
      .news-card { padding: 15px 16px; }
      .topbar-inner { height: 54px; }
      .brand span.brand-sub { display: none; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="container topbar-inner">
      <a class="brand" href="__HOME_LINK__">
        <span class="brand-dot"></span>
        <span>DAILY BRIEF</span>
        <span class="brand-sub" style="font-weight:400;color:var(--text-muted);font-size:12px;">· 全球财经与热点早报</span>
      </a>
      <div class="controls">
        __ARCHIVE_SELECT__
        <button class="theme-btn" id="themeToggle" type="button" aria-label="切换主题">☀️ 浅色</button>
      </div>
    </div>
  </header>

  <main class="container">
    <section class="hero">
      <div class="hero-meta">
        <span>📅 __DATE_FULL__ __WEEKDAY__</span>
        <span>·</span>
        <span>🕒 生成于 __GEN_TIME__ (北京时间)</span>
        <span class="mood-pill __MOOD_CLASS__">__MOOD_LABEL__</span>
      </div>
      <h1>__MOOD_HEADLINE__</h1>
      <nav class="quick-nav">
        <a href="#markets">📊 跨市场行情</a>
        <a href="#us-global">🇺🇸 美股与全球财经</a>
        <a href="#cn-a">🇨🇳 A股与国内宏观</a>
        <a href="#general">🌐 国际时政与科技</a>
      </nav>
    </section>

    <section id="markets" class="market-grid">
      __MARKET_CARDS__
    </section>

    <section class="overview-box">
      <div class="overview-label">🧭 今日主编全景总览</div>
      <div class="overview-text">__OVERVIEW__</div>
    </section>

    <section id="us-global" class="section">
      <div class="section-header">
        <h2 class="section-title">🇺🇸 美股与全球财经</h2>
        <span class="section-count">__US_COUNT__ 条精选</span>
      </div>
      <div class="news-list">
        __US_NEWS_HTML__
      </div>
    </section>

    <section id="cn-a" class="section">
      <div class="section-header">
        <h2 class="section-title">🇨🇳 A 股与国内宏观</h2>
        <span class="section-count">__CN_COUNT__ 条精选</span>
      </div>
      <div class="news-list">
        __CN_NEWS_HTML__
      </div>
    </section>

    <section id="general" class="section">
      <div class="section-header">
        <h2 class="section-title">🌐 国际时政与前沿科技</h2>
        <span class="section-count">__GEN_COUNT__ 条精选</span>
      </div>
      <div class="news-list">
        __GEN_NEWS_HTML__
      </div>
    </section>

    <footer>
      <div>权威信源：Bloomberg · Financial Times · Reuters · WSJ · CNBC · 财联社 · BBC（严格 24 小时时效过滤）</div>
      <div>行情数据：Yahoo Finance &amp; 东方财富（代码直出，零模型幻觉） · 自动生成于 GitHub Actions</div>
    </footer>
  </main>

  <script>
    (function () {
      const root = document.documentElement;
      const btn = document.getElementById("themeToggle");
      const saved = localStorage.getItem("brief-theme") || "dark";
      function applyTheme(t) {
        root.setAttribute("data-theme", t);
        btn.textContent = t === "dark" ? "☀️ 浅色" : "🌙 深色";
      }
      applyTheme(saved);
      btn.addEventListener("click", function () {
        const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
        localStorage.setItem("brief-theme", next);
        applyTheme(next);
      });
    })();
  </script>
</body>
</html>
"""


def render_market_card_html(title, subtitle, data, is_us=True):
    rows_html = []
    if not data:
        rows_html.append('<div class="market-row"><span class="ticker-name" style="color:var(--text-muted)">暂无有效数据</span></div>')
    else:
        for item in data:
            pct = item["pct"]
            sign = "+" if pct > 0 else ""
            if pct == 0:
                cls = "pct-flat"
            elif is_us:
                cls = "pct-green" if pct > 0 else "pct-red"
            else:
                cls = "pct-red" if pct > 0 else "pct-green"
            rows_html.append(
                f'<div class="market-row">'
                f'  <span class="ticker-name">{esc(item["name"])}</span>'
                f'  <span class="ticker-right">'
                f'    <span class="ticker-price">{esc(item["price"])}</span>'
                f'    <span class="pct-badge {cls}">{sign}{pct:.2f}%</span>'
                f'  </span>'
                f'</div>'
            )
    return (
        f'<div class="market-card">'
        f'  <div class="market-card-title"><span>{esc(title)}</span><span style="font-weight:400;font-size:11px;color:var(--text-muted)">{esc(subtitle)}</span></div>'
        f'  {"".join(rows_html)}'
        f'</div>'
    )


def render_news_cards_html(items, with_impact=True):
    if not items:
        return '<div class="news-card"><div class="news-fact">今日暂无重要更新</div></div>'
    cards = []
    for i, item in enumerate(items, 1):
        tag = esc(item.get("tag", "要闻"))
        title = esc(item.get("title", ""))
        source = esc(item.get("source", ""))
        link = str(item.get("link", "")).strip()

        sent_html = ""
        if with_impact:
            s = item.get("sentiment", "中性")
            s_cls = "sent-bull" if s == "利好" else ("sent-bear" if s == "利空" else "sent-neu")
            sent_html = f'<span class="sent-pill {s_cls}">{esc(s)}</span>'

        body_text = esc(item.get("fact", "") if with_impact else item.get("summary", ""))
        impact_html = ""
        if with_impact and item.get("impact"):
            impact_html = f'<div class="news-impact"><span>逻辑推演</span>{esc(item.get("impact"))}</div>'

        link_html = (
            f'<a class="source-link" href="{esc(link)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a>'
            if link.startswith(("http://", "https://"))
            else "<span></span>"
        )

        cards.append(
            f'<article class="news-card">'
            f'  <div class="news-top">'
            f'    <span class="news-idx">#{i:02d}</span>'
            f'    <span class="tag-pill">{tag}</span>'
            f'    {sent_html}'
            f'  </div>'
            f'  <h3 class="news-title">{title}</h3>'
            f'  <p class="news-fact">{body_text}</p>'
            f'  {impact_html}'
            f'  <div class="news-footer">'
            f'    <span>{source}</span>'
            f'    {link_html}'
            f'  </div>'
            f'</article>'
        )
    return "\n".join(cards)


def build_archive_select_html(dates, current_date, is_archive=False):
    if not dates:
        return ""
    prefix = "" if is_archive else "archive/"
    options = []
    for d in dates:
        selected = " selected" if d == current_date else ""
        label = f"{d} (最新)" if d == dates[0] else d
        target = f"{prefix}{d}.html"
        options.append(f'<option value="{esc(target)}"{selected}>{esc(label)}</option>')
    return (
        '<select class="archive-select" aria-label="往期早报" '
        'onchange="if(this.value) window.location.href=this.value">'
        + "".join(options)
        + "</select>"
    )


def render_page_html(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data, archive_dates, is_archive=False):
    date_str = now.strftime("%Y-%m-%d")
    date_full = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood = llm_data.get("mood", "neutral")
    mood_map = {
        "bullish": ("mood-bullish", "🔥 风险偏好回升"),
        "bearish": ("mood-bearish", "❄️ 市场情绪偏谨慎"),
        "neutral": ("mood-neutral", "⚖️ 震荡分化格局"),
    }
    mood_cls, mood_label = mood_map.get(mood, ("mood-neutral", "📊 每日早报"))

    market_cards = "\n".join([
        render_market_card_html("🇺🇸 隔夜美股指数", "绿涨红跌", us_idx, is_us=True),
        render_market_card_html("🚀 美股科技风向", "绿涨红跌", tech_stocks, is_us=True),
        render_market_card_html("🇨🇳 A 股核心收盘", "红涨绿跌", a_idx, is_us=False),
        render_market_card_html("🌍 大宗 · 加密 · 汇率", "绿涨红跌", macro_assets, is_us=True),
    ])

    us_news = llm_data.get("us_global_news", [])
    cn_news = llm_data.get("cn_a_news", [])
    gen_news = llm_data.get("general_news", [])

    replacements = {
        "__DATE_STR__": esc(date_str),
        "__DATE_FULL__": esc(date_full),
        "__WEEKDAY__": esc(weekday),
        "__GEN_TIME__": esc(now.strftime("%H:%M")),
        "__MOOD_CLASS__": mood_cls,
        "__MOOD_LABEL__": esc(mood_label),
        "__MOOD_HEADLINE__": esc(llm_data.get("mood_headline") or f"{date_full} 全球财经与热点早报"),
        "__HOME_LINK__": "../index.html" if is_archive else "index.html",
        "__ARCHIVE_SELECT__": build_archive_select_html(archive_dates, date_str, is_archive=is_archive),
        "__MARKET_CARDS__": market_cards,
        "__OVERVIEW__": esc(llm_data.get("overview", "")),
        "__US_COUNT__": str(len(us_news)),
        "__CN_COUNT__": str(len(cn_news)),
        "__GEN_COUNT__": str(len(gen_news)),
        "__US_NEWS_HTML__": render_news_cards_html(us_news, with_impact=True),
        "__CN_NEWS_HTML__": render_news_cards_html(cn_news, with_impact=True),
        "__GEN_NEWS_HTML__": render_news_cards_html(gen_news, with_impact=False),
    }
    page = HTML_TEMPLATE
    for k, v in replacements.items():
        page = page.replace(k, v)
    return page


def save_web_pages(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data):
    """生成 docs/index.html 与 docs/archive/YYYY-MM-DD.html"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    date_str = now.strftime("%Y-%m-%d")
    manifest_file = ARCHIVE_DIR / "manifest.json"
    dates = []
    if manifest_file.exists():
        try:
            dates = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            dates = []

    if date_str not in dates:
        dates.append(date_str)
    dates = sorted(set(dates), reverse=True)[:60]  # 保留最近 60 天归档
    manifest_file.write_text(json.dumps(dates, ensure_ascii=False, indent=2), encoding="utf-8")

    # 写入首页与当日归档页
    index_html = render_page_html(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data, dates, is_archive=False)
    archive_html = render_page_html(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data, dates, is_archive=True)

    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")
    (ARCHIVE_DIR / f"{date_str}.html").write_text(archive_html, encoding="utf-8")
    # 防止 GitHub Pages 启用 Jekyll 干扰
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"已生成网页版早报：docs/index.html 与 docs/archive/{date_str}.html")


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


def build_feishu_summary_card(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data, site_url):
    """构建精致、不刷屏的飞书入口卡片（含直达网页按钮）"""
    today_str = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood_map = {
        "bullish": ("red", "🔥 风险偏好回升"),
        "bearish": ("green", "❄️ 市场情绪偏谨慎"),
        "neutral": ("blue", "⚖️ 震荡分化格局"),
    }
    header_color, mood_label = mood_map.get(llm_data.get("mood", "neutral"), ("blue", "📊 每日早报"))

    # 提炼 5 条最核心头条作为预览
    preview_lines = []
    for item in llm_data.get("us_global_news", [])[:3]:
        preview_lines.append(f"• **[{item.get('tag', '美股')}]** {item.get('title', '')}")
    for item in llm_data.get("cn_a_news", [])[:2]:
        preview_lines.append(f"• **[{item.get('tag', 'A股')}]** {item.get('title', '')}")
    preview_md = "\n".join(preview_lines)

    elements = [
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{"tag": "markdown", "content": f"🇺🇸 **美股指数**\n{format_market_md(us_idx, is_us=True)}"}],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{"tag": "markdown", "content": f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}"}],
                },
            ],
        },
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{"tag": "markdown", "content": f"🇨🇳 **A 股收盘**\n{format_market_md(a_idx, is_us=False)}"}],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{"tag": "markdown", "content": f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}"}],
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"🧭 **{llm_data.get('mood_headline', '今日核心总览')}**  ·  {mood_label}\n\n{llm_data.get('overview', '')}",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"📌 **核心要闻抢先看**\n{preview_md}",
        },
    ]

    if site_url:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📖 阅读完整图文网页版早报"},
                    "type": "primary",
                    "url": site_url,
                }
            ],
        })

    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"信源：Bloomberg / FT / Reuters / WSJ / CNBC / 财联社 / BBC | {now.strftime('%H:%M')}",
            }
        ],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_color,
                "title": {"tag": "plain_text", "content": f"☕ 全球财经与热点早报 | {today_str} {weekday}"},
            },
            "elements": elements,
        },
    }


def run_generate():
    now = datetime.datetime.now(BJ_TZ)
    print("1/3 正在抓取跨市场精准行情...")
    us_idx = fetch_yf_group({"纳斯达克": "^IXIC", "标普500": "^GSPC", "道琼斯": "^DJI", "费城半导体": "^SOX"})
    tech_stocks = fetch_yf_group({"英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT", "特斯拉": "TSLA"})
    macro_assets = fetch_yf_group({"黄金": "GC=F", "WTI原油": "CL=F", "BTC": "BTC-USD", "离岸RMB": "CNH=X"})
    a_idx = get_a_market()

    print("2/3 正在抓取过去 24 小时机构级信源...")
    global_fin_raw = fetch_rss_within_24h(GLOBAL_FINANCE_RSS, max_per_feed=6)
    general_raw = fetch_rss_within_24h(GENERAL_NEWS_RSS, max_per_feed=5)
    cn_fin_raw = fetch_cls_telegraph(limit=50)

    print("3/3 正在调用大模型生成早报并渲染网页...")
    llm_data = analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw)
    save_web_pages(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data)

    site_url = get_site_url()
    card_payload = build_feishu_summary_card(now, us_idx, tech_stocks, a_idx, macro_assets, llm_data, site_url)
    CARD_CACHE_FILE.write_text(json.dumps(card_payload, ensure_ascii=False), encoding="utf-8")
    print(f"网页地址已绑定为: {site_url or '未检测到仓库名'}")


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
    mode = sys.argv[1] if len(sys.argv) > 1 else "--all"
    if mode == "--generate":
        run_generate()
    elif mode == "--notify":
        run_notify()
    else:
        run_generate()
        run_notify()
