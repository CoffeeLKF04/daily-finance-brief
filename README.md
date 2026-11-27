# 全球财经早报 / 收盘报 / 晚报

每天三个时点，抓取官方一手公告 + 全球财经媒体 + 国内四家快讯，配合跨市场行情，
调用大模型做去重、精译、重磅评级，生成一个手机端优先的静态网页，并推送一张飞书入口卡片。

> 本文档面向「接手这个仓库的人或大模型」。读完应该能直接改代码，不用先通读 2000 行。
> 最后更新：2026-11-28，对应提交 `3daa994`。

---

## 1. 运行链路

```
服务器 admin 的 crontab（时间在服务器上，不在 GitHub）
  08:28 周一~周六 → trigger_brief.sh am
  15:28 周一~周五 → trigger_brief.sh close
  21:58 周一~周五 → trigger_brief.sh pm
        │
        │  POST /repos/{owner}/{repo}/actions/workflows/brief.yml/dispatches
        │  （fine-grained PAT，只有 Actions: write，够派发、不能推代码）
        ▼
GitHub Actions: .github/workflows/brief.yml
  1. checkout → setup-python 3.11 → pip install -r requirements.txt
  2. python daily_brief.py --generate --edition <am|close|pm|auto>
        · 抓行情 → 抓信源 → LLM 评级 → 写 docs/ → 写 latest_feishu_card.json
  3. git add docs/ && commit && push        → 触发 GitHub Pages 构建
  4. sleep 30
  5. python daily_brief.py --notify          → 读卡片缓存 → POST 飞书 webhook
```

**关键点：GitHub 侧没有任何 `schedule`，定时完全由服务器 cron 提供。**
所以「改推送时间」要么改服务器上的 crontab，要么给 workflow 加 `schedule`（注意 Actions 的定时是 UTC 且会延迟几分钟）。

### 串行化

`brief.yml` 里配了 `concurrency: group: daily-finance-brief, cancel-in-progress: false`。
两次运行同时 `git push` 会让后者 non-fast-forward 失败，而且是在烧完 LLM token 之后才失败，
所以后来的排队等待，绝不取消正在跑的那次。

---

## 2. 目录结构

| 路径 | 说明 |
|---|---|
| `daily_brief.py` | **唯一主程序**（约 2150 行），抓数据 + 渲染 + 推送全在这 |
| `.github/workflows/brief.yml` | 生产流水线，只支持 `workflow_dispatch`，输入 `edition` |
| `.github/workflows/probe.yml` | 手动诊断用，实测各候选信源可用性，**不碰飞书** |
| `probe_feeds.py` | 上者的探针脚本，改动信源前先跑它 |
| `requirements.txt` | **锁死版本**，不要放开（见「已知坑」） |
| `docs/index.html` | 当前最新一期（Pages 首页） |
| `docs/archive/<YYYY-MM-DD>-<am\|close\|pm>.html` | 每期独立页面 |
| `docs/archive/manifest.json` | 期号索引，倒序，上限 180 期 |
| `docs/.nojekyll` | 关掉 Jekyll，保证 `_` 开头的资源能访问 |
| `latest_feishu_card.json` | 运行期产物，`--generate` 写、`--notify` 读，**不进版本库** |

---

## 3. `daily_brief.py` 地图

行号是当前提交下的参考值，改动后会漂移，**优先按函数名搜索**。

### 3.1 配置与工具（18–215 行）

| 函数 / 常量 | 作用 |
|---|---|
| `OFFICIAL_RSS` / `GLOBAL_FINANCE_RSS` / `GENERAL_NEWS_RSS` | 三组 RSS 信源，值是 URL 列表（列表内按顺序尝试，有一个成功就 `break`） |
| `resolve_edition(arg, now)` | `auto` 判定：<12 点 am、<19 点 close、否则 pm |
| `get_edition_meta(edition)` | 每档的图标、标题、`rss_hours`、飞书配色等 |
| `get_site_url()` | 优先读 `SITE_URL`，否则由 `GITHUB_REPOSITORY` 推 Pages 地址（owner 转小写） |
| `esc(text)` | **所有写进 HTML 的文本都必须过它** |
| `safe_score(val)` | 把 LLM 给的分数夹到 1–5 整数，异常返回 3 |
| `archive_sort_key` / `format_archive_label` | 归档排序（同日 pm>close>am）与下拉框短标签 |

