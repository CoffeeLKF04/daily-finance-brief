import os
import re
import json
import calendar
import datetime
import requests
import akshare as ak
import yfinance as yf
import feedparser
from openai import OpenAI

# ================= 配置区（自动读取 GitHub Secrets） =================
FEISHU_WEBHOOK = os.environ["FEISHU_WEBHOOK"]
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 国际权威一手财经 RSS（海外节点直连，大模型直译）
GLOBAL_FINANCE_RSS = {
    "WSJ市场": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "WSJ商业": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
    "CNBC财经": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "Yahoo财经": "https://finance.yahoo.com/news/rssindex",
}

# 国际时政与前沿科技 RSS
GENERAL_NEWS_RSS = {
    "BBC中文": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "CNBC科技": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    "TechCrunch": "https://techcrunch.com/feed/",
}
# ====================================================================


def clean_html(raw_html):
    """清除 HTML 标签与多余空白"""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def fetch_yf_group(tickers_dict):
    """批量获取 Yahoo Finance 行情"""
    results = []
    for name, symbol in tickers_dict.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if len(hist) >= 2:
                close = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                pct = (close - prev) / prev * 100
                results.append({"name": name, "price": f"{close:,.2f}", "pct": pct})
        except Exception as e:
            print(f"[行情] {name}({symbol}) 获取失败: {e}")
    return results


def get_a_market():
    """获取 A 股核心指数（优先东方财富，海外偶发超时自动切换 Yahoo Finance 兜底）"""
    targets = ["上证指数", "深证成指", "创业板指", "沪深300"]
    results = []
    try:
        df = ak.stock_zh_index_spot_em()
        for name in targets:
            row = df[df["名称"] == name]
            if not row.empty:
                price = float(row["最新价"].values[0])
                pct = float(row["涨跌幅"].values[0])
                results.append({"name": name, "price": f"{price:,.2f}", "pct": pct})
    except Exception as e:
        print(f"[行情] 东方财富接口异常，切换备用源: {e}")

    if len(results) < 4:
        fallback = {
            "上证指数": "000001.SS",
            "深证成指": "399001.SZ",
            "创业板指": "399006.SZ",
            "沪深300": "000300.SS",
        }
        results = fetch_yf_group(fallback)
    return results


