# Market Sentiment Skills

*[English version below](#english-version)*

---

## 中文版

### 这个项目是干什么的

一句话：**帮你判断一只股票该不该动，但不让 AI 自己瞎编。**

常见的场景是——你 watchlist 里的票跌了，你想知道是该跑、该观望，还是该加仓。直接问 AI「这只票怎么了」，它会去刷一圈新闻然后给你一个听起来很有道理但没法验证的答案。

这个项目换了个做法：先用一套**确定性的程序**把数据拉齐、整理成结构化的「证据包」，再让 AI 只在这份整理好的证据上做最后那一步判断。AI 不负责找信息，只负责推理。

项目是一个 **Agent Skill**，配一套 Python 运行时。**没有 API key 依赖** —— 所有数据源（SEC EDGAR、Yahoo Finance、Stooq）都是免费且无需认证的。

---

### Market Sentiment Research —— 单只票深度复核

针对**单只票的下跌事件**做复核。

每天 pipeline 跑完，先检查 watchlist 里每只票有没有异常下跌（跌幅超阈值、跑输大盘、创新低）。只有触发的票才进入复核，pipeline 会同时从几个方向拉数据：

- **价格**：Yahoo Finance 优先，自动 fallback 到 Stooq / 本地缓存
- **官方披露**：SEC EDGAR 的财报、filing、财务数字
- **分析师参考**：Yahoo quoteSummary 拉取机构共识目标价和近期升降级记录，作为仅供参考的附加证据（不进打分体系）
- **社交信号**（可选）：Agent 使用自己的浏览工具获取 Reddit、X 等平台的讨论链接。如果环境无法访问这些来源，社交数据会被标记为不可用，评分会自动调整。

整理完后输出两样东西：一份给人看的中文日终报告，和一份给 Agent 用的结构化证据包。Agent 读完证据后，给出五种动作之一：`Reject`（不用管）/ `Watch`（观望）/ `Starter`（建小仓）/ `Add`（加仓）/ `Exit`（减仓）。

给出观望或加仓建议时，Agent 必须写清楚「什么情况下这个判断失效」，这些条件会被记下来，之后每天自动复查。

**状态**：核心流程已经跑通，能端到端出报告，目前在持续打磨和部署实盘追踪。

---

### 快速上手

需要 Python `>= 3.11`。

```bash
# 安装
python3 -m pip install -e .

# 配置必需的环境变量
export SEC_USER_AGENT="YourName your.email@example.com"

# 验证配置
market-sentiment --config config/watchlist.toml preflight

# 跑日终
market-sentiment --config config/watchlist.toml run-daily
```

**仅需**：`SEC_USER_AGENT` 环境变量（格式为 `"Name email@example.com"`，SEC 用来识别你的脚本）。

**嫌麻烦？** 直接把这个仓库交给 Claude Code，告诉它「帮我把环境配好，我要跑 market-sentiment-research」，它会自己读文档、问你要信息、写配置、跑 preflight 直到跑通。

---

### 目录结构

```text
skills/
  market-sentiment-research/   # 唯一的 Skill
sharing-resources/
  src/market_sentiment/        # 共用的 Python 运行时
  references/ docs/ scripts/   # 文档和工具脚本
  secrets/                     # 本地密钥，不提交 git
config/watchlist.toml          # 日常运行配置
data/                          # 生成的报告、缓存、证据包
PROJECT_REVIEW.md              # 实现历程和决策记录
```

---

## English Version

### What This Is

In one line: **it helps you decide whether to act on a stock — without letting the AI just make things up.**

The usual situation: something in your watchlist drops, and you want to know whether to exit, hold, or add. Ask an AI directly and it'll skim some news and hand you an answer that sounds reasonable but can't be verified.

This project does it differently. A **deterministic pipeline** first gathers the data and assembles it into a structured *evidence packet*. Only then does the AI step in — to reason over that packet. The AI doesn't fetch information; it only does the final judgment.

The project is one **Agent Skill** with a Python runtime. **No API keys required** — all data sources (SEC EDGAR, Yahoo Finance, Stooq) are free and keyless.

---

### Market Sentiment Research — single-name review

Reviews a **pullback on a single stock**.

After each daily run, the pipeline checks every watchlist ticker for an abnormal drop (drawdown past threshold, underperforming the benchmark, fresh low). Only triggered tickers get reviewed, pulling data from several lanes at once:

- **Price**: Yahoo Finance first, with automatic fallback to Stooq / local cache
- **Official disclosures**: SEC EDGAR filings, financials, company facts
- **Analyst context**: Yahoo `quoteSummary` pulls institutional consensus price targets and recent upgrade/downgrade activity as advisory-only evidence (not factored into the scoring system)
- **Social signals** (optional): The Agent fetches discussion URLs (Reddit, X) using its own browsing tools. If the environment cannot reach those sources, social data is marked unavailable and scoring adjusts automatically.

The result is two outputs: a human-readable end-of-day report, and a structured evidence packet for the Agent. After reading the evidence, the Agent picks one of five actions: `Reject` / `Watch` / `Starter` / `Add` / `Exit`.

For a Watch or Add call, the Agent must spell out what would invalidate the thesis. Those conditions are recorded and re-checked automatically every day.

**Status**: the core flow works end to end and produces reports; currently being polished with forward-tracking deployment.

---

### Quick Start

Requires Python `>= 3.11`.

```bash
# Install
python3 -m pip install -e .

# Set the required environment variable
export SEC_USER_AGENT="YourName your.email@example.com"

# Validate config
market-sentiment --config config/watchlist.toml preflight

# Run the daily pipeline
market-sentiment --config config/watchlist.toml run-daily
```

**Only needed**: `SEC_USER_AGENT` environment variable (format: `"Name email@example.com"` — SEC uses this to identify your script).

**Don't want to do this by hand?** Hand the repo to Claude Code and say "set up the environment so I can run market-sentiment-research" — it'll read the docs, ask you for information, write the config, and run preflight until everything passes.

---

### Layout

```text
skills/
  market-sentiment-research/   # the only Skill
sharing-resources/
  src/market_sentiment/        # shared Python runtime
  references/ docs/ scripts/   # docs and utility scripts
  secrets/                     # local credentials, not committed
config/watchlist.toml          # daily run config
data/                          # generated reports, caches, evidence packets
PROJECT_REVIEW.md              # implementation history and decisions
```