### 3.2 行情层（222–610 行）

| 函数 | 作用 |
|---|---|
| `_build_quote(...)` | **行情统一出口**，也做脏数据护栏：价格非正、涨跌幅越界一律丢弃 |
| `_pct_limit(symbol)` | 单日涨跌幅上限：指数 12%、加密 40%、期货/外汇 15%、其他 25% |
| `_yf_quote(name, symbol)` | Yahoo 行情，处理「日线最后一根是不是今天」以统一昨收口径 |
| `fetch_yf_group(dict)` | 8 线程并发拉一组 Yahoo 标的 |
| `_load_index_frames()` | 按可靠性加载 A 股指数快照，最多取 2 个源做交叉校验 |
| `_index_history(sym)` | 指数 7 日走势，东财失败退新浪 |
| `_match_index_row(...)` | 先按名称（含别名）精确匹配，再按代码匹配 |
| `_quote_from_index_row(...)` | 用官方涨跌幅**反推**昨收，而不是直接信「昨收」字段 |
| `get_a_market()` | A股/港股：东财 → 新浪 → Yahoo 三级源，逐只补齐 + 双源校验 + 缺失清单 |

**标的定义**（改这里就改页面上的行情卡）：

- `A_INDEX_TARGETS` — 11 个，`(显示名, 国内代码, Yahoo代码, 名称别名列表)`，Yahoo 代码为空表示无兜底
- `US_INDEX_TARGETS` — 7 个；`US_STOCK_TARGETS` — 12 个；`MACRO_TARGETS` — 10 个

### 3.3 信源层（612–895 行）

| 函数 | 作用 |
|---|---|
| `fetch_rss_within_hours(dict, hours, max_per_feed)` | 8 线程并发拉 RSS，按发布时间过滤窗口；**命中少于 8 条时自动放宽到 24h**；无时间戳的条目标为「近期」并排在有时间条目之后 |
| `_call_cls_api()` | 财联社接口，兼容 `stock_news_global_cls` 与 `stock_info_global_cls` 两个名字 |
| `fetch_cls_telegraph` / `fetch_sina_7x24` / `fetch_em_flash` / `fetch_ths_flash` | 四个国内快讯源，各自返回 `[{source, ts, text, link}]` |
| `_dedup_key(text)` | 去标点空格后取前 34 字做指纹，吃掉同事件转载 |
| `fetch_cn_flash(limit=90)` | 四源聚合 → 去重 → 按时间倒序混排，返回纯文本行 |

### 3.4 大模型层（897–1085 行）

