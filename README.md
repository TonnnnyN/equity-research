# Market Sentiment Skills

---

## 中文版

这个项目本质上是两个并列的 Agent Skill，加上一套共享的 Python 运行时。目前第一个 Skill（市场情绪研究）已经完整落地，第二个（美股小中盘错位筛选）还在开发中。

---

### 这东西是干什么的

核心场景：某天你 watchlist 里的票跌了，你想知道这次跌是该跑、该观望、还是该加仓——但你不想让 AI 自己去刷一遍新闻然后随便说个结论。

`market-sentiment-research` 做的事是：先用一套确定性 pipeline 把这次下跌事件整理成结构化的证据包，再让 Agent 只在这份压缩好的证据上做最终判断。LLM 在这里不是信息检索工具，它只负责最后那一步推理。

---

### 整体逻辑

![workflow](docs/workflow.png)

> 图片待放入

---

### Skill 1：Market Sentiment Research（主线，已完成）

#### 触发逻辑

pipeline 每天跑完后，先按配置检查每只票有没有触发异常下跌：10日/20日跌幅超阈值、相对 benchmark 表现明显偏离、创新低。只有触发的票才会进入后续复核流程，不触发的直接跳过。

#### 数据 Lane

触发之后，pipeline 同时从多个方向拉数据：

- **价格 Lane**：优先走 Tiger Trade，港股自动 fallback 到 Yahoo Finance，再往后是 Alpha Vantage → Stooq → 本地 SQLite 缓存。哪个源成功用哪个，失败自动往下走，最终来源会写进 review packet 里。

- **官方披露 Lane**：拉 SEC EDGAR 的 submissions、近期 filing 列表和 company facts（财务数字）。filing 缓存已接入持久化。

- **宏观 Lane**：FRED 的 10 年期国债利率（DGS10）和联邦基金利率（DFF），可选 EIA 能源数据。

- **社交 Lane**：Reddit 是当前主力来源，Discourse 论坛可选配，X/Twitter 默认关闭（需要配 cookie）。抓完帖子之后，由 DeepSeek 做第一层压缩——每条帖子判 bull/bear/neutral + 置信度 + 一句话中文摘要。判过的帖子缓存 14 天，同一条帖子不会重复送给 LLM。

- **存储 Lane**：所有价格、官方事件、基本面、宏观、社交快照和判决结果都落 SQLite，复跑时直接复用。

#### 输出结构

每次跑完会生成两类产物：

1. `data/reports/<date>/manual_agent_report.zh.md`：给人看的中文日终报告，覆盖所有触发的票。

2. `data/reports/<date>/review_packets/<TICKER>.json`：给 Agent 用的结构化证据包。

evidence packet 分两层：

- **Layer 1（仅供参考）**：`bucket_scores`、`rule_engine_precheck`、`decision_summary`，来自确定性规则引擎，用来快速做 sanity check，不是最终结论。

- **Layer 2（真正的证据）**：`price_context`、`official_events`、`fundamentals_snapshot`、`social_summary`、`macro_summary`、`source_health`，这才是 Agent 要先看的东西。

**Agent 的阅读顺序**：先看 Layer 2 自己形成判断，再对照 Layer 1。如果最终动作和规则引擎不一致，必须说清楚是哪条 Layer 2 证据导致了分歧。

#### 最终动作

Agent 给出五种动作之一：

| 动作 | 含义 |
|---|---|
| `Reject` | 不值得关注，或者有硬 veto（比如 SEC 披露含负面关键词、财务结构性恶化） |
| `Watch` | 值得跟踪但还不到动手的时候 |
| `Starter` | 可以建小仓位 |
| `Add` | 可以加仓 |
| `Exit` | 减仓或清仓 |

给出 `Watch / Starter / Add` 时，必须写明 `invalidate_if`（什么情况下这个判断失效）和 `rerate_if`（什么情况下需要重新评估）。这些条件会被写入 `data/decisions/` 目录，之后每次 daily run 会自动检查有没有触发。

---

### Skill 2：U.S. Small/Mid Dislocation（开发中）

第二个 Skill 做的是更宽的初筛：给一张准备好的美股小中盘 CSV 宽表，过滤掉红旗（破产、going concern、造假、退市风险等），找出"价格跌得比近期基本面严重"的候选票，输出 `Pass / Watchlist / Investigate`。

它不做最终买入判断，只是候选生成。真要做单票决策，还是走第一个 Skill。

因为要同时扫几十只票，这个 Skill 计划强制使用并发 subagent，每只票一个 subagent 并行处理，主 agent 只负责汇总。具体实现还在规划中。

---

### 嫌麻烦？让 Agent 来配

如果不想手动一步步配环境，可以直接把这个仓库扔给 Claude Code（或其他支持读取本地仓库的 Agent），告诉它：

> 帮我把这个项目的运行环境配好，我需要跑 `market-sentiment-research`。

Agent 会自己读 `SKILL.md`、`secrets.example.sh`、`configuration_and_secrets.md` 等文档，问你要 API key，帮你写 `secrets.sh`，跑 `preflight` 验证配置，直到跑通为止。

