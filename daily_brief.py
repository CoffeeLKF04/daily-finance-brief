import os
import json
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

# 自定义综合新闻 RSS 源（后续可随时增删）
RSS_FEEDS = {
    "科技/商业": "https://36kr.com/feed",
    "全球/宏观": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "数码/热点": "https://sspai.com/feed"
}

# 北京时间 (UTC+8)
BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))
# ====================================================================


def get_us_market():
    """获取隔夜美股核心指数"""
    tickers = {"纳斯达克": "^IXIC", "标普500": "^GSPC", "道琼斯": "^DJI"}
    results = []
    for name, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if len(hist) >= 2:
                close = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2]
                pct = (close - prev) / prev * 100
                results.append({"name": name, "price": f"{close:,.2f}", "pct": pct})
        except Exception as e:
            print(f"美股 {name} 获取失败: {e}")
    return results


def get_a_market():
    """获取 A 股核心指数"""
    targets = ["上证指数", "深证成指", "创业板指"]
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
    """抓取财联社 7x24 财经电报 + 各类 RSS 综合新闻"""
    finance_texts = []
    general_texts = []

    try:
        df_cls = ak.stock_info_global_cls()
        for _, row in df_cls.head(25).iterrows():
            title = str(row.get("标题", "")).strip()
            content = str(row.get("内容", "")).strip()
            pub_time = str(row.get("发布时间", ""))
            finance_texts.append(f"[{pub_time}] {title} {content[:120]}")
    except Exception as e:
        print(f"财联社抓取失败: {e}")

    for category, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:6]:
                general_texts.append(f"[{category}] {entry.title}")
        except Exception as e:
            print(f"RSS {category} 抓取失败: {e}")

    return "\n".join(finance_texts), "\n".join(general_texts)


def analyze_with_llm(finance_raw, general_raw):
    """让大模型提炼并强制输出严格 JSON"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    today = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d")

    prompt = f"""你是一位资深投研主编。请根据今天({today})抓取的原始快讯，去重并提炼出高价值的每日早报。
严格按以下 JSON 格式输出，不要包含任何多余文字或 Markdown 代码块标记：
{{
  "mood": "bullish" 或 "bearish" 或 "neutral",
  "core_summary": [
    "一句话核心主线1（25字以内）",
    "一句话核心主线2（25字以内）",
    "一句话核心主线3（25字以内）"
  ],
  "finance_news": [
    {{
      "tag": "美股/A股/宏观/科技（选一个）",
      "sentiment": "利好/利空/中性",
      "title": "精炼标题（18字以内）",
      "desc": "核心逻辑与影响（45字以内）"
    }}
  ],
  "general_news": [
    {{
      "tag": "科技/国际/社会/商业",
      "title": "精炼标题（18字以内）",
      "desc": "一句话概要（40字以内）"
    }}
  ]
}}
要求：finance_news 精选 5 条最重要的财经新闻；general_news 精选 4 条有价值的综合/科技新闻。

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
    return json.loads(resp.choices[0].message.content)


def format_market_md(data, is_us=False):
    """格式化行情文本（美股绿涨红跌，A股红涨绿跌）"""
    if not data:
        return "暂无数据"
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


def build_feishu_card(us_data, a_data, llm_data):
    """动态拼装飞书卡片 JSON"""
    now = datetime.datetime.now(BJ_TZ)
    today_str = now.strftime("%Y年%m月%d日")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    mood_color_map = {"bullish": "red", "bearish": "green", "neutral": "wathet"}
    header_color = mood_color_map.get(llm_data.get("mood", "neutral"), "blue")

    summary_md = "\n".join([f"• {s}" for s in llm_data.get("core_summary", [])])

    fin_lines = []
    for item in llm_data.get("finance_news", []):
        s = item.get("sentiment", "中性")
        s_color = "red" if s == "利好" else ("green" if s == "利空" else "grey")
        fin_lines.append(
            f"**【{item['tag']}】{item['title']}** <font color='{s_color}'>[{s}]</font>\n"
            f"<font color='grey'>{item['desc']}</font>"
        )
    finance_md = "\n\n".join(fin_lines)

    gen_lines = []
    for item in llm_data.get("general_news", []):
        gen_lines.append(
            f"**【{item['tag']}】{item['title']}**\n"
            f"<font color='grey'>{item['desc']}</font>"
        )
    general_md = "\n\n".join(gen_lines)

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_color,
                "title": {
                    "tag": "plain_text",
                    "content": f"☕ 每日财经与热点早报 | {today_str} {weekday}"
                }
            },
            "elements": [
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
                                {"tag": "markdown", "content": f"🇺🇸 **隔夜美股**\n{format_market_md(us_data, is_us=True)}"}
                            ]
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {"tag": "markdown", "content": f"🇨🇳 **A 股收盘**\n{format_market_md(a_data, is_us=False)}"}
                            ]
                        }
                    ]
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"💡 **今日核心主线**\n{summary_md}"
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"📈 **美股 / A 股与宏观要闻**\n\n{finance_md}"
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"🌐 **科技与全球热点**\n\n{general_md}"
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"数据来源：Yahoo Finance / 东方财富 / 财联社 / RSS | 生成时间：{now.strftime('%H:%M')}"
                        }
                    ]
                }
            ]
        }
    }
    return card


def main():
    print("正在抓取行情与新闻...")
    us_data = get_us_market()
    a_data = get_a_market()
    fin_raw, gen_raw = get_news_raw()

    print("正在调用大模型整理...")
    llm_data = analyze_with_llm(fin_raw, gen_raw)

    print("正在推送到飞书...")
    card_payload = build_feishu_card(us_data, a_data, llm_data)
    resp = requests.post(FEISHU_WEBHOOK, json=card_payload, timeout=10)
    print("推送结果:", resp.text)


if __name__ == "__main__":
    main()
