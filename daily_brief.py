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