---

### 安装

需要 Python `>= 3.11`。

```bash
python3 -m pip install -e .
```

如果需要 X/Twitter 社交源：

```bash
python3 -m pip install -e ".[social]"
```

---

### 配置密钥

真实 API key 放在本地，不提交 git：

```bash
sharing-resources/secrets/market_sentiment.secrets.sh
```

模板在：

```bash
sharing-resources/secrets/market_sentiment.secrets.example.sh
```

运行前加载：

```bash
source sharing-resources/secrets/market_sentiment.secrets.sh
```

主要环境变量：

| 变量 | 用途 | 是否必须 |
|---|---|---|
| `TIGER_CONFIG_PATH` | Tiger Trade 凭证目录 | 是 |
| `ALPHAVANTAGE_API_KEY` | 价格备用源，免费 key 25次/天 | 是 |
| `FRED_API_KEY` | 宏观数据 | 是 |
| `SEC_USER_AGENT` | SEC EDGAR 访问，格式：`"名字 email"` | 是 |
| `DEEPSEEK_API_KEY` | 社交帖子情绪判断 | 是 |
| `EIA_API_KEY` | 能源数据，可选 | 否 |

Tiger 凭证需要单独放两个文件到 `sharing-resources/secrets/tiger_api/` 目录：
- `tiger_openapi_config.properties`
- `tiger_openapi_token.properties`（在 Tiger 开发者后台生成）

---

### 日常使用

```bash
# 验证配置
market-sentiment --config config/watchlist.toml preflight

# 跑日终
market-sentiment --config config/watchlist.toml run-daily

# 指定日期
market-sentiment --config config/watchlist.toml run-daily --date 2026-03-26

# 顺带发邮件
market-sentiment --config config/watchlist.toml run-daily --email

# 查历史报告
market-sentiment --config config/watchlist.toml show-report --date 2026-03-26
```

---

### 其他脚本

刷新价格缓存：

```bash
python3 skills/market-sentiment-research/scripts/update_price_cache.py
```

清理旧缓存（建议挂 cron，每天跑）：

```bash
python3 sharing-resources/scripts/purge_caches.py \
  --db-path data/state/market_sentiment.sqlite3 \
  --social-days 14
```

跑测试：

```bash
python3 -m pytest sharing-resources/tests
```

---

### 目录结构

```text
skills/
  market-sentiment-research/   # 主 Skill
  us-smallmid-dislocation/     # 开发中

sharing-resources/
  src/market_sentiment/        # Python 运行时引擎
  tests/                       # 单测
  scripts/                     # 工具脚本
  docs/                        # 架构和运维说明
  references/                  # 配置、密钥、数据、输出参考
  secrets/                     # 本地凭证，不提交

config/watchlist.toml          # 日常运行配置
data/                          # 生成的报告、缓存、review packet
PROJECT_REVIEW.md              # 实现历程和决策记录
```

---

---

## English Version

This project is two sibling Agent Skills sharing a common Python runtime. The first Skill (market sentiment research) is fully built and running. The second (U.S. small/mid dislocation screening) is still in development.

---

### What This Is For

The core scenario: something in your watchlist drops, and you want to know whether to exit, hold, or add — without just asking an AI to summarize news and make something up.

`market-sentiment-research` works by first running a deterministic pipeline that assembles the pullback event into a structured evidence packet, then letting the Agent reason only over that compressed evidence. The LLM doesn't do information retrieval here — it only handles the final reasoning step.

---

### How It Works

![workflow](docs/workflow.png)

> Image to be inserted

---

### Skill 1: Market Sentiment Research (main, complete)

#### Trigger Logic

After each daily run, the pipeline checks every ticker in your watchlist for abnormal drops: 10-day/20-day drawdown exceeding thresholds, meaningful underperformance vs. benchmark, or a fresh low. Only triggered tickers proceed to the full review. Others are skipped.

#### Data Lanes

Once a ticker triggers, the pipeline pulls from multiple directions in parallel:

- **Price lane**: Tiger Trade first, with automatic fallback to Yahoo Finance for HK-listed stocks, then Alpha Vantage → Stooq → local SQLite cache. The pipeline records which source actually delivered in the review packet.

- **Official disclosures lane**: SEC EDGAR submissions, recent filing list, and company facts (filed financials). Filing cache is persisted to SQLite.

- **Macro lane**: FRED 10-year Treasury yield (DGS10) and Fed funds rate (DFF). EIA energy data is optional.

- **Social lane**: Reddit is the primary active source. Discourse forums are optionally configurable. X/Twitter is off by default (requires cookie setup). Posts are first compressed by DeepSeek — each post gets a bull/bear/neutral label, a confidence score, and a one-line Chinese summary. Judged posts are cached for 14 days and won't be re-sent to the LLM.

- **Storage lane**: All prices, official events, fundamentals, macro, social snapshots, and sentiment judgments are stored in SQLite and reused on subsequent runs.

#### Output Structure

Each run produces two outputs:

1. `data/reports/<date>/manual_agent_report.zh.md` — a human-readable Chinese end-of-day report covering all triggered tickers.

