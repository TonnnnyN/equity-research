# 美股小中盘错位筛选 Skill —— 架构设计与开发计划

> 文档定位：本文件专门服务 `us-smallmid-dislocation` 这一个 Skill 的开发。
> `PROJECT_REVIEW.md` 主要记录 `market-sentiment-research` 的实现历程，其中零散提到本 Skill 的内容（特别是 0.6 节「subagent 用在哪个 skill」的待定项）在此文统一收口并定稿。
> 创建日期：2026-05-17
> 状态：架构已定稿，待派 Sonnet subagent 实现。

---

## 一、Skill 定位回顾

| 维度 | 内容 |
|---|---|
| 名字 | `us-smallmid-dislocation` |
| 一句话 | 在一大批美股小中盘里做**初筛**，挑出「价格跌得比基本面严重」的候选票 |
| 输出档位 | `Pass` / `Watchlist` / `Investigate`（3 档，**不是**买入建议） |
| 与 Skill 1 的边界 | 本 Skill 只做候选生成；`Investigate` ≠ `Starter`。单票最终决策走 `market-sentiment-research` |

本 Skill 与 `market-sentiment-research` 共用 `sharing-resources/` 下的全部 Python 运行时。

---

## 二、现状盘点

### 2.1 已完成（不要动）

- `skills/us-smallmid-dislocation/scripts/screen_candidates.py`（414 行）——确定性 CSV 筛选脚本。完成宇宙过滤、跨族触发、红旗 ceiling、打分、`Pass/Watchlist/Investigate` 分档、JSON + Markdown 输出。
- `defaults/universe.toml` —— 宇宙配置（市值区间、流动性下限、排除结构/行业/特殊情形、state ceiling）。
- `references/`（input_schema / thresholds / red_flags / dislocation_workflow / script_reference）+ `docs/design_notes.md` + `SKILL.md`。

### 2.2 缺失（本轮要开发）

经与用户确认，本轮补两块：

1. **宇宙构建脚本**（用户选择「新增宇宙构建脚本」）——`screen_candidates.py` 目前只吃「别人准备好的 CSV」，本轮新增一个能**自动抓取并组装**这张 CSV 的脚本，复用 market_sentiment 已有的价格 / SEC 数据源客户端。
2. **并发 subagent 深度复核层**（用户选择「复用 market-sentiment 单票管线」）——筛选出的每只候选票，由运行时 subagent 调用 market_sentiment 的**单票管线**做深度证据复核。

---

## 三、目标架构（端到端数据流）

```
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 0：宇宙构建（新增，离线批处理）                              │
│   build_universe.py                                              │
│   种子 ticker 列表(SEC company_tickers_exchange.json)             │
│      ↓ 便宜过滤：交易所 / 价格≥$5  (Yahoo 价格)                    │
│      ↓ 市值 / ADV20 过滤                                          │
│      ↓ 存活者补基本面 (SEC companyfacts)                          │
│      ↓ 计算 drawdown_52w / 60d / relative_underperformance_60d    │
│   产出：universe.csv（符合 references/input_schema.md）            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 1：确定性筛选（已完成，不动）                                 │
│   screen_candidates.py universe.csv                              │
│   产出：smallmid_results.json（含 Watchlist / Investigate 候选）   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 2：并发 subagent 深度复核（新增）                            │
│   主 agent 读 smallmid_results.json，对每只非-Pass 候选            │
│      ↓ 并发派 1 个 Sonnet subagent / 票                           │
│   每个 subagent：                                                 │
│      market-sentiment review-ticker <T> → 单票 review packet      │
│      读 Layer 2 证据，独立判断                                     │
│      返回紧凑 JSON verdict                                        │
│   主 agent 汇总所有 verdict → 最终候选排名报告                     │
└─────────────────────────────────────────────────────────────────┘
```

设计原则（沿用 `design_notes.md`）：确定性筛选不依赖脆弱的实时抓取——所以阶段 0 是一个**独立的、可重跑、带缓存**的批处理步骤，产物是一张静态 CSV；阶段 1 只评估它拿到的行。

---

## 四、组件详细设计

### 4.1 组件 A：宇宙构建脚本 `build_universe.py`

**新文件**：`skills/us-smallmid-dislocation/scripts/build_universe.py`
**测试**：`sharing-resources/tests/test_build_universe.py`

#### 职责

把一个宽泛的美股票池，逐票补齐 `references/input_schema.md` 要求的列，输出一张可直接喂给 `screen_candidates.py` 的 CSV。

