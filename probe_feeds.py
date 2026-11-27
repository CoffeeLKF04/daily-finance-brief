"""临时探针：在 GitHub Actions 环境里实测各候选新闻源的可用性。"""
import json
import time
from datetime import datetime, timezone

import feedparser
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}

RSS_CANDIDATES = {
    # --- 官方一手源 ---
    "美联储Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "美国劳工局BLS": "https://www.bls.gov/feed/bls_latest.rss",
    "美国SEC": "https://www.sec.gov/news/pressreleases.rss",
    "欧洲央行ECB": "https://www.ecb.europa.eu/rss/press.html",
    "美财政部": "https://home.treasury.gov/rss/press.xml",
    # --- 媒体直连 ---
    "MarketWatch头条": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "MarketWatch实时": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "YahooFinance": "https://finance.yahoo.com/news/rssindex",
    "CNBC经济": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "CNBC世界": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "Investing商品": "https://www.investing.com/rss/news_11.rss",
    "Investing经济": "https://www.investing.com/rss/news_14.rss",
    "日经亚洲": "https://asia.nikkei.com/rss/feed/nar",
    "南华早报商业": "https://www.scmp.com/rss/92/feed",
    "半岛电视台": "https://www.aljazeera.com/xml/rss/all.xml",
    "FT首页": "https://www.ft.com/rss/home",
    "路透社官方": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
    "路透社outbound": "https://www.reuters.com/arc/outboundfeeds/rss/?outputType=xml",
    "美联社AP": "https://apnews.com/index.rss",
    "Google新闻商业(中文)": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "Google新闻财经(英文)": "https://news.google.com/rss/search?q=stock+market+when:1d&hl=en-US&gl=US&ceid=US:en",
}

JSON_CANDIDATES = {
    "新浪财经7x24": "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=20&zhibo_id=152&tag_id=0",
    "华尔街见闻": "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&client=pc&limit=20",
    "金十数据": "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1",
}

AK_CANDIDATES = [
    ("财联社(新名)", "stock_news_global_cls", {"symbol": "全部"}),
    ("东方财富快讯", "stock_info_global_em", {}),
    ("同花顺快讯", "stock_info_global_ths", {}),
    ("新浪快讯", "stock_info_global_sina", {}),
    ("富途快讯", "stock_info_global_futu", {}),
    ("财经早餐", "stock_info_cjzc_em", {}),
    ("全球财经快讯CJZC", "stock_info_global_cls", {}),
]


def probe_rss(name, url):
    t0 = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        cost = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return f"FAIL   {name:<18} HTTP {r.status_code} ({cost}ms)"
        feed = feedparser.parse(r.content)
        entries = feed.entries
        if not entries:
            return f"EMPTY  {name:<18} 0 条 ({cost}ms)"
        latest = ""
        ts = entries[0].get("published_parsed") or entries[0].get("updated_parsed")
        if ts:
            latest = datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
        title = (entries[0].get("title") or "")[:60]
        return f"OK     {name:<18} {len(entries):>3} 条 | 最新 {latest or '无时间'} | {cost}ms | {title}"
    except Exception as e:
        return f"ERR    {name:<18} {type(e).__name__}: {str(e)[:70]}"


def probe_json(name, url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        body = r.text.strip()
        return f"HTTP {r.status_code} | {len(body)}B | {body[:260]}"
    except Exception as e:
        return f"ERR {type(e).__name__}: {str(e)[:80]}"


def main():
    print("=" * 100)
    print("RSS 候选源")
    print("=" * 100)
    for name, url in RSS_CANDIDATES.items():
        print(probe_rss(name, url))

    print()
    print("=" * 100)
    print("JSON 快讯接口")
    print("=" * 100)
    for name, url in JSON_CANDIDATES.items():
        print(f"--- {name} ---")
        print(probe_json(name, url))

    print()
    print("=" * 100)
    print("akshare 快讯函数")
    print("=" * 100)
    try:
        import akshare as ak

        print("akshare 版本:", ak.__version__)
        for label, fname, kwargs in AK_CANDIDATES:
            fn = getattr(ak, fname, None)
            if fn is None:
                print(f"MISS   {label:<18} akshare 无 {fname}")
                continue
            try:
                df = fn(**kwargs)
                if df is None or df.empty:
                    print(f"EMPTY  {label:<18} 返回空表")
                    continue
                print(f"OK     {label:<18} {len(df):>3} 行 | 列: {list(df.columns)[:8]}")
                first = df.iloc[0].to_dict()
                print(f"       样例: {json.dumps({k: str(v)[:80] for k, v in list(first.items())[:6]}, ensure_ascii=False)}")
            except Exception as e:
                print(f"ERR    {label:<18} {type(e).__name__}: {str(e)[:80]}")
    except Exception as e:
        print("akshare 导入失败:", e)


if __name__ == "__main__":
    main()