def fetch_rss_within_24h(rss_dict, max_per_feed=8):
    """抓取 RSS 并严格过滤 24 小时内的新闻，附带北京时间"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(hours=24)
    items = []

    for source_name, url in rss_dict.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            count = 0
            for entry in feed.entries:
                if count >= max_per_feed:
                    break
                time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_str = "24h内"
                if time_struct:
                    pub_dt = datetime.datetime.fromtimestamp(
                        calendar.timegm(time_struct), tz=datetime.timezone.utc
                    )
                    if pub_dt < cutoff:
                        continue
                    pub_str = pub_dt.astimezone(BJ_TZ).strftime("%m-%d %H:%M")

                title = clean_html(entry.get("title", ""))
                summary = clean_html(entry.get("summary", entry.get("description", "")))[:240]
                link = entry.get("link", "").strip()
                if title:
                    items.append(
                        f"[{source_name} | {pub_str}] 标题:{title} | 摘要:{summary} | 链接:{link}"
                    )
                    count += 1
        except Exception as e:
            print(f"[RSS] {source_name} 抓取失败: {e}")
    return "\n".join(items)


def fetch_cls_telegraph(limit=50):
    """抓取财联社 7x24 实时电报（拉取 50 条覆盖隔夜至早盘）"""
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
请基于以下过去 24 小时内的权威一手信源，去重合并同类事件，将英文信源自然精准地翻译为专业中文，生成一份高密度、高价值的每日早报。

严格按以下 JSON 格式输出（不要输出任何 Markdown 代码块标记或多余说明）：
{{
  "mood": "bullish" 或 "bearish" 或 "neutral",
  "overview": "130-170字的今日全景总览。串联隔夜美股核心驱动、宏观预期变化，以及今天A股与科技线需要重点关注的催化或风险。",
  "us_global_news": [
    {{
      "tag": "美股/美联储/科技/宏观/财报",
      "sentiment": "利好/利空/中性",
      "title": "高信息量中文标题（24字以内）",
      "fact": "核心事实与具体数据（60-90字，保留关键金额、百分比、公司与人物）",
      "impact": "市场逻辑与关联资产/板块影响（30-45字）",
      "source": "来源与时间（如 WSJ市场 · 06:30）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ],
  "cn_a_news": [
    {{
      "tag": "A股/政策/产业/资金/港股",
      "sentiment": "利好/利空/中性",
      "title": "高信息量中文标题（24字以内）",
      "fact": "核心事实与具体数据（60-90字）",
      "impact": "对A股对应板块或个股的催化逻辑（30-45字）",
      "source": "来源与时间（如 财联社 · 08:05）"
    }}
  ],
  "general_news": [
    {{
      "tag": "国际/AI科技/商业/地缘",
      "title": "高信息量中文标题（24字以内）",
      "summary": "完整的事件脉络与核心看点（60-90字，不要只复述标题）",
      "source": "来源与时间（如 BBC中文 · 05:20）",
      "link": "提取对应的原始链接，没有则留空"
    }}
  ]
}}

严格要求：
1. us_global_news 精选 5 条最重要的美股与全球财经新闻；cn_a_news 精选 4 条最具影响力的国内宏观/A股产业新闻；general_news 精选 4 条高价值国际时政或前沿科技新闻。
2. 严禁编造数据或链接，所有事实必须来自下方原始素材。

【全球与美股一手财经信源】：
{global_fin_raw if global_fin_raw else "（暂无数据，请从国内电报中提取海外财经要闻补充）"}

【国内财联社 7x24 实时电报】：
{cn_fin_raw if cn_fin_raw else "（暂无数据，请从国际信源中提取中国市场相关内容补充）"}

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


def format_market_md(data, is_us=False):
    """格式化行情列表（美股/大宗绿涨红跌，A股红涨绿跌）"""
    if not data:
        return "<font color='grey'>暂未获取到数据</font>"
    lines = []
    for item in data:
        pct = item["pct"]
        sign = "+" if pct > 0 else ""
        if is_us:
            color = "green" if pct > 0 else ("red" if pct < 0 else "grey")
        else:
            color = "red" if pct > 0 else ("green" if pct < 0 else "grey")
        lines.append(
            f"**{item['name']}**  `{item['price']}`  <font color='{color}'>{sign}{pct:.2f}%</font>"
        )
    return "\n".join(lines)


def build_news_block(items, with_impact=True):
    """将新闻列表格式化为视觉层次分明的 Markdown"""
    blocks = []
    for i, item in enumerate(items, 1):
        tag = item.get("tag", "要闻")
        title = item.get("title", "")
        source = item.get("source", "")
        link = item.get("link", "").strip()

        sentiment_md = ""
        if with_impact:
            s = item.get("sentiment", "中性")
            s_color = "red" if s == "利好" else ("green" if s == "利空" else "grey")
            sentiment_md = f" <font color='{s_color}'>[{s}]</font>"

        meta_parts = []
        if source:
            meta_parts.append(source)
        meta_str = f"<font color='grey'>{' | '.join(meta_parts)}</font>" if meta_parts else ""
        link_md = f" · [阅读原文 ↗]({link})" if link.startswith("http") else ""

        if with_impact:
            body = (
                f"**{i}. 【{tag}】{title}**{sentiment_md}\n"
                f"{item.get('fact', '')}\n"
                f"<font color='grey'>↳ 逻辑：{item.get('impact', '')}</font>\n"
                f"{meta_str}{link_md}"
            )
        else:
            body = (
                f"**{i}. 【{tag}】{title}**\n"
                f"{item.get('summary', '')}\n"
                f"{meta_str}{link_md}"
            )
        blocks.append(body)
    return "\n\n".join(blocks) if blocks else "<font color='grey'>今日暂无重要更新</font>"


def build_feishu_card(us_idx, tech_stocks, a_idx, macro_assets, llm_data):
    """拼装高密度飞书交互式卡片"""
    now = datetime.datetime.now(BJ_TZ)
    today_str = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood_map = {
        "bullish": ("red", "🔥 风险偏好回升"),
        "bearish": ("green", "❄️ 市场情绪偏谨慎"),
        "neutral": ("blue", "⚖️ 震荡分化格局"),
    }
    header_color, mood_label = mood_map.get(llm_data.get("mood", "neutral"), ("blue", "📊 每日早报"))

    elements = [
        # 第一行行情：美股大盘 + 焦点科技
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"🇺🇸 **隔夜美股指数**\n{format_market_md(us_idx, is_us=True)}"}
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"🚀 **美股科技风向**\n{format_market_md(tech_stocks, is_us=True)}"}
                    ],
                },
            ],
        },
        # 第二行行情：A股核心 + 大宗加密汇率
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"🇨🇳 **A 股核心收盘**\n{format_market_md(a_idx, is_us=False)}"}
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"🌍 **大宗 · 加密 · 汇率**\n{format_market_md(macro_assets, is_us=True)}"}
                    ],
                },
            ],
        },
        {"tag": "hr"},
        # 市场全景总览
        {
            "tag": "markdown",
            "content": f"🧭 **今日全景总览**  ·  {mood_label}\n\n{llm_data.get('overview', '')}",
        },
        {"tag": "hr"},
        # 板块 1：美股与全球财经
        {
            "tag": "markdown",
            "content": f"🇺🇸 **美股与全球财经**\n\n{build_news_block(llm_data.get('us_global_news', []), with_impact=True)}",
        },
        {"tag": "hr"},
        # 板块 2：A股与国内宏观
        {
            "tag": "markdown",
            "content": f"🇨🇳 **A 股与国内宏观**\n\n{build_news_block(llm_data.get('cn_a_news', []), with_impact=True)}",
        },
        {"tag": "hr"},
        # 板块 3：国际时政与前沿科技
        {
            "tag": "markdown",
            "content": f"🌐 **国际时政与前沿科技**\n\n{build_news_block(llm_data.get('general_news', []), with_impact=False)}",
        },
        {"tag": "hr"},
        # 底部注脚
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"信源：WSJ / CNBC / Yahoo Finance / 财联社 / BBC / TechCrunch（严格24h过滤） | 推送时间：{now.strftime('%H:%M')}",
                }
            ],
        },
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_color,
                "title": {
                    "tag": "plain_text",
                    "content": f"☕ 全球财经与热点早报 | {today_str} {weekday}",
                },
            },
            "elements": elements,
        },
    }


def main():
    print("1/4 正在抓取跨市场精准行情...")
    us_idx = fetch_yf_group({"纳斯达克": "^IXIC", "标普500": "^GSPC", "道琼斯": "^DJI", "费城半导体": "^SOX"})
    tech_stocks = fetch_yf_group({"英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT", "特斯拉": "TSLA"})
    macro_assets = fetch_yf_group({"黄金": "GC=F", "WTI原油": "CL=F", "BTC": "BTC-USD", "离岸RMB": "CNH=X"})
    a_idx = get_a_market()

    print("2/4 正在抓取过去 24 小时权威信源...")
    global_fin_raw = fetch_rss_within_24h(GLOBAL_FINANCE_RSS, max_per_feed=8)
    general_raw = fetch_rss_within_24h(GENERAL_NEWS_RSS, max_per_feed=6)
    cn_fin_raw = fetch_cls_telegraph(limit=50)

    print("3/4 正在调用大模型精译与整合...")
    llm_data = analyze_with_llm(global_fin_raw, cn_fin_raw, general_raw)

    print("4/4 正在推送飞书早报卡片...")
    card_payload = build_feishu_card(us_idx, tech_stocks, a_idx, macro_assets, llm_data)
    resp = requests.post(FEISHU_WEBHOOK, json=card_payload, timeout=15)
    print("飞书响应:", resp.text)

    res_json = resp.json()
    if resp.status_code != 200 or (res_json.get("code", 0) != 0 and res_json.get("StatusCode", 0) != 0):
        raise RuntimeError(f"飞书推送失败: {resp.text}")


if __name__ == "__main__":
    main()