#### 数据来源（全部复用现有客户端，不写新数据源）

| 列 | 来源 | 复用的类 |
|---|---|---|
| `ticker` / `name` / `exchange` / `cik` | SEC `company_tickers_exchange.json` | 新增一个轻量 fetch（走 `HttpClient`） |
| `price` / `avg_dollar_volume_20d_usd` / `drawdown_52w` / `drawdown_60d` | Yahoo 日线（近 1 年） | `YahooFinanceClient` |
| `relative_underperformance_60d` | 候选股 60 日收益 − benchmark 60 日收益 | `YahooFinanceClient`（基准 IWM/IJH） |
| `market_cap_usd` | `price × 流通股数` | 流通股数取 SEC companyfacts；取不到则跳过该票 |
| `revenue_yoy` / `operating_cashflow_latest` / `normalized_fcf_latest` / `cash_latest` / `debt_latest` / `filing_age_days` | SEC companyfacts → `FundamentalSnapshot` | `SecClient.fetch_company_facts` |

- `normalized_fcf_latest = operating_cashflow_latest − capex_latest`
- `revenue_yoy = (revenue_latest − revenue_previous) / revenue_previous`
- `filing_age_days = run_date − FundamentalSnapshot.filed_on`
- 拿不到的「强烈推荐」「事件/tape」列（`runway_months`、`interest_coverage`、`share_count_growth_yoy`、`working_capital_release_ratio`、`earnings_*`、`sector`、`industry`、`structure`、`flags`）**留空**——`screen_candidates.py` 对缺列已是 None-safe。不要为了凑列伪造数据。

#### 分阶段过滤（控制 API 调用量）

SEC 全量约 1 万只票，**绝不能**对每只都打 companyfacts。顺序：

1. 先取种子列表，按 `universe.toml` 的 `allowed_exchanges` 过滤交易所。
2. 用 Yahoo 价格做便宜过滤：`price ≥ min_price_usd`、`ADV20 ≥ min_avg_dollar_volume_20d_usd`。
3. 对存活者算 `market_cap_usd`，过滤到 `[market_cap_min_usd, market_cap_max_usd]`。
4. **只对最终存活者**（目标量级约 2200–3200）调 SEC companyfacts 补基本面。

#### 关键工程要求

- **可重跑 / 可恢复**：每只票的抓取结果落一个本地缓存（建议 `data/state/universe_cache/` 下按 ticker 存 JSON，或一张 SQLite 表）。重跑时已抓过的直接复用，不重复打 API。
- `--limit N` 参数：只处理前 N 只，供开发期 smoke test。
- `--seed-file PATH` 参数：允许传入自备种子列表（每行一个 ticker），缺省走 SEC `company_tickers_exchange.json`。
- `--output PATH`：输出 CSV 路径，缺省 `data/universe/universe_<date>.csv`。
- `--as-of DATE`：计算 drawdown 的基准日，缺省今天。
- 单票任意异常**graceful 跳过**：记一条 warning，继续下一只，绝不整体崩。结束时打印「成功 N / 跳过 M / 跳过原因 top 5」。
- SEC 访问遵守 10 req/s 限速；Yahoo 顺序抓即可，无需并发。
- 纯 stdlib + 项目现有依赖，不引入新第三方库。

#### 验收

- `--limit 20` 能跑通并产出一张列齐全、`screen_candidates.py` 能直接消费的 CSV。
- `test_build_universe.py`：派生列计算（drawdown / relative / fcf / revenue_yoy / filing_age）单测；分阶段过滤单测；单票异常被 graceful 跳过的单测。全部用假数据，不打真网络。

### 4.2 组件 B：单票深度复核桥接

**改动文件**：`sharing-resources/src/market_sentiment/pipeline.py`、`sharing-resources/src/market_sentiment/cli.py`
**测试**：`sharing-resources/tests/test_review_single.py`

#### 职责

让 market_sentiment 的单票管线能对**任意一只 ad-hoc ticker**（不在 `watchlist.toml` 里）跑一遍证据采集，并产出 review packet。

#### 设计

`DailyPipeline.run()` 现在遍历 `self.config.securities`。新增一个方法：

```python
def review_single(self, ticker: str, *, benchmark: str, layer: Layer,
                   run_date: date, name: str | None = None) -> dict:
    """对单只 ad-hoc ticker 跑价格/SEC/宏观/社交 lane，构建并返回 review packet dict。"""
```

要点：