2. `data/reports/<date>/review_packets/<TICKER>.json` — structured evidence packets for Agent or manual review.

The evidence packet has two layers:

- **Layer 1 (advisory only)**: `bucket_scores`, `rule_engine_precheck`, `decision_summary` — from the deterministic rule engine. These are a quick sanity check, not the final answer.

- **Layer 2 (the actual evidence)**: `price_context`, `official_events`, `fundamentals_snapshot`, `social_summary`, `macro_summary`, `source_health` — this is what the Agent should read first.

**Reading order for the Agent**: form your own view from Layer 2 first, then compare against Layer 1. If your conclusion differs from the rule engine's, you must state which specific Layer 2 evidence drove the divergence.

#### Final Actions

The Agent outputs one of five actions:

| Action | Meaning |
|---|---|
| `Reject` | Not worth pursuing, or a hard veto applies (e.g., negative SEC disclosure keywords, structural financial deterioration) |
| `Watch` | Worth tracking but not acting yet |
| `Starter` | Start a small position |
| `Add` | Add to an existing position |
| `Exit` | Reduce or close |

For `Watch / Starter / Add`, the Agent must write explicit `invalidate_if` conditions (when the thesis breaks) and `rerate_if` conditions (when to reassess). These are written to `data/decisions/` and automatically checked on every subsequent daily run.

---

### Skill 2: U.S. Small/Mid Dislocation (in development)

The second Skill runs a broader first-pass screen: given a prepared CSV universe of U.S. small/mid-cap names, it filters out hard red flags (bankruptcy, going concern, fraud, delisting risk, etc.) and surfaces names where price action looks significantly worse than recent business fundamentals. Outputs are `Pass / Watchlist / Investigate`.

This Skill does not make final buy decisions — it generates candidates. Single-stock decisions go through Skill 1.

Because it needs to scan dozens of tickers simultaneously, this Skill is planned to use concurrent subagents (one per candidate ticker), with the main Agent only handling final aggregation. The detailed implementation is still being designed.

---

### Let an Agent Set It Up For You

If you'd rather not go through the setup steps manually, hand the repo to Claude Code (or any Agent that can read local files) and say:

> Help me get the environment configured to run `market-sentiment-research`.

The Agent will read `SKILL.md`, `secrets.example.sh`, `configuration_and_secrets.md`, and related docs on its own, ask you for API keys, write the `secrets.sh` file, run `preflight` to validate, and keep going until everything passes.

---

### Install

Requires Python `>= 3.11`.

```bash
python3 -m pip install -e .
```

For X/Twitter social provider:

```bash
python3 -m pip install -e ".[social]"
```

---

### Secrets Setup

Real API keys live locally and are not committed to git:

```bash
sharing-resources/secrets/market_sentiment.secrets.sh
```

Template:

```bash
sharing-resources/secrets/market_sentiment.secrets.example.sh
```

Load before running:

```bash
source sharing-resources/secrets/market_sentiment.secrets.sh
```

Key environment variables:

| Variable | Purpose | Required |
|---|---|---|
| `TIGER_CONFIG_PATH` | Tiger Trade credentials directory | Yes |
| `ALPHAVANTAGE_API_KEY` | Price fallback source; free key is 25 calls/day | Yes |
| `FRED_API_KEY` | Macro data | Yes |
| `SEC_USER_AGENT` | SEC EDGAR access; format: `"Name email"` | Yes |
| `DEEPSEEK_API_KEY` | Social sentiment judging | Yes |
| `EIA_API_KEY` | Energy data, optional | No |

Tiger credentials go in `sharing-resources/secrets/tiger_api/`:
- `tiger_openapi_config.properties`
- `tiger_openapi_token.properties` (generate in Tiger developer console)

---

### Daily Usage

```bash
# Check config
market-sentiment --config config/watchlist.toml preflight

# Run daily pipeline
market-sentiment --config config/watchlist.toml run-daily

# Run for a specific date
market-sentiment --config config/watchlist.toml run-daily --date 2026-03-26

# Run and send email
market-sentiment --config config/watchlist.toml run-daily --email

# View a past report
market-sentiment --config config/watchlist.toml show-report --date 2026-03-26
```

---

### Other Scripts

Refresh price cache:

```bash
python3 skills/market-sentiment-research/scripts/update_price_cache.py
```

Purge old cache (recommended as a daily cron job):

```bash
python3 sharing-resources/scripts/purge_caches.py \
  --db-path data/state/market_sentiment.sqlite3 \
  --social-days 14
```

Run tests:

```bash
python3 -m pytest sharing-resources/tests
```

---

### Directory Layout

```text
skills/
  market-sentiment-research/   # main Skill
  us-smallmid-dislocation/     # in development

sharing-resources/
  src/market_sentiment/        # Python runtime engine
  tests/                       # unit tests
  scripts/                     # utility scripts
  docs/                        # architecture and operations docs
  references/                  # config, secrets, data, output references
  secrets/                     # local credentials, not committed

config/watchlist.toml          # daily run config
data/                          # reports, caches, review packets
PROJECT_REVIEW.md              # implementation history and decisions
```
