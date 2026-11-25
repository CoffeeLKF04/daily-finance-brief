import os
import re
import json
import datetime
import requests
import akshare as ak
import yfinance as yf
import feedparser
from openai import OpenAI

# ================= 配置区 =================
FEISHU_WEBHOOK = os.environ["FEISHU_WEBHOOK"]
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

RSS_FEEDS = {
    "科技商业": "https://36kr.com/feed",
    "全球时政": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "数码前沿": "https://sspai.com/feed",
    "深度商业": "https://www.huxiu.com/rss/0.xml"
}

BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))
# ==========================================


def clean_html(raw_html):
    """清除 RSS 摘要中的 HTML 标签"""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", "", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def fetch_yf_group(tickers_dict):
    """批量获取 Yahoo Finance 行情"""
    results = []
    for name, symbol in tickers_dict.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if len(hist) >= 2:
                close = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2]
                pct = (close - prev) / prev * 100
                results.append({"name": name, "price": f"{close:,.2f}", "pct": pct})
        except Exception as e:
            print(f"{name}({symbol}) 获取失败: {e}")
    return results


def get_a_market():
    """获取 A 股核心指数"""
    targets = ["上证指数", "深证成指", "创业板指", "科创50"]
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
        print(f"A股行情获取失败: {e}")
    return results


def get_news_raw():
    """抓取带正文的财联社电报 + 带摘要和链接的 RSS 综合新闻"""
    finance_texts = []
    general_texts = []

    # 1. 财联社 7x24 电报（保留完整正文，取最新 30 条）
    try:
        df_cls = ak.stock_info_global_cls()
        for _, row in df_cls.head(30).iterrows():
            title = str(row.get("标题", "")).strip()
            content = str(row.get("内容", "")).strip()
            pub_time = str(row.get("发布时间", ""))
            finance_texts.append(f"[{pub_time}] 标题:{title} | 正文:{content[:300]}")
    except Exception as e:
        print(f"财联社抓取失败: {e}")

    # 2. RSS 综合新闻（同时抓取标题、正文摘要、原文链接）
    for category, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:6]:
                title = entry.get("title", "").strip()
                summary = clean_html(entry.get("summary", entry.get("description", "")))[:220]
                link = entry.get("link", "")
                general_texts.append(f"[{category}] 标题:{title} | 摘要:{summary} | 链接:{link}")
        except Exception as e:
            print(f"RSS {category} 抓取失败: {e}")

    return "\n".join(finance_texts), "\n".join(general_texts)


def analyze_with_llm(finance_raw, general_raw):
    """让大模型深度整合，输出结构化投研内容"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    today = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d")

    prompt = f"""你是一位华尔街见闻/彭博风格的资深财经主编。请根据今天({today})的原始资讯，去重合并同类事件，撰写一份信息密度高、有深度的每日早报。

严格按以下 JSON 格式输出（不要输出任何 Markdown 代码块标记或多余解释）：
{{
  "mood": "bullish" 或 "bearish" 或 "neutral",
  "overview": "120-160字的今日市场总览。串联隔夜美股主线、宏观预期与今天A股需要重点关注的板块或风险，语言干练专业。",
  "finance_news": [
    {{
      "tag": "美股/A股/宏观/半导体/政策（选一个）",
      "sentiment": "利好/利空/中性",
      "title": "信息量充足的标题（25字以内）",
      "fact": "核心事实与具体数据（60-90字，保留关键数字、公司名、政策细节）",
      "impact": "逻辑推演与关联板块/标的（30-50字）"
    }}
  ],
  "general_news": [
    {{
      "tag": "科技/国际/商业/社会",
      "title": "信息量充足的标题（25字以内）",
      "summary": "完整的事件脉络与看点（60-90字，不要只复述标题）",
      "link": "从原始素材中提取对应的原文链接，没有则留空字符串"
    }}
  ]
}}

要求：
1. finance_news 精选 6 条最具市场影响力的财经要闻，不要流水账。
2. general_news 精选 5 条高质量的科技、商业或国际热点，务必保留原始链接。

【财经原始电报】：
{finance_raw}