| 函数 | 作用 |
|---|---|
| `analyze_with_llm(...)` | 组装 prompt（版本定位 + 信源权重 + 1–5 分评分标准），发起调用 |
| `_extract_json(text)` | 宽容解析：容忍 ` ``` ` 围栏、前后寒暄、尾部杂质 |
| `_call_llm_json(client, prompt, attempts=3)` | 失败重试 3 次，递增退避 |
| `_fallback_llm_data(edition)` | 全失败时的降级数据：行情与页面结构照常，新闻区标注不可用 |
| `normalize_and_sort_llm_data(data, edition)` | 代码层清洗 score/sentiment/link、按分数降序，并按 am/close/pm 配额截断，最终不信任模型顺序 |

**LLM 输出契约**（`us_global_news` / `cn_a_news` / `general_news` 三个列表）：

```
score       1-5 整数，5 = 头版级（每期通常 2-4 条）
tag         栏目
sentiment   利好 / 利空 / 中性（仅 us、cn 两组）
title       26 字以内中文标题
fact        70-110 字事实与数据（us、cn）
summary     70-110 字事件脉络（general）
impact      35-55 字影响逻辑（us、cn）
source      来源与时间
link        原文链接，没有则空
```

### 3.5 渲染层（1087–1961 行）

| 函数 | 作用 |
|---|---|
| `HTML_TEMPLATE` | 单文件模板，内联 CSS + 约 30 行 JS，深色默认、浅色可切、`localStorage` 记忆 |
| `render_sparkline_svg` | 手写 SVG 走势线（68×22），无图表库依赖 |
| `render_market_group_html` | 行情组卡：整组 `<details>` 折叠 + 组内「展开其余 N 项」，每条带独立 SVG 走势线 |
| `render_must_read_html` | 首屏「必读」横向 chip，带右侧滚动提示，锚点跳到对应新闻并自动切 Tab |
| `render_news_cards_html` | 每条新闻一个 `<details>`，只有首条 5 分默认展开，标签按栏目高亮 |
| `render_news_sections_html` | 三个板块做成 Tab；**close 版顺序调整为 A股优先**；每个 Tab 可一键展开/收起全部新闻 |
| `render_page_html` | 占位符替换，组装整页 |
| `save_web_pages` | 写 `index.html`、独立归档页、`manifest.json`，并清理超期孤儿页 |

### 3.6 飞书层（1963–2091 行）与入口（2093–2153 行）

| 函数 | 作用 |
|---|---|
| `format_market_md` | 卡片内行情小表，只显示前 4 项，其余提示「见网页」 |
| `build_feishu_summary_card` | 2×2 四宫格 + 定调 + 概览 + Top5 预览 + 网页按钮 |
| `run_generate(edition)` | 主流程编排：行情 → 信源 → LLM → 落盘 → 卡片缓存 |
| `run_notify()` | 读缓存 POST webhook，检查 `code`/`StatusCode` 判断成功 |

**飞书卡片注脚刻意保留「早报」二字**，因为机器人已配置该安全关键词，删了会发不出去。

---

## 4. 三个版本

| | `am` 早报 | `close` 收盘报 | `pm` 晚报 |
|---|---|---|---|
| 实际触发 | 08:28 | 15:28 | 21:58 |
| RSS 窗口 `rss_hours` | 16h | 10h | 10h |
| 官方源窗口 | max(48, rss_hours) | 同左 | 同左 |
| 定位 | 隔夜美股 + 盘前催化 | A股全天复盘 + 日间催化 | 欧盘 + 美股盘前 |
| 新闻配额 | 美6 / 国5 / 国际4 | 美4 / 国6 / 国际4 | 美6 / 国4 / 国际4 |
| 行情卡顺序 | 美股·科技·A股·大宗 | **A股**·大宗·美股·科技 | 美股·科技·大宗·A股 |
| Tab 顺序 | 美股·A股·国际 | **A股**·美股·国际 | 美股·A股·国际 |
| 主题色 | 天蓝 `#38bdf8` | 琥珀 `#f59e0b` | 靛蓝 `#818cf8` |
| 命名 | `...-am.html` | `...-close.html` | `...-pm.html` |

---

## 5. Secrets / 环境变量

在仓库 Settings → Secrets and variables → Actions 配置：

| 名称 | 必需 | 说明 |
|---|---|---|
| `LLM_API_KEY` | ✓ | 大模型 Key |
| `LLM_BASE_URL` | ✓ | 默认 `https://api.deepseek.com` |
| `LLM_MODEL` | ✓ | 默认 `deepseek-chat` |
| `FEISHU_WEBHOOK` | ✓ | 飞书机器人 webhook |
| `SITE_URL` | 可选 | 自定义域名，不设则由 `GITHUB_REPOSITORY` 推导 |

---

## 6. 常见修改 → 去哪里改

| 想做的事 | 改哪里 |
|---|---|
| 加/删一个 RSS 信源 | 第 33–113 行的三个字典，加一行 `"显示名": ["url"]` |
| 加一个国内快讯源 | 写一个 `fetch_xxx(limit)` 返回 `[{source,ts,text,link}]`，再登记进 `fetch_cn_flash` 的 `collectors` |
| 加减行情标的 | `A_INDEX_TARGETS` / `US_INDEX_TARGETS` / `US_STOCK_TARGETS` / `MACRO_TARGETS` |
| 调评分标准或配文口吻 | `analyze_with_llm` 里的 prompt 文本 |
| 改每档新闻条数 | 同上，`count_req` 变量 |
| 改飞书卡片排版 | `build_feishu_summary_card` 的 `elements` 列表 |
| 改推送时间 | **服务器** `crontab -u admin -e`；或给 workflow 加 `schedule`（UTC） |
| 改网页样式 | `HTML_TEMPLATE` 里的 CSS |
| 改归档保留期 | `save_web_pages` 里的 `[:180]` |
| 改行情脏数据阈值 | `_pct_limit`（A股普通指数 12%，创业板/科创50 22%，北证50 32%） |
| 改页面交互 | `HTML_TEMPLATE` 里的 JS：主题持久化、Tab、全部展开/收起、回顶按钮、锚点高亮 |