- 复用现有私有方法：`_fetch_prices_with_fallback`、`_fetch_events_with_recovery`、`_fetch_companyfacts_with_recovery`、`_fetch_macro`、`_fetch_benchmarks`（或针对单个 benchmark 抓价）、`self.social.collect`。
- 复用 `compute_trigger` / `classify_event_tag` / `build_scorecard` / `build_review_packet`。
- **触发与否不影响是否产出 packet**：本 Skill 的候选已经在阶段 1 触发过，深度复核要的是完整证据。`compute_trigger` 结果照常带进 scorecard，但即使 `triggered=False` 也要构建并返回 review packet。
- `layer` 参数：复用现有 `Layer` 之一即可（阶段 1 已做过触发判断，这里 layer 只用于取 `Threshold`）。若现有 4 个 layer 都不贴切，可在 `watchlist.toml` 的 `[triggers.*]` 加一个 `smallmid` 段并扩 `Layer` 枚举——由实现者评估，改动要最小。
- 异常处理沿用 pipeline 现有的 `_fetch_*_with_recovery` graceful 风格。

CLI 新增子命令：

```bash
market-sentiment review-ticker <TICKER> [--benchmark IWM] [--layer ...] [--date YYYY-MM-DD] [--name "..."]
```

- 调 `DailyPipeline.review_single(...)`，把返回的 review packet 以 JSON 打到 stdout（subagent 直接捕获），同时落盘到 `data/reports/<date>/review_packets/<TICKER>.json`。
- `--benchmark` 缺省 `IWM`（小盘）。

#### 验收

- `test_review_single.py`：用假数据源（沿用现有测试里 fake client 的模式，参考 `tests/test_pipeline.py`）验证 `review_single` 返回结构正确的 packet（含 Layer 1 / Layer 2 两层）；验证 `triggered=False` 时仍产出 packet。
- 不破坏现有测试——`run-daily` 行为零改动。

### 4.3 组件 C：运行时 subagent 编排（重写 `SKILL.md`）

**改动文件**：`skills/us-smallmid-dislocation/SKILL.md`
**新增文件**：`skills/us-smallmid-dislocation/references/subagent_workflow.md`

`PROJECT_REVIEW.md` 0.6 节确认本 Skill **强制开 subagent**（宽表扫多股，总证据量随票数线性增长，容易撑爆主 agent 上下文）。`SKILL.md` 要补一段强制指令。下面这些 0.6 节里「待定」的参数，本文**定稿**（见第五章）。

`SKILL.md` 要新增的「强制 subagent」段落须明确：

1. 阶段 1 跑完后，主 agent 读 `smallmid_results.json`，取所有 `state != "Pass"` 的候选。
2. **每只候选派 1 个 subagent**，用**并行 tool call**（一条消息里发多个 Task），分批并发（一批约 6–8 个，避免限速）。
3. 每个 subagent：跑 `market-sentiment review-ticker <T>` → 拿 review packet → 按 Skill 1 的「Layer 2 优先、独立打分」规则读证据 → 返回**紧凑 JSON verdict**（schema 见下）。
4. subagent **不做**最终组合排名，只返回单票 verdict；主 agent 负责汇总成最终报告。
5. subagent 用 **Sonnet**（见第五章理由）。

subagent 返回的 verdict schema（写进 `subagent_workflow.md`）：

```json
{
  "ticker": "ABCD",
  "screen_state": "Investigate",
  "refined_state": "Investigate",
  "conviction": "high | medium | low",
  "key_evidence": ["...", "..."],
  "kill_risks": ["...", "..."],
  "next_checks": ["...", "..."],
  "diverged_from_screen": false,
  "divergence_reason": null
}
```

`refined_state` 仍限 `Pass / Watchlist / Investigate`。若 subagent 的判断与阶段 1 的 `screen_state` 不一致，`diverged_from_screen=true` 且必须在 `divergence_reason` 写清是哪条 Layer 2 证据导致分歧。

### 4.4 组件 D：配置与文档收尾

**改动文件**：`defaults/universe.toml`、`references/script_reference.md`、`references/dislocation_workflow.md`、`README.md`

- `universe.toml`：补 build_universe 需要的配置（如种子来源 URL、缓存目录、benchmark 映射已有 `[benchmarks]` 段可复用）。
- `script_reference.md`：补 `build_universe.py` 用法。
- `dislocation_workflow.md`：在工作流开头补「阶段 0 宇宙构建」「阶段 2 subagent 深度复核」两段，串成完整三阶段流程。
- `README.md`：把 Skill 2 段落从「开发中」状态描述更新为当前进度（由 orchestrator 在全部 merge 后统一改，不派给 subagent）。