【综合原始新闻】：
{general_raw}
"""
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3
    )
    content = resp.choices[0].message.content.strip()
    # 兼容部分模型可能自带 ```json 包裹的情况
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return json.loads(content)


def format_market_md(data, is_us=False):
    """格式化行情列表"""
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
        lines.append(f"**{item['name']}**  `{item['price']}`  <font color='{color}'>{sign}{pct:.2f}%</font>")
    return "\n".join(lines)


def build_feishu_card(us_idx, tech_stocks, a_idx, macro_assets, llm_data):
    """拼装高密度、分层清晰的飞书交互式卡片"""
    now = datetime.datetime.now(BJ_TZ)
    today_str = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood_map = {
        "bullish": ("red", "🔥 市场情绪偏暖"),
        "bearish": ("green", "❄️ 市场情绪偏冷"),
        "neutral": ("blue", "⚖️ 市场震荡观望")
    }
    header_color, mood_label = mood_map.get(llm_data.get("mood", "neutral"), ("blue", "📊 每日早报"))

    elements = [
        # 1. 第一行行情：美股大盘 + 焦点科技股
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
                        {"tag": "markdown", "content": f"🇺🇸 **美股大盘**\n{format_market_md(us_idx, is_us=True)}"}
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"🚀 **焦点科技**\n{format_market_md(tech_stocks, is_us=True)}"}
                    ]
                }
            ]
        },
        # 2. 第二行行情：A股核心 + 大宗/加密
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
                        {"tag": "markdown", "content": f"🇨🇳 **A 股收盘**\n{format_market_md(a_idx, is_us=False)}"}
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "markdown", "content": f"🌍 **大宗与加密**\n{format_market_md(macro_assets, is_us=True)}"}
                    ]
                }
            ]
        },
        {"tag": "hr"},
        # 3. 主编综述
        {
            "tag": "markdown",
            "content": f"🧭 **今日市场总览**  |  {mood_label}\n\n{llm_data.get('overview', '')}"
        },
        {"tag": "hr"},
        # 4. 财经要闻标题
        {
            "tag": "markdown",
            "content": "📈 **深度财经与市场要闻**"
        }
    ]

    # 逐条添加财经要闻（分层排版：标题 + 事实 + 影响）
    for i, item in enumerate(llm_data.get("finance_news", []), 1):
        s = item.get("sentiment", "中性")
        s_color = "red" if s == "利好" else ("green" if s == "利空" else "grey")
        block = (
            f"**{i}. 【{item['tag']}】{item['title']}**  <font color='{s_color}'>[{s}]</font>\n"
            f"📌 {item.get('fact', '')}\n"
            f"<font color='grey'>↳ 影响：{item.get('impact', '')}</font>"
        )
        elements.append({"tag": "markdown", "content": block})

    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "🌐 **科技、商业与全球热点**"})

    # 逐条添加综合新闻（带原文直达链接）
    for i, item in enumerate(llm_data.get("general_news", []), 1):
        link = item.get("link", "").strip()
        link_md = f"  [阅读原文 ↗]({link})" if link.startswith("http") else ""
        block = (
            f"**{i}. 【{item['tag']}】{item['title']}**\n"
            f"{item.get('summary', '')}{link_md}"
        )
        elements.append({"tag": "markdown", "content": block})

    # 底部注脚
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"数据来源：Yahoo Finance / 东方财富 / 财联社 / 36氪 / 虎嗅 / BBC | 生成时间：{now.strftime('%H:%M')}"
            }
        ]
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_color,
                "title": {
                    "tag": "plain_text",
                    "content": f"☕ 全球财经与热点早报 | {today_str} {weekday}"
                }
            },
            "elements": elements
        }
    }


def main():
    print("正在抓取跨市场行情...")
    us_idx = fetch_yf_group({"纳斯达克": "^IXIC", "标普500": "^GSPC", "道琼斯": "^DJI", "费城半导体": "^SOX"})
    tech_stocks = fetch_yf_group({"英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT", "特斯拉": "TSLA"})
    macro_assets = fetch_yf_group({"黄金": "GC=F", "WTI原油": "CL=F", "BTC": "BTC-USD", "美元/离岸RMB": "CNH=X"})
    a_idx = get_a_market()

    print("正在抓取深度新闻...")
    fin_raw, gen_raw = get_news_raw()

    print("正在调用大模型撰写深度早报...")
    llm_data = analyze_with_llm(fin_raw, gen_raw)

    print("正在推送到飞书...")
    card_payload = build_feishu_card(us_idx, tech_stocks, a_idx, macro_assets, llm_data)
    resp = requests.post(FEISHU_WEBHOOK, json=card_payload, timeout=15)
    print("推送结果:", resp.text)


if __name__ == "__main__":
    main()