---

## 7. 改代码前必须知道的约定

1. **任何写进 HTML 的外部文本都要 `esc()`**。信源标题、LLM 输出一律不可信。
2. **行情数值必须走 `_build_quote`**，它同时负责格式化与脏数据拦截；绕过它就会把 `nan` 或离谱涨跌幅发到页面上。
3. **LLM 输出必须过 `normalize_and_sort_llm_data`**，不要相信模型自己排的序和分数。
4. **单个信源函数失败要返回 `[]` 或 `""`，不要抛异常**。整条链是「尽力而为」哲学：缺一个源不影响出报。
5. **`edition` 的 code 与文件名、颜色、Tab 顺序是绑定的**，新增档位要同时改 `resolve_edition`、`get_edition_meta`、`render_page_html` 的三处 `if/elif`。
6. **`docs/` 只由程序写**，不要手改；`git add docs/` 是 workflow 里唯一的提交范围。

---

## 8. 已知坑

| 坑 | 说明 |
|---|---|
| **akshare 会双向改名** | 1.20.41 有 `stock_zh_index_daily_em`、1.20.70 换成 `index_zh_daily_sina`；`index_zh_spot_em` 与 `stock_zh_index_spot_em` 也是互相替代。代码用候选链覆盖了两头，**别再写死单个函数名**，并保持 `requirements.txt` 锁版本 |
| 北证50 无兜底 | `A_INDEX_TARGETS` 里它的 Yahoo 代码是空串，快照源没给就只能缺，日志会明说 |
| 没有时间戳的 RSS | 现在标为「近期」并排到有时间戳条目之后，不再伪装成窗口中点 |
| A股指数阈值不同 | 创业板/科创50可到 ±20%，北证50可到 ±30%；`_pct_limit` 已按指数类型放宽，普通指数仍拦在 ±12% |
| 推送 token 权限 | 服务器上的 fine-grained PAT 只有 `Actions: write`，**能派发、不能 push 代码**。要改代码得用别的凭据（如 `gh` OAuth 会话） |
| Node 20 弃用 | actions 已升 `checkout@v5` / `setup-python@v6`（node24），不要再退回 v4/v5 |
| Pages 有延迟 | 提交后固定 `sleep 30` 再发卡片，偶尔仍可能点开是旧页面 |

---

## 9. 本地验证

```bash
# 语法
python -m py_compile daily_brief.py

# 完整生成（需要 4 个 secret 对应的环境变量）
LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=... \
python daily_brief.py --generate --edition am

# 只发卡片（读 latest_feishu_card.json）
FEISHU_WEBHOOK=... python daily_brief.py --notify
```

远端触发与排查：

```bash
gh workflow run brief.yml -f edition=am          # 手动出一期
gh run list --workflow=brief.yml --limit 5
gh run watch <run-id> --exit-status               # 实时跟到底
gh run view <run-id> --log | grep '\[行情\]\|\[LLM\]\|\[RSS\]'
```

日志里的关键标记：`[行情]` 数据源与缺失、`[RSS] 本次共注入 N 条`、`[国内快讯] 各源命中 {...}`、
`[LLM] 第 x/3 次调用失败`（出现说明在重试，连续出现说明走了降级）、`[归档] 已清理 N 个`。

---

## 10. 维护记录

| 日期 | 变更 |
|---|---|
| 2026-11-27 | 接入官方一手源与国内四源快讯；标的扩容（指数 11 / 美股 7 / 个股 12 / 大宗 10）；行情卡折叠 |
| 2026-11-28 | 依赖锁版本、并发串行化、LLM 重试与降级、归档孤儿清理、`.gitignore`、Actions 升 node24、A股兜底失败不再静默 |