---

## 五、运行时 subagent 决策表（定稿 0.6 节待定项）

| 待定项 | 定稿 | 理由 |
|---|---|---|
| 开几个 subagent | **每只非-Pass 候选 1 个** | 阶段 1 已把宇宙收敛到候选量级（一般 10–30 只），逐票一个 subagent 最简单、隔离最干净，不必按 sector 分组 |
| 每个 subagent 读多少数据 | 一只票的 review packet（DeepSeek 已压过社交，单 packet 约 7–10KB） | 不会撑爆 subagent 上下文 |
| subagent 模型 | **Sonnet** | 深度复核要在 Layer 2 原始证据上做独立推理（基本面、披露语气、错位归因），需要理解力；用户也已指示开发用 Sonnet，运行时保持一致 |
| 输出格式 | **紧凑 JSON verdict**（schema 见 4.3） | 主 agent 机械汇总，避免叙事拼接的不确定性 |
| 是否并发 | **并发**，分批 6–8 个 | 多票天然适合并行；分批控限速 |

---

## 六、开发派工计划

orchestrator（我）负责拆任务、写 spec、验收，**不碰代码、不跑测试**。全部实现交 Sonnet subagent。

派工按**文件不重叠**切分（沿用 `PROJECT_REVIEW.md` 教训：同一文件多 subagent 并行必出冲突）。

| Subagent | 任务 | 触碰文件 | 可否并行 |
|---|---|---|---|
| **S1（impl）** | 组件 A 宇宙构建脚本 | `scripts/build_universe.py`（新）、`tests/test_build_universe.py`（新） | 与 S2/S3 并行 |
| **S2（impl）** | 组件 B 单票深度复核桥接 | `pipeline.py`、`cli.py`、`tests/test_review_single.py`（新）；如需扩 Layer 则碰 `models.py`/`config.py`/`watchlist.toml` | 与 S1/S3 并行 |
| **S3（docs）** | 组件 C + D：`SKILL.md` 重写、`subagent_workflow.md` 新建、`universe.toml` / 两个 reference 文档更新 | 全是 docs/config，与代码不重叠 | 与 S1/S2 并行 |
| **S4（review）** | 独立审计 S1+S2 的代码改动（正确性 / scope / 副作用 / 测试质量 / 边界 / pytest） | 只读，不改 | S1+S2 完成后 |

- S1/S2/S3 文件集互不重叠，可同时跑。
- S4 在 S1+S2 报告完成后启动；review 发现 BLOCKER 由 orchestrator 决定是否再派 fix subagent。
- 所有 subagent **只改不提交**——改动留在工作树，由 orchestrator 验收后再决定 commit/push（需用户确认）。

---

## 七、总体验收标准

1. `python3 skills/us-smallmid-dislocation/scripts/build_universe.py --limit 20` 跑通，产出列齐全的 CSV。
2. 该 CSV 能被 `screen_candidates.py` 直接消费，产出 `Pass/Watchlist/Investigate` 结果。
3. `market-sentiment review-ticker <某只候选>` 跑通并产出双层 review packet。
4. `python3 -m pytest sharing-resources/tests` 全绿（含新增的 `test_build_universe.py`、`test_review_single.py`），现有测试零回归。
5. `SKILL.md` 含明确、可执行的强制 subagent 编排段落。

---

## 八、风险与已知约束

- **API 量级**：宇宙构建对约 2200–3200 只票打 SEC companyfacts，即使遵守 10 req/s 也要数分钟。必须做缓存 + 可恢复，否则开发期反复重跑会很痛。
- **Alpha Vantage 限额**：免费 key 25 次/天。宇宙构建的价格主力走 Yahoo（无 key），AV 只作 fallback，不要在宇宙构建里主动大量调 AV。
- **市值依赖流通股数**：SEC companyfacts 的流通股字段偶有缺失；取不到就跳过该票并计入 skip 统计，不要用估算值硬凑。
- **subagent 不能联想**（`PROJECT_REVIEW.md` 反复强调的教训）：dev subagent 的 spec 必须精确到文件、函数、字段；review subagent 给的根因分析 orchestrator 要复核，不照单全收。
- 本文为定稿架构；若 subagent 实现中发现架构层面的硬阻塞，须回报 orchestrator，不得自行改设计。
