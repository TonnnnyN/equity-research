# 市场情绪感知项目 —— 深度评审报告

> 初版生成日期：2026-05-12
> 最新更新：2026-05-15（0.17 新增：Tiger Trade 价格 lane + Yahoo Finance 备用 + 5 bug 闭环，133/133 测试）
> 当前代码版本：`c387fa3` + 本地未 commit 改动

---

## 零、改造进度（最新）

### 0.1 已完成的 commit

| Commit | 标题 | 内容要点 | 测试 |
|---|---|---|---|
| `0b64e9c` | 初始版本 | 双 skill 分离后的基线 | 76/76 |
| `ea14626` | PROJECT_REVIEW.md | 本评审文档初版 | 76/76 |
| `4acbeec` | P0 step 1:缓存层 + 清理脚本 | 新增 `social_post_cache` / `filing_summary_cache` 两张 SQLite 表 + 索引 + CRUD + 独立 `purge_caches.py` 清理脚本 | 82/82 |
| `1b8195f` | P0 step 2:pipeline 接入缓存 + 子代理接口 | `social_service.py` 抓帖前先查缓存,只把新帖送给情绪判官;输出剥 body 全文;judge 异常 graceful 降级 | 88/88 |
| `7f2e02b` | P0 step 2.5:stub 不污染缓存 | 给 `SentimentJudgement` 加 `is_stub` 字段,stub 输出不写缓存,确保未来真 LLM 不被 `INSERT OR IGNORE` 锁死 | 90/90 |
| `9dd26b2` | P0 step 3:DeepSeek 情绪判官 | `DeepSeekSentimentJudge` 调用 deepseek-chat API,批量 20 帖/次,纯 stdlib `urllib`,失败全 graceful;按 `DEEPSEEK_API_KEY` 环境变量自动切换真/假判官 | 105/105 |
| (未提交) | P0 step 4:`http.py` SSL 修复 | `HttpClient` 改用 `certifi.where()` 作为 CA bundle,绕开 Python 3.12 framework 安装缺默认 cafile 的问题;Reddit/SEC/FRED 等所有 HTTPS 调用受益 | 105/105 |

### 0.2 P0 当前数据流(已生效)

```
触发检测(异常跌幅)
   ↓
社交源抓帖(Reddit / Discourse / X)
   ↓
按 (source, post_id) 查 social_post_cache(14 天窗口)
   ├── 已缓存 → 直接复用结构化判决,不再调 LLM
   └── 未缓存 → 攒批 20 帖
                  ↓
              DeepSeekSentimentJudge.judge_batch()
                  ├── 设了 DEEPSEEK_API_KEY → DeepSeek 真判
                  └── 没设 → StubSentimentJudge(neutral 占位,不写缓存)
                  ↓
              得到 SentimentJudgement(sentiment / confidence / one_line_summary / is_stub)
                  ↓
              is_stub=False 的写入 social_post_cache(INSERT OR IGNORE,情绪锁死)
   ↓
合并"缓存里的旧帖 + 本次新判的帖"
   ↓
**剥掉 body 全文**,只保留 title / sentiment / confidence / 摘要 / 时间 / 互动数
   ↓
作为结构化 social_snapshot 喂给主 agent(Claude / Codex)
   ↓
主 agent 看时间线("过去 5 天 bear 占比、新出现的 bear 帖")自行判断
   ↓
进入打分流程(scoring.py)
```

### 0.3 已经被解决的问题

参照本文第五章的问题清单,目前已闭环:
- ✅ **5.1 上下文撑爆**:body 全文不再进主 agent;判过的帖不再二次读
- ✅ **5.2 重复读取**:14 天滚动窗口内的帖子缓存复用;清理脚本 cron 跑
- ✅ **5.3 LLM 失败炸 pipeline**:DeepSeek 任意失败都返回 [],上层走 unknown 路径
- ✅ **2.5 stub 污染缓存**:`is_stub=True` 跳过 upsert,真 LLM 可无障碍写入

### 0.4 已新增的文件

```
sharing-resources/
├── scripts/
│   └── purge_caches.py            # 新增:独立清理脚本(cron 用,不进 SKILL.md)
├── src/market_sentiment/
│   └── subagent_sentiment.py      # 新增:PostToJudge / SentimentJudgement /
│                                  #   StubSentimentJudge / DeepSeekSentimentJudge /
│                                  #   build_default_sentiment_judge()
├── secrets/
│   └── market_sentiment.secrets.example.sh  # 改动:加 DEEPSEEK_API_KEY 行
└── tests/
    ├── test_cache_storage.py              # 新增:6 测试
    ├── test_social_cache_integration.py   # 新增:7 测试
    └── test_deepseek_judge.py             # 新增:15 测试
```

修改的现有文件:`models.py`(加 2 个 dataclass)、`storage.py`(加 2 表 + 索引 + 6 个 CRUD)、`social_service.py`(接缓存 + 判官)、`pipeline.py`(注入工厂)。**未动**:`sources/*`、`scoring.py`、`triggers.py`、`reporting.py`、`social_rebound.py`。

### 0.5 你现在要做的事(按优先级)

> 历史步骤 A(配置 DeepSeek)已在 secrets 中完成;社交抓帖链路已在 2026-05-12 端到端验证(见 0.7)。下面是下一阶段。

#### 步骤 A':补 X 接入(可选,但用户明确想要)
1. 注册一个 **X 小号**(强烈别用主号,twikit 自己的 `ToProtectYourAccount.md` 说明有封号风险)
2. 二选一登录路径:
   - 账密路径:secrets.sh 里填 `X_USERNAME` / `X_EMAIL` / `X_PASSWORD` / `X_EMAIL_PASSWORD`
   - cookie 路径(更稳):浏览器登小号 → 导出 `auth_token` + `ct0` → 存成 JSON 到 `X_COOKIES_PATH`(注意当前 secrets.sh 路径还是 `/Users/votee_tommy/...` 老地址,要改成 `/Users/dr/Desktop/市场情绪/data/state/x_cookies.json`)
3. 装库:`pip install twikit twscrape`
4. 国内必走代理(`HTTPS_PROXY` 环境变量注入 python 进程)
5. 写一个最小验证脚本,只走 `XClient.fetch_posts(...)`,把结果存到 `data/diagnostics/x_verify.json`
6. 失败可能性高;若 cookie/账密都跑不通,跳到步骤 A''

#### 步骤 A'':写 StockTwits provider(X 走不通时的最佳补位)
- 公开 stream 端点 `https://api.stocktwits.com/api/2/streams/symbol/<TICKER>.json` **无需 token**,直接返回带 bull/bear 标签的股票相关消息
- 新建 `sharing-resources/src/market_sentiment/sources/stocktwits.py`,实现 `SocialProvider` 接口
- 注册到 `social_registry.py`,加 `config.SocialConfig.stocktwits` 配置段
- 已规划过(本文 P2-2),现在可以提前到 P0/P1 优先级
- 实现成本:1-2 小时

#### 步骤 B:跑一次完整 daily,验证 DeepSeek 判官
1. `source sharing-resources/secrets/market_sentiment.secrets.sh`
2. `market-sentiment --config config/watchlist.toml preflight`
3. `market-sentiment --config config/watchlist.toml run-daily`
4. 看生成的 `data/reports/<date>/report.md`,确认社交段落的 sentiment 是真的有 bull/bear 区分(不再是清一色 neutral)

#### 步骤 B:观察一周,验证 DeepSeek 判得准不准
- 抽样 20 条被判 bear 的帖子人工对照,看判得对不对
- 如果不准,改 `DeepSeekSentimentJudge` 里的 prompt(`_build_prompt()` 函数,见 subagent_sentiment.py)
- 不准的几种典型可能:讽刺帖被判 bull、回踩帖被判 bear、内容空的帖被强行打标签

#### 步骤 C:跑 cron 跑清理脚本
建议添加 crontab 一行:
```
0 3 * * * cd /Users/votee_tommy/市场情绪 && python sharing-resources/scripts/purge_caches.py --social-days 14
```
每天凌晨 3 点清掉 14 天前的社交缓存。财报缓存默认不清(`--filing-days 0`),手动决定何时清。

#### 步骤 D:决定下一步走 P1 还是先继续 P0 的财报缓存
P0 还差一块没做:**SEC 财报缓存接入**。schema 已建好(`filing_summary_cache`),但 `sources/sec.py` 还没接入缓存逻辑,每次触发还是会重拉 10-Q/10-K/8-K。同样要做的事:
- 抓 filing 前先查 `get_filing_summaries_for_ticker(ticker)`
- 已有 `accession_number` 跳过详情拉取,直接复用 summary
- 用 DeepSeek 摘要 filing 内容(可选——or 直接用 SEC 已有结构化数据)
- 区分 10-Q/K(长期缓存)和 8-K(每次重拉 list,内容缓存)

要不要继续做财报缓存?这是 P0 闭环的最后一块。

### 0.6 架构决策:subagent 用在哪个 skill

经讨论确认两个 skill 的处理方式**不对称**:

| Skill | 是否在 SKILL.md 里强制主 agent 开 subagent | 理由 |
|---|---|---|
| `market-sentiment-research` | **不开**(直接读 DeepSeek 已经压缩好的结构化结果) | 单股深度,帖子量可控;DeepSeek 已经做完第一层压缩(每帖 ~150 字),50 帖大约 7-8KB,主 agent 直接读不撑爆,多一层 subagent 反而增加延迟和调试成本 |
| `us-smallmid-dislocation` | **强制开**(SKILL.md 待补充强制指令) | 宽表扫多股,总帖子量随股票数线性增长,容易撑爆;subagent 天然适合"每只票各吃各的帖子并行返回小结"的并发场景 |

#### TODO:更新 `skills/us-smallmid-dislocation/SKILL.md` 强制 subagent 段落

待定细节(等 DeepSeek 跑过一周有真实数据再决定):
- **开几个 subagent**:每只候选股一个?还是按 sector / size 分组?候选股一般 10-30 只
- **每个 subagent 读多少帖**:每只票上限多少帖给 subagent?(比如 50 帖封顶)
- **subagent 用什么模型**:Haiku(快、便宜)还是 Sonnet(理解更深)?
- **subagent 输出格式**:JSON 数组(机读)还是中文叙事段(主 agent 直接拼装)?
- **是否并发执行**:并发 N 个 subagent 同时跑,还是串行?(SKILL.md 里需要明确告诉主 agent 用并行 tool call)

实施时机:**等步骤 A-C 跑通、积累一周数据后再做**——届时能根据真实帖子量级和 DeepSeek 输出质量来定上面这些数字,免得拍脑袋定的参数后期返工。

---

### 0.7 P0 step 4 实测记录(2026-05-12)

#### 0.7.1 已测试什么

由 haiku subagent 跑端到端社交抓帖测试,目标:验证 Reddit / Discourse / X 三条链路在不走缓存的前提下能否真的把帖子拉下来落盘。测试 ticker:NVDA / GOOGL。

| Provider | 结果 | 关键发现 |
|---|---|---|
| Reddit | ❌ → ✅(修复后) | 命中**公开** `search.json`,不需要 OAuth,前面以为缺 `REDDIT_CLIENT_ID` 是误判;真实 bug 是 `http.py` SSL 失败 |
| Discourse | ⊝ | 配置里 `enabled=false`,不在本轮范围 |
| X (twikit) | ❌ | `twikit` 库未安装 + 缺 X 账号凭证,属于已知问题 |

#### 0.7.2 修了什么

**根因**:`/Library/Frameworks/Python.framework/Versions/3.12/` 安装包默认 `ssl.get_default_verify_paths().cafile` 是 `None`(需手动跑 `Install Certificates.command`),所以 `ssl.create_default_context()` 创建出来的 ctx 没有 CA bundle,所有 HTTPS 调用都炸 `CERTIFICATE_VERIFY_FAILED`。conda 3.10 自带 cert.pem 才碰巧能跑。

**补丁**(`sharing-resources/src/market_sentiment/http.py`,2 行改动,未提交):
```python
import certifi   # 新增
...
self._ssl_context = ssl.create_default_context(cafile=certifi.where())  # 原为不带参数
```

**验证**:
- pytest:105/105 通过
- Python 3.12 实跑 `RedditClient.fetch_posts("GOOGL", "Alphabet Inc.", today)`:**返回 58 条真实帖**,success=True,partial=False
- 样本帖落盘到 `/Users/dr/Desktop/市场情绪/data/diagnostics/reddit_verify.json`(~140KB)

#### 0.7.3 顺手发现的"假问题"

诊断 subagent 报告里有几条要打折扣,不要照单全收:
- "缺 REDDIT_CLIENT_ID/SECRET":❌ 误判。代码用公开 search 端点,不需要 OAuth
- "secrets.sh 路径全错":部分误判。出错的只有 `X_DB_PATH` / `X_COOKIES_PATH` 两个**操作型路径**(指向数据文件),其它 export 行正常;但这两个路径确实要在配 X 时改回 `/Users/dr/Desktop/...`
- "DEEPSEEK_API_KEY missing":subagent 自己的 shell 没 source secrets.sh,不代表配置里没有

#### 0.7.4 下一步选项(给用户决策)

**优先级排序(orchestrator 推荐)**:

1. **写 StockTwits provider**(1-2 小时,纯增量)——公开 stream 端点 `api.stocktwits.com/api/2/streams/symbol/<TICKER>.json` 无需 token,返回带 bull/bear 标签的股票消息,可大幅减轻 DeepSeek 工作量
2. **接入 X(twikit)**——用户明确想要,但需要先去注册 X 小号 + 导 cookie + 配代理;封号风险真实存在
3. **跑一次完整 `run-daily`**——把 Reddit + DeepSeek 链路实跑一遍,看 report.md 社交段落是不是真的有 bull/bear 区分

> 顺序逻辑:1 让社交源更丰富、3 验证已有链路、2 是补强。可以 1 和 3 并行做,2 等用户拿到 X 小号 cookie 再说。

#### 0.7.5 派工流程留档(下次复用)

- 诊断 subagent:`general-purpose` + `model: haiku`,只读、产 JSON + MD 报告,严禁改代码
- 由 orchestrator(我)对照源码复核报告 → 区分真假问题 → 圈定**唯一**真 bug
- 修复 subagent:同样 haiku,但**给出精确 diff 描述**(几行加在哪、改成什么),不让它自己发挥
- 验证:跑 pytest + 写一个独立 verify 脚本调真接口,产物落盘到 `data/diagnostics/`

教训:haiku subagent 给的根因分析要审,**不能直接复制**。它倾向于把所有"看着不对"的事情都列成根因,实际可能只是它的执行环境问题(比如它用了 Python 3.12 framework 而项目实际是用 conda 3.10 跑)。

---

### 0.16 大规模集中收尾(2026-05-15)—— P0/P1/P2/P3 整轮处理完毕

orchestrator 自主分派 7 个 haiku subagent(3 impl + 1 review + 1 cleanup + 2 Tier2/3 impl),处理 PROJECT_REVIEW 第五-六章里除 Tiger 替换之外的全部开放项。最终 **118/118 单测**,4 个 commit 推上线。

#### 0.16.1 各 subagent 任务和结果

| # | Subagent | 任务 | 文件 | 结果 |
|---|---|---|---|---|
| G | impl | P0-1 + P0-2:社交帖 body 600 截断、official_events 8→5 | `social_service.py` + `review_packets.py` | ✅ 109/109 |
| H | impl | P1-1 + P3-3:preflight API key 状态表 + 社交全挂 WARN | `runtime_preflight.py` | ✅ 111/111 |
| I | impl | P1-3 + P0-3:基本面/披露保底分 8→4 / 5→3 + 核心源全挂强制 WATCH | `scoring.py` + `pipeline.py` + `models.py` + `review_packets.py` | ✅ 114/114 |
| K | review | 审 G+H+I 合并 diff | (read-only) | PASS,2 个 NOTE |
| L | cleanup | 删 `_EVENT_BODY_CHAR_CAP` 死代码 + `data_quality` 抬到 packet 顶层 | `review_packets.py` | ✅ 114/114 |
| M | impl | 0.15.4 social lookback 调参 + P3-2 全源失败集成测试 | `config/watchlist.toml` + `config.py` + `test_pipeline.py` | ✅ 115/115 |
| N | impl | P2-1 HTTP 指数退避 retry + P3-1 partial_coverage 下负分对称减半 | `http.py` + `scoring.py` | ✅ 118/118 |

#### 0.16.2 具体改动一览

**5.1 上下文撑爆 / P0-1+P0-2**:
- `social_service._cap_posts`:每条 surviving post 的 `body` 超 600 字符截断 + `"...[truncated]"` 标记
- `review_packets`:`official_events[:8]` → `[:5]`,`_EVENT_BODY_CHAR_CAP` 之前是 placeholder(`OfficialEvent` 实际无 `body` 字段),已删
- ⚠️ `social_rebound.py:103` 提到的关键词匹配在原 PROJECT_REVIEW 5.1A 里也点名了,本轮**未动**——影响小,等真出问题再处理

**5.3 静默失败 / P1-1+P3-3**:
- preflight 新增 API key 状态表(`ALPHAVANTAGE_API_KEY` / `FRED_API_KEY` / `SEC_USER_AGENT` / `DEEPSEEK_API_KEY` blocking;`EIA_API_KEY` + `REDDIT_*` WARN)
- 当 `social.enabled=True` 但 reddit/x/forum 都关闭/空配时,WARN 一行

**5.4 评分鲁棒性 / P1-3+P0-3+P3-1**:
- `scoring.py:155` fundamentals 缺数据保底分 `8/30` → `4/30`
- `scoring.py:162` sentiment 缺数据保底分 `5/15` → `3/15`
- 新增 `cap_state_if_data_insufficient(state, source_health)`:`sec.success=False` 或 `price.success=False`(含 `daily_prices_cache partial=True`)时,**ADD/STARTER 强降 WATCH**,并把 `ScoreCard.data_insufficient=True`
- `review_packet` 顶层新增 `data_quality: "ok" | "insufficient"`,主 agent 一眼能看到
- `score_social_rebound`:`partial_coverage=True` 之前只清零正分、留满负分(原 5.4 问题 I 报告的非对称漏洞),现在**负分对半减(向 0 取整)**

**5.2 数据稳定 / P2-1**:
- `http.HttpClient.get`:3 次 attempt,backoff (1s, 2s),retry 条件 `URLError + HTTP 5xx + 429`(4xx 不重试,直接抛)
- backoff 常数 module-level,测试可 monkeypatch 为 0 以提速

**社交 lookback / 0.15.4**:
- `lookback_hours: 72` → `168`(3 天 → 7 天)
- `min_recent_posts: 10` → `6`
- 起因:CRM E2E 显示 16 条帖只有 2 条落在 72h,新阈值让稀疏但真实数据能算出 social_rebound

**集成测试 / P3-2**:
- `test_pipeline_graceful_degradation_when_all_sources_fail`:全源 success=False,断言 pipeline 不崩、`data_insufficient=True`、`state ∈ {WATCH, REJECT}`

#### 0.16.3 本轮 commit 列表(都已 push origin main)

```
c387fa3  HTTP retry middleware and symmetric partial-coverage social cap
087a10c  Widen social lookback + assert graceful all-source-fail behavior
a36c2e1  Tier 1 robustness: truncation, preflight visibility, data gating
```

#### 0.16.4 测试基线

```
状态:2026-05-15
总测试数:118
通过率:100%
新增本轮:11 个(G:0 + H:2 + I:3 + L:0 + M:1 + N:3 + 2 个被更新的现有测试)
```

#### 0.16.5 派工流程总结

**有效的模式**:
1. orchestrator 把 spec 写到字段级 / 行号级,常数和命名都点名;haiku impl 几乎能一次过
2. impl 后跟 review subagent,**review 不止一次抓到 impl 漏的 BLOCKER**(本批是 cache 兜底那一轮的 `statuses.append`)
3. 复合任务按文件分组,避免 subagent 并行编辑冲突——本轮全部串行
4. orchestrator 自己 `grep` 一遍 diff,把"超出 spec 的副效应"挑出来(例如 G 留下的 `_EVENT_BODY_CHAR_CAP` 死代码),再派 cleanup subagent

**无效或要小心的模式**:
- 让 haiku 自己决定"用多少阈值 / 怎么处理边界"会跑偏,必须 orchestrator 先定数值
- haiku 写测试时可能加冗余 assert 或 fixture,review 时要查"是不是真测了 spec 要的行为"
- 同一文件多个 subagent 并行 = 必出 merge 冲突,严格串行

#### 0.16.6 仍未处理的(优先级低或被替换计划覆盖)

| 项 | 现状 | 原因 |
|---|---|---|
| P2-2 StockTwits provider | 未做 | 价格 lane 走 Tiger,社交 lane 现有 Reddit 已够稳;StockTwits 等真有需要再开 |
| P2-3 X provider early-abort | 未做 | X 整体默认 disabled,且短期没用户压力;Tiger 接好之后再决策 X 是不是真要 |
| SEC/FRED/EIA 完整本地缓存 | 部分(SEC filing 已写但还没读取兜底) | 这几个源稳定性高,quota 也宽松;真出 quota 问题再做 |
| 原 P0 step 9 给 AV 加 SQLite 日级缓存 | **作废** | Tiger Trader 计划已替换 AV;改名为 step 9'(实现 Tiger source),等用户拿到凭证启动 |

#### 0.16.7 下次进场可以做的事

按时间顺序排:
1. **(用户)在 Tiger Trader 申请开发者凭证** → 触发 step 9'(haiku impl + review,工作量 3-4 小时)
2. **观察一周**:用现在的 watchlist 跑日常 run-daily,看 `social_rebound != 0` 在多少票上稳定出现,验证 0.15.4 调参是否合适
3. **填 `filing_summary_cache.sentiment` 字段**:目前是 placeholder `"unknown"`,可以加 DeepSeek 二次调用做 filing 摘要(P0 step 8 当时留下的 Phase 2)
4. **SEC/FRED/EIA 本地日级缓存**:若 quota 问题真冒出来再做,模板可复用 P0 step 8 的写法

---

### 0.17 Tiger Trade 价格 lane 接入 + Yahoo Finance 备用源(2026-05-15)

#### 0.17.1 本轮工作概述

用户拿到 Tiger 开发者凭证,触发了 0.16.6 表里挂着的 **P0 step 9'**。orchestrator(我)派工 4 轮 subagent(2 Haiku impl + 1 Sonnet impl + 多次 Haiku 实测复核),最终实现:
- 价格 fallback 链:**Tiger → Yahoo Chart → Alpha Vantage → Stooq → SQLite 缓存**
- 5 个 bug 全部闭环
- 33 watchlist ticker(含 4 个港股)100% 覆盖,Tiger 拿美股、Yahoo 拿港股
- pytest **118 → 133**(原有 + 11 Tiger 单测 + 5 Yahoo 单测,以及随重构调整的若干现有 case)

#### 0.17.2 新增 / 改动的代码

**新增**:
- `sharing-resources/src/market_sentiment/sources/tiger.py`(~250 行):`TigerClient.fetch_daily_prices`,SDK 用 `TigerOpenClientConfig(props_path=...)` 读 `tiger_api/` 目录下的 `tiger_openapi_config.properties` + `tiger_openapi_token.properties`,内部用 `QuoteClient(config, is_grab_permission=False)` 跳过实时行情权限抓取(关键修复,见 0.17.3)
- `sharing-resources/src/market_sentiment/sources/yahoo_finance.py`(~110 行):`YahooFinanceClient.fetch_daily_prices`,免 key,走 `query1.finance.yahoo.com/v8/finance/chart/{ticker}`,原生支持美股 + 港股 `.HK`(无需 symbol 转换)
- `sharing-resources/tests/test_tiger.py`(11 case)+ `tests/test_yahoo_finance.py`(5 case)

**改动**:
- `pipeline.py:192-244` 把 `_fetch_prices_with_fallback` 改成 5 级链(Tiger → Yahoo → AV → Stooq → cache)
- `runtime_preflight.py`:加 `TIGER_CONFIG_PATH` blocking 检查,支持目录或文件路径
- `secrets/market_sentiment.secrets.example.sh`:删 4 个旧 env(`TIGER_ID` / `TIGER_PRIVATE_KEY_PATH` / `TIGER_ACCOUNT` / `TIGER_SERVER`),改成单一 `TIGER_CONFIG_PATH="sharing-resources/secrets/tiger_api/"`
- `.gitignore`:点名 `sharing-resources/secrets/tiger_api/`(catch-all 本来就覆盖,显式列出图清晰)
- `pyproject.toml`:加 `tigeropen>=3.0.0` 依赖
- `tests/test_pipeline.py` + `test_sources.py` + `test_runtime_fixes.py`:同步修订 fallback 链长度断言、preflight 字段集

**凭证布局(用户负责)**:
```
sharing-resources/secrets/tiger_api/
├── tiger_openapi_config.properties   # tiger_id / account / license / env / private_key
└── tiger_openapi_token.properties    # user_token(从 Tiger 开发者后台生成)
```

#### 0.17.3 5 个 bug 闭环过程(派工流水线复盘)

| # | 阶段 | bug | 真因 | 修法 |
|---|---|---|---|---|
| 1 | impl-A(haiku) | `tiger.py` import 路径错 | 跟着 orchestrator 凭研究笔记伪造的文档抄入 `from tigeropen.common.consts import TigerOpenClientConfig` | 改为 `from tigeropen.tiger_open_config import TigerOpenClientConfig`(orchestrator `python -c` 验过) |
| 2 | impl-A(haiku) | `TigerOpenClientConfig(props_file_path=...)` 参数名错 | 同上来源 | 改 `props_path=...`(`inspect.signature` 验过) |
| 3 | impl-A(haiku) | tigeropen SDK 调 `openapi.tigerfintech.com` 抛 `CERTIFICATE_VERIFY_FAILED` | Mac 上 Python 3.12 framework 安装包没默认 cafile;SDK 用 requests/urllib3 自己的 CA 路径,**不走** http.py 的 certifi 修复 | tiger.py 模块顶层 `os.environ.setdefault("SSL_CERT_FILE", certifi.where())` + `REQUESTS_CA_BUNDLE` 同样,在 SDK lazy import 之前注入 |
| 4 | impl-Sonnet | `QuoteClient(config)` 启动时调 `grab_quote_permission()` 抛 `code=2400 user token cannot be empty` | SDK 默认 `is_grab_permission=True`,要求账户有实时行情订阅;但拉历史日线(`get_bars`)其实不需要这个权限 | `QuoteClient(config, is_grab_permission=False)` 跳过启动权限抓取 |
| 5 | 配置层(用户做) | 所有 `get_bars` 调用仍报 `user_token cannot be empty` | SDK 内部所有请求都会附带 `token` 字段,token 来自 `tiger_openapi_token.properties` 或环境变量;orchestrator 通过 `inspect.getsource` 查清 SDK 加载 token 的 3 个来源,告诉用户去 Tiger 开发者后台生成 user_token | 用户在 `tiger_api/` 目录加了 `tiger_openapi_token.properties`,SDK 自动读 |

每个 bug 都经过 orchestrator 用 `python -c` / `inspect.getsource` / 真凭证 smoke test **独立复核**,没有照单全收 subagent 的诊断。bug 3 那个 SSL 问题甚至 0.7.2 在 Reddit/SEC 上修过一次,这是同种问题在 tigeropen 上的重演——subagent 没有联想能力,需要 orchestrator 把"Mac 3.12 framework 安装包默认无 cafile"这条共性认知带进 spec 里。

#### 0.17.4 实测结果(8 ticker)

最终 verification(2026-05-15,用真凭证):

| 类 | Ticker | Tiger | Yahoo | pipeline 最终来源 |
|---|---|---|---|---|
| 美股 | MSFT | ✅ 103 bar @ 409.43 | ✅ 123 bar | tiger(103 bar) |
| 美股 | CRM | ✅ 103 bar @ 167.58 | ✅ 123 bar | tiger |
| 美股 | NVDA | ✅ | ✅ | tiger |
| 美股 | AAPL | ✅ | ✅ | tiger |
| 港股 | 9660.HK | ❌ empty | ✅ 120 bar @ 6.26 | yahoo_chart(120 bar) |
| 港股 | 3033.HK | ❌ empty | ✅ 120 bar @ 4.83 | yahoo_chart |
| 港股 | 9880.HK | ❌ empty | ✅ | yahoo_chart |
| 港股 | 2252.HK | ❌ empty | ✅ | yahoo_chart |

**港股 Tiger 失败原因**:用户的 Tiger 账户没买港股 Level 1 实时行情订阅(license=TBHK 但订阅没开)。Tiger 服务端不抛错,**返回空 DataFrame**,导致 success=False → fallback 链自动落到 Yahoo。**这是设计上预期的兜底行为,不是 bug**。

#### 0.17.5 价格数据本地持久化(回应用户提问)

历史 K 线**自动持久化到 SQLite**:
- 表:`daily_prices(ticker, trading_date, open, high, low, close, volume, source, source_url, ingested_at)`,主键 `(ticker, trading_date)`
- 写入点:`pipeline.py:79` 每次成功 fetch 后 `storage.upsert_prices(security_prices)`(INSERT OR REPLACE)
- 保留期:**365 天**(`watchlist.toml: daily_price_days = 365`)
- 退出兜底:`pipeline._fetch_prices_with_fallback` 在 Tiger+Yahoo+AV+Stooq 全挂时,从本地缓存读最近 60 天的 bar(`storage.read_cached_prices(ticker, days_back=60)`),`partial=True` 标记
- 数据多源混存:同一 ticker 同一天会被新来源 REPLACE 旧来源(Tiger 在前所以最新一次写入通常是 Tiger / Yahoo 数据,`source` 字段记录哪条 lane 写的)

**之前提到的"30 天"是 Tiger 服务端 quota 复用窗口**(同一 symbol 30 天内不重复扣 quota),那是 Tiger 内部机制,**与本地存储无关**——本地是 365 天。

#### 0.17.6 调研结论:支撑价 / 平均价不接(2026-05-15 用户决策)

orchestrator 用真凭证探了 Tiger SDK 所有相关接口:

| 想要的 | Tiger 接口 | 结论 |
|---|---|---|
| VWAP / 日均价 | `get_bars` 已返 `amount`,`VWAP = amount / volume` 直接算 | 不必新接接口 |
| 盘中分时均价 | `get_timeline` permission denied(需实时订阅) | — |
| 实时报价(briefs) | `get_stock_briefs` permission denied | — |
| 延迟 15 分钟报价 | `get_stock_delay_briefs` 可用,字段 `symbol/pre_close/halted/time/open/high/low/close/volume`(无 VWAP) | 用户决定不接 |
| 支撑/压力价 | Tiger **没有这个 API**,是技术分析衍生量(MA / Pivot / 布林),都能从已有 K 线本地推 | 用户决定不加 |

最终决策:都不加。当前价格 lane 已经能满足 trigger + scoring 需求。

#### 0.17.7 派工模式总结(本轮 vs 0.16 大批量)

本轮共派 9 个 subagent:
- Haiku impl × 2(初版 tiger.py + 重构 properties 路径 + Yahoo Finance)
- Sonnet impl × 2(`is_grab_permission` 一行修 + tiger_api/ 目录迁移)
- Haiku verify × 4(每次代码改动后跑 pytest + 真凭证拉数据)
- 中间 orchestrator 自己 `python -c` 验过 3 次,直接 inspect SDK 源码

**关键教训**:
1. SDK 类的接入,**orchestrator 在写 spec 时不能凭研究笔记假设 API 名字**——必须先 `python -c` 验过真实 import 路径 + `inspect.signature` 看签名,再写 spec
2. **Haiku 在做"诊断"时容易过度发挥**——0.17.3 表 # 1+#2 那两条 bug 测试 subagent 居然能识别出来(虽然测试时并没真跑到那行),但 impl subagent 跟着 orchestrator 错指令照抄进去
3. **Sonnet impl 适合做"一行精确改动"**(`is_grab_permission=False`)和"路径迁移"类任务,不需要发挥
4. **真凭证 smoke test 不可省**——所有"测试用 mock 全过"的 case,真打服务端都暴露了新的 bug 层(SSL → 启动权限 → token)

#### 0.17.8 仍未处理的(优先级低)

| 项 | 现状 | 备注 |
|---|---|---|
| 港股 Tiger 行情订阅 | 用户账户 license=TBHK 但 Level 1 没买 | 不阻塞——Yahoo 已 100% 兜底港股;真要切官方源时去 Tiger 后台买订阅即可,代码一行不改 |
| Tiger 专用 SQLite 缓存(原 step 9 衍生想法) | 不做 | `daily_prices` 通用表已经服务所有源,Tiger SDK 自身又有 30 天 quota 复用,加专用缓存收益接近 0 |
| `tigeropen` 写入 pyproject 的版本下限 | `>=3.0.0`,但实测装的是 3.5.8 | 这条不动;实际行为没问题,等真需要锁版本再调 |

---

### 0.18 回测系统(2026-05-16)

#### 0.18.1 本轮工作概述

用户要求做一套**完整的回测系统**:用真实历史数据,按规则引擎产出的状态(Reject/Watch/Starter/Add)模拟仓位买卖,含手续费。范围**只做美股**(港股暂不管),回测窗口约 1 年。orchestrator 派 Haiku subagent 实现 data_loader / simulator / signal-exit,其余(as-of 基本面、status_engine、runner)由 orchestrator 自写。

#### 0.18.2 新增文件(包 `sharing-resources/src/market_sentiment/backtest/`)

- `__init__.py` — 包说明。
- `data_loader.py` — 抓 2 年历史数据,写自包含 JSON 数据集。价格走 Yahoo Chart `range=2y`,**做了拆股/分红全口径复权**(`factor=adjclose/close`,OHLC 同步缩放,volume 反向),否则 NVDA 2024 年 10:1 拆股会看成 -90% 暴跌。SEC events + 原始 companyfacts 各抓一次。
- `asof_fundamentals.py` — point-in-time 基本面。`build_fundamentals_timeline` 按 filing 的 `filed` 日期切片,每个 filing 日生成一份快照;`snapshot_asof(timeline, D)` 取 `filed<=D` 的最新一份。避免回测时基本面"看到未来"。
- `status_engine.py` — 对每个决策日 × 每只美股,构造 as-of `PipelineContext`(prices/events/fundamentals 全部按日切片,social/options 略),复用生产 `compute_trigger` + `build_scorecard`,产出 status 记录。
- `simulator.py` — 日级组合模拟。信号在收盘产生,**次日开盘成交**;Starter/Add 建仓($10k/笔,每标的最多 3 笔);手续费**每笔买/卖各 $2 固定**。退出规则四条,按优先级:① 止损 -12% ② 止盈 +25% ③ **信号退出 signal_exit**(持仓标的当天 status 变 Reject 或带 veto_reason → 次日开盘平仓)④ 最长持有 60 交易日。signal_exit 可经 `BacktestConfig.signal_exit_enabled` / CLI `--no-signal-exit` 关闭。
- `runner.py` — 编排 dataset → statuses → simulation → report.md。

新增脚本:`scripts/build_backtest_dataset.py`(建数据集 CLI)、`scripts/run_backtest.py`(跑回测 CLI)。
新增测试:`sharing-resources/tests/test_backtest.py`,13 个单测。**全套测试 196 → 209,全绿**。

#### 0.18.3 首跑结果(2025-05-15 ~ 2026-05-15,默认参数)

期末权益 $105,902 / 总收益 +5.90% / 最大回撤 13.71% / 41 笔平仓 / 胜率 46.3% / 手续费 $226。平仓原因:止损 17、最长持有 11、止盈 8、回测结束 5。同期 QQQ +37%、SOXX +140% —— 策略是"逢跌轻仓低吸",2025-26 大牛市里跑输买入持有属预期,**不是 bug**。

**signal_exit 实跑触发 0 次**:监测的都是 NVDA/META/LLY 这类大盘股,1079 条 status 里 Reject = 0、veto = 0,规则引擎从不否决这些优质大盘股的中途回调,所以信号退出虽已实现并通过单测,本轮数据未实际触发。

#### 0.18.4 设计取舍

- **social lane 不进回测**:历史社交情绪无法 point-in-time 重建,`social_snapshot=None` → social bucket 0/0,scoring 的 `achievable_max` 自动缩放(110→100)。
- **fundamentals / sentiment 进回测**:这两桶来自 SEC(companyfacts + filing events),有真实历史时间戳,可按日还原。注意 `sentiment` 桶打的是 SEC 官方披露,不是社交情绪。
- 数据集自包含在 `data/backtest/dataset/`(约 200MB,已被 `.gitignore` 的 `data/` 覆盖,不入库)。
- 退出规则由 orchestrator 设计(规则引擎本身只产买入信号);止盈/止损/持有天数/signal_exit 全部可经 CLI 参数调。

---

### 0.19 分析师目标价 / 评级动量数据源接入(2026-05-17)

#### 0.19.1 本轮工作概述

本轮新增了**分析师目标价 + 评级升降级动量**数据 lane，作为 Layer 2 仅供参考的附加证据接入 market-sentiment-research pipeline。数据内容：机构共识目标价（level 信号——隐含相对当前价格的上行空间）+ 近期升降级动量（trend 信号）。

**关键设计原则**：该 lane **不进入** 6 维度打分体系，不创建 veto，不标记 `partial_coverage`，仅序列化进 review packet 供 Agent 参考推理。原因：分析师目标价属于滞后信号且存在跟风效应，优先级低于 SEC 披露和一手财务数据（source priority 第 4 位），强行纳入评分会干扰基于一手证据的确定性逻辑。

#### 0.19.2 新增文件

- `sharing-resources/src/market_sentiment/sources/analyst_targets.py`：新数据源模块，类 `AnalystTargetsClient`，核心方法 `fetch_analyst_snapshot(ticker, run_date)`。
  - **主数据源**：Yahoo `quoteSummary` 端点（`query2.finance.yahoo.com/v10/finance/quoteSummary`，modules `financialData,recommendationTrend,upgradeDowngradeHistory`），无需 API key，与 earnings-calendar lane 复用同一已验证端点。
  - **备用数据源**：Finnhub API（`/stock/price-target`、`/stock/recommendation`、`/stock/upgrade-downgrade`），仅在 Yahoo 失败/空且环境变量 `FINNHUB_API_KEY` 已设置时启用；`FINNHUB_API_KEY` 为**可选**，不设置时 US 股票依赖 Yahoo 即可正常运行。
  - 数据模型：`AnalystSnapshot`（目标价汇总）+ `AnalystRatingChange`（单条升降级记录）。
- `sharing-resources/tests/test_analyst_targets.py`：16 个新增单测，覆盖 Yahoo 主路径、Finnhub fallback、两者均失败的降级处理。**全套测试 263 → 279，全绿**。

#### 0.19.3 改动的文件

- `sharing-resources/src/market_sentiment/pipeline.py`：将 analyst_targets lane 以非阻塞 enrichment 方式接入（仅对触发票拉取，包在 try/except 内，与 earnings-calendar lane 模式一致）。`SourceStatus.source` 字符串为 `"analyst_targets"`。
- `sharing-resources/src/market_sentiment/runtime_preflight.py`：`FINNHUB_API_KEY` 未设置时发出 `[WARN]`（非 `[FAIL]`），属信息性提示，不阻断 pipeline 启动。
- review packet 新增顶层字段 `analyst_summary`（与 `macro_summary` 并列放在 Layer 2 证据区），字段含：`source, target_mean, target_high, target_low, target_median, current_price, implied_upside, number_of_analysts, recommendation_key, recommendation_mean, trend, recent_changes`；`recent_changes` 每条含 `firm, date, action, from_grade, to_grade`。

#### 0.19.4 设计取舍

- **不进打分**：分析师共识价属于 source priority 4，低于 SEC 披露（1）和财报数字（2）；目标价常滞后并有跟风效应，若纳入 6 维度打分反而会掩盖一手证据的信号。仅作为 Layer 2 参考注记，由 Agent 在最终叙事层面自行权衡。
- **Yahoo 优先，Finnhub 可选**：Yahoo quoteSummary 对美股覆盖率高、无需 key，与现有 earnings-calendar 端点同源，维护成本低。Finnhub 仅作 fallback，避免强依赖付费 key。
- **非美股（如 .HK）两者可能皆空**：已在文档中说明 web-search fallback 流程（Agent 自行搜索 TipRanks / MarketBeat 等聚合器补充，并标注为 web-sourced 低置信度数据）。
- **非阻塞设计**：lane 失败仅记录在 `source_health`，不影响主流程的打分和报告生成。

---

### 0.8 真实状态盘点 + 下一步路线(2026-05-14)

#### 0.8.1 状态澄清:"代码到位" ≠ "端到端跑通"

之前几次进度叙述把"代码写好了"当成"完成了",这里**重新校对一遍**。截至 2026-05-14:

| 数据 lane | 代码 | 端到端实跑过 | 备注 |
|---|---|---|---|
| Reddit 社交 | ✅ | ✅ 2026-05-12 | 58 帖落盘,见 0.7 |
| Discourse 社交 | ✅ | ⊝ 配置禁用 | 没配 base_urls |
| X 社交 | ✅ | ❌ 已知失败 | 需 X 小号 + cookie + 代理,**暂时跳过** |
| SEC 财报数字 (`company_facts`) | ✅ | ❓ **未实测** | 单测 mock 通过,真 API 没拉过 |
| SEC 官方披露 (`submissions`) | ✅ | ❓ **未实测** | 同上 |
| Alpha Vantage 日线 | ✅ | ❓ **未实测** | 25 次/天配额风险已知 |
| Stooq 备用价格 | ✅ | ❓ **未实测** | 无 retry |
| FRED 宏观(DGS10/DFF) | ✅ | ❓ **未实测** | |
| EIA 能源 | ✅ | ❓ **未实测** | 默认 optional |
| Alpha Vantage 期权 | ✅ | ⊝ 默认 disabled | 不在本轮范围 |
| DeepSeek 情绪判官 | ✅ | ❓ **未实跑** | 密钥已配,但没实际 batch 过 |

**结论**:我们其实只验证了 1/11 条 lane。之前对话里说"已完成获取财报数字 / 社交情绪 / 官方披露"是不准确的——只有社交里的 Reddit 一条算真完成。

#### 0.8.2 暂时跳过的小问题(用户确认 2026-05-14)

- DeepSeek API 二次调试(prompt 校准、判得准不准)→ 跳过,先跑通 pipeline 再说
- X 社交链路(twikit + 小号 + cookie)→ 跳过
- StockTwits 新 provider → 跳过
- 老路径 `/Users/votee_tommy/...` 修正 → 跳过(只影响 X,X 也暂不用)

> 这些都是局部 polish,不阻挡"主链路是否能端到端跑通"这个更大的验证目标。

#### 0.8.3 下一步:整跑 vs 逐 lane

两种思路:

**思路 A:逐 lane 写 verify 脚本(像 Reddit 那样一个一个测)**
- 优点:精细
- 缺点:每条 lane 都要写 30 行验证脚本 × 8 条 lane = 工作量翻倍;并且 pipeline 内部还有数据合并、序列化、打分等链路,逐 lane 测不到

**思路 B:直接跑一次 `run-daily`,看 `source_health` 谁挂(推荐)** ⭐
- 单条命令,~10 分钟拿全景图
- pipeline 已经把每个 source 的 success / partial / message 字段写进 review_packet
- 失败的 lane 才需要后续单独深挖,全成功的 lane 不需要再单测
- 顺带验证打分、报告生成、邮件投递等下游链路

**判断**:打分决策链(`scoring.py`)是 data 的纯函数,105/105 单测已经覆盖;它不会因外部依赖挂掉,只会因为输入空数据给保底分。所以**风险点全在数据 lane**,不在打分。

#### 0.8.4 执行步骤(下次会话开始就跑)

```bash
# 1. 加载密钥
source sharing-resources/secrets/market_sentiment.secrets.sh

# 2. 预飞,验证配置层
market-sentiment --config config/watchlist.toml preflight

# 3. 跑一次完整 daily
market-sentiment --config config/watchlist.toml run-daily

# 4. 看产出
cat data/reports/<date>/report.md           # 总报告
cat data/review_packets/<ticker>.json | jq .source_health  # 每个源的状态
```

**判读规则**:
- `source_health[*].success == true` 的 lane → 不用管
- `success == false` 或 `partial == true` 的 lane → 看 `message` 字段,排队修
- 全部 lane 都 success 但 `scorecard` 看着不合理 → 才去看打分逻辑

#### 0.8.5 预期会发现的问题(打提前量)

按本文第五章和经验,实跑大概率会暴露:

1. **SEC EDGAR User-Agent**:`secrets.sh` 里写的 `SEC_USER_AGENT="NT nt1786146194@gmail.com"` 格式不是 SEC 推荐的"AppName/version (contact@email)",可能 403
2. **Alpha Vantage 配额**:免费 key 25 次/天,watchlist 多于 25 票直接挂
3. **FRED 系列 ID 不更新**:DGS10 / DFF 长时间没新数据时返回空,代码可能没处理
4. **report.md 模板兼容性**:新增的 `is_stub` 字段、缓存复用帖,模板能不能正确显示
5. **DeepSeek 第一次真调用**:可能命中 API 限流、超时、批量大小问题

这些不要预先修——**等实跑暴露了再处理**,避免拍脑袋改代码。

---

### 0.9 P0 step 5-7 收尾(2026-05-14 当日完成)

#### 0.9.1 实跑 + DeepSeek 真验证

**派工**:Sonnet test subagent(本轮按用户要求换 Sonnet)+ Sonnet fix(未触发,无 bug)

**全链路实测结果**(2026-05-14 run-daily,ticker = MSFT + CRM):

| lane | 状态 | 证据 |
|---|---|---|
| Reddit | ✅ | MSFT 109 帖 / CRM 14 帖 |
| SEC submissions | ✅ | source_health 全部 ok |
| SEC company_facts | ✅ | MSFT 取到 2026 Q1 fundamentals |
| Alpha Vantage 日线 | ✅ | 到 2026-05-13 |
| FRED 宏观 | ✅ | DGS10 + DFF |
| EIA 能源 | ✅ | 天然气数据 |
| **DeepSeek 真判官** | ✅ **本轮真验通** | HTTP 200 + server UUID + usage 1090 tokens + 235ms latency |
| X (twikit) | ❌ | 预期失败,无 cookie/账号 |
| Discourse | ⊝ | 配置禁用 |
| 期权 | ⊝ | 默认禁用 |

#### 0.9.2 DeepSeek 切到 v4-flash

**为什么换**:用户反馈 dashboard 看不到额度变化,且 `deepseek-chat` 官方公告 2026-07-24 退役。

**真调用证据**(无法本地伪造):
- 服务端 `id`: `1f04e863-d410-4c22-b0b1-33ee0e486866`(DeepSeek 后端生成 UUID)
- 服务端回显 `model`: `deepseek-v4-flash`
- `usage.total_tokens`: 1090(计费字段)
- 网络延迟: 235ms

**之前用户看不到额度变化的真实原因**:DeepSeek dashboard 有数分钟到数十分钟延迟,且早期测试 token 量小,余额变化不显眼。

**代码改动**(`subagent_sentiment.py`,2 处):
1. `__init__` 默认 model:`"deepseek-chat"` → `"deepseek-v4-flash"`
2. `_call_deepseek_api` 请求体新增 `"thinking": {"type": "disabled"}` —— v4-flash 默认开思考模式,不关的话 token 都被 reasoning 吃掉、`content` 返回空字符串、JSON 解析直接挂

**测试**:105/105 通过,5/5 真情绪判决(bull/bear/neutral 都识别正确),证据落 `data/diagnostics/deepseek_v4_flash_verify.json`

#### 0.9.3 打分输出验证(MSFT 实例)

```
bucket_scores:
  fundamentals      : 30/30  ← 营收+17.8%、正经营现金流、现金覆盖债务
  sentiment         : 12/15  ← 披露窗口新鲜
  chain_confirmation: 20/20  ← 公司特有跌幅
  price_flow        :  4/15  ← 仍在 10日/20日 回撤区间
  risk_red_flags    : 17/20  ← 仍处新低 -3
  social_rebound    :  0/10  ← ⚠️ 见 0.9.5
基础总分: 83/100,无 veto
```

→ **打分链路本身正常**,五大维度数据齐全且分值合理。

#### 0.9.4 数据缓存盘点(回应用户提问)

| 缓存表 | schema | 写入 | 读取(命中跳过 LLM/API) | 状态 |
|---|---|---|---|---|
| `social_post_cache`(14天滚动) | ✅ | ✅ social_service 写 | ✅ social_service 读 | **完整闭环** |
| `filing_summary_cache`(SEC 财报) | ✅ | ⚠️ CRUD 存在 | ❌ **未接入 sources/sec.py** | **schema 有,链路没通** |
| Alpha Vantage 日线 | ❌ | ❌ | ❌(仅 yfinance 脚本另存) | **无任何缓存** |
| FRED / EIA | ❌ | ❌ | ❌ | **无任何缓存** |

**结论**:用户问的"财报会一直保存下来"——**还没**。表已经建好,storage.py 里有 `upsert_filing_summary_cache` 和 `get_filing_summaries_for_ticker`,但 `sources/sec.py` 没调它们,所以每次 run-daily 都会重拉 SEC EDGAR。这是 P0 的最后一块没做完的活(本文 0.5 步骤 D)。

**其他要做类似缓存的**:
1. **SEC filing**(优先级最高)——schema 已建,只需把 `sources/sec.py` 改成"先查 `filing_summary_cache`,命中跳过 HTTP"。10-Q/10-K 一旦发布就不变,可缓存 90 天;8-K 列表每次重拉,但单条 8-K 内容也是 immutable
2. **Alpha Vantage 日线**(优先级高)——25 次/天配额是项目最大风险点。考虑用 SQLite 表 `price_cache(ticker, run_date, bars_json)`,同一 ticker 同天复用
3. **FRED / EIA**(优先级低)——quota 宽松,但数据本身是按日 immutable 的,做缓存可以减少网络调用,不急

#### 0.9.5 仍待修的小问题

**social_rebound = 0 的原因**:本日第一次 run-daily 是在 DeepSeek SSL 还没修之前(0.7),那次的社交分都成了 stub,`is_stub=True` 不写缓存(0.3),所以 social_summary 拿到 `social_recent_sample_insufficient`。修完 DeepSeek 后**还没重跑一次 run-daily**——本轮 commit 之后会再跑一次,届时 social_rebound 应该非 0。

---

### 0.10 下一阶段路线(P0 收尾 + P1 启动)

整个 P0 框架现在差**一块**没收尾(filing 缓存接入)。收完 P0 后转 P1。

#### 0.10.1 P0 step 8 —— SEC filing 缓存接入(下一个动作)

**任务**:把 `sources/sec.py` 改成查 `filing_summary_cache` 命中即跳过 HTTP

- 抓 filing 前先 `storage.get_filing_summaries_for_ticker(ticker)` 看本地有没有
- 已有 `accession_number` 跳过详情拉取,直接复用 summary
- 10-Q/10-K:长期缓存(默认无过期,form 一旦归档就 immutable)
- 8-K:列表每次重拉(可能有新事件),但单条 8-K 详情按 accession_number 走缓存

**预计工作量**:2-3 小时(已有 CRUD,主要是改 `sources/sec.py` 的拉取流程 + 单测)

**验收**:同一 ticker 同一天第二次 run-daily,SEC EDGAR 不发出任何 HTTP 请求

#### 0.10.2 P0 步骤 D'(可选):Alpha Vantage 价格缓存

- 新建 `price_cache(ticker, run_date, payload_json)` 表
- `sources/alpha_vantage.py` 写前先查
- 解决 25 次/天配额问题

**预计工作量**:2 小时,可与 0.10.1 并行

#### 0.10.3 P0 全部收尾后,P1 启动(以下都在本文第六章已列,这里只排顺序)

| Pn | 任务 | 涉及文件 | 备注 |
|---|---|---|---|
| **P1-1** | 社交配置缺失时主动报警 | `runtime_preflight.py` + `reporting.py` | 现在 Reddit 任一 subreddit 挂了不会告警,导致 social_rebound 静默 0(本日 MSFT 就遇到了) |
| **P1-2** | Alpha Vantage 配额耗尽 fail-fast | `sources/alpha_vantage.py` | 现在静默 fall through 到 Stooq |
| **P1-3** | 调整数据缺失保底分 | `scoring.py:155,162` | 让缺数据真的扣分,不要 8/30 偏高 |
| **P0-1** | 社交帖 body 字数截断 | `social_service.py` | 已有 stub 限制数量,但单帖 body 没截断 |
| **P0-2** | review_packet event.body 截断 | `review_packets.py` | 同上,8-K body 可能数千词 |
| **P0-3** | 最低数据完整度门控 | `pipeline.py` + `scoring.py` | 全源失败时强制 WATCH |

> P0-1/-2/-3 名义上仍属 P0(本文第六章),但因为本轮已经把"主链路打通 + DeepSeek 真验通"作为更紧急的目标插队,所以编号上排到 P1 之后了。等 0.10.1 + 0.10.2 收尾后看是否优先做这三个。

#### 0.10.4 等用户决策的事(从第七章里挑现在最紧的)

- **#1** 单帖 body 截断字数(500-800 之间挑一个)
- **#4** 数据缺失保底分给多少
- **#8** `official_events` 列表从 8 降到 5 还是别的

剩下的(#2/#3/#5/#7/#9/#10)等 0.10.1 + 0.10.2 跑通再回来定。

---

### 0.13 触发零结果诊断 + Tiger Trader 决策(2026-05-14 当晚)

#### 0.13.1 0 触发的真正原因(haiku 诊断 subagent 给的结论,已复核)

不是触发逻辑 bug。是 **Alpha Vantage 25 次/天免费配额耗尽**:
- run-daily 第一个调用打 QQQ 基准价时(UTC 13:03:40),AV 返回 `"our standard API rate limit is 25 requests per day"`
- `alpha_vantage.py:67` 把 `_daily_limit_exhausted=True` 置位,后续所有调用直接 `return empty`
- Stooq 备援同时返回 `empty csv response`
- 33 个 ticker 全部拿不到价格 → `triggers.py:7` 的 `len(prices) < 21` 把所有人都丢进 `insufficient_price_history`

**触发逻辑本身经实测无 bug**:subagent 用数据库里 2026-05-13 的缓存价跑 `compute_trigger`,得出本来应该触发的 4 只:

| Ticker | 触发条件 |
|---|---|
| MSFT | relative_underperformance 13.59% + fresh_low |
| CRM | 10日跌 8.49% + 相对跌 18.75% + fresh_low |
| CEG | 10日跌 11.9% + 20日跌 13.44% + 相对跌 10.51% |
| NRG | 10日跌 5.79% + 20日跌 8.34% + 相对跌 5.41% |

诊断报告落 `data/diagnostics/trigger_diagnosis.md` + per-ticker JSON。

#### 0.13.2 方向决策:用 Tiger Trader 替换 Alpha Vantage(价格 lane)

用户决定后续直接换接口,价格数据走 **Tiger Trader API**。这一决策让本文之前规划的 **P0 step 9(给 AV 加日级 SQLite 缓存)作废**——既然要换源,不应该在将死的源上做工。

新的等价工作改名为 **P0 step 9'**:写一个 Tiger Trader 价格 source

- 新建 `sharing-resources/src/market_sentiment/sources/tiger.py`
- 实现 `SourcePayload[list[PriceBar]]` 接口(与 `alpha_vantage.py` / `stooq.py` 同型)
- 在 `pipeline.py` 把 Tiger 接入价格 fallback 链:**Tiger 主源 → AV 兜底(25/天用作低频校验)→ Stooq 兜底**
- 缓存还是要做(沿用 step 8 模板,新建 `daily_price_cache` 表),只是写入的是 Tiger 的数据
- 配置:`secrets.sh` 加 `TIGER_API_KEY` / `TIGER_TIGER_ID` / `TIGER_PRIVATE_KEY_PATH` 等

**触发时机**:等用户拿到 Tiger Trader 的开发者凭证后启动,**不在本轮做**。

**临时方案**(到 Tiger 接好前):每日 AV 配额够 watchlist 前 24 票即可(33 票里第 25 票开始静默挂)。短期可以缩 watchlist 到 ≤24 票,或接受"每天后段 ticker 拿不到价"。

#### 0.13.3 本轮还要补的事(本文 0.14)

诊断暴露了一个**与 AV 无关的 bug 候选**:Stooq 在 AV 挂掉时本应兜底,却也返回了 `empty csv response`。是 Stooq URL 拼错、返回格式变了、还是网络瞬时挂了——本轮要派 subagent 查清。如果 Stooq 是个真 bug,在 Tiger 接好之前的过渡期它是唯一兜底,值得修。

另外,**今天 run-daily 0 触发** = **没有 review packet 被新写出** = `social_post_cache` / `filing_summary_cache` 的实跑填充也没观察到。要派 subagent 做一次绕开 AV 的"单票端到端"测试,把 CRM 从触发→社交→DeepSeek→缓存→报告完整跑一次。

---

### 0.14 本轮 subagent 派工计划(2026-05-14 晚)

orchestrator(我)负责拆任务 + 验收,不动代码。所有 subagent 用 haiku。

| # | 任务 | 是否需要先做 | 触发条件 |
|---|---|---|---|
| A | **Stooq 失败根因调查**:为何 `alpha_vantage` 挂掉之后 Stooq 返回 `empty csv response`。范围:`sources/stooq.py` + 抓一次 CRM 的实际 stooq 调用看是 URL / 返回格式 / 还是别的 | 现在 | 无依赖 |
| B | **survey 现有 pipeline 能否走纯缓存价格**:`pipeline.py` 价格获取链里,有没有现成的"AV/Stooq 都挂时 fallback 读 SQLite price 缓存"代码路径,还是要新加 | 现在 | 无依赖,与 A 并行 |
| C | **CRM 单票端到端 E2E 测试**:把 watchlist 临时改成 `CRM + QQQ`,跑 run-daily,确认链路打通:trigger 触发 → SEC + Reddit + DeepSeek → social_post_cache 填充 → filing_summary_cache 填充 → report.md 写出。跑完恢复 watchlist | A + B 后 | 取决于 A/B 结果决定怎么绕过价格问题 |
| D(条件) | **Stooq bug 修复**:若 A 发现是 stooq.py 代码 bug,按 orchestrator 给的精确 diff 修 | A 完成 | 仅当 A=代码 bug |

---

### 0.15 本轮收尾结果(2026-05-14 晚)—— 全链路 E2E 第一次真打通

#### 0.15.1 A、B subagent 结论

- **A (Stooq 诊断)**:Stooq **官方策略变了**——现在要求 `apikey` 参数,免费但需要通过 captcha 在网页手工领取。`stooq.py` 之前不传 key,所以返回的都是"Get your apikey: ..."的纯文本说明,被当 CSV 解析就 0 行。orchestrator 自己 curl 三种变体复核确认。
  - **决策**:既然用户后续要切 Tiger Trader,**不修 Stooq**,Stooq 接口在 Tiger 接好之前进入"已知挂"状态
- **B (缓存价格 survey)**:`daily_prices` SQLite 表早就存在,每次成功 fetch 后 `Storage.upsert_prices()` 都在写,但**从来没人读**。`pipeline._fetch_prices_with_fallback` 当 AV+Stooq 都挂就返回空,不会兜底回缓存。survey 估"trivial,~20 行就能补上"

#### 0.15.2 D + 复核 + F:缓存兜底落地

- **D (impl)**:加 `Storage.read_cached_prices(ticker, days_back=60)` + 在 pipeline fallback 链末尾加缓存兜底块,`partial=True`,message 含 `"using cached prices through <iso_date>; AV+Stooq both unavailable"`。2 个新单测
- **review subagent**:发现 **1 个 BLOCKER**——`cache_status` 没被 `statuses.append`,会导致 `source_health` 看不到缓存事件;1 个 WARN——`date.today()` 用本地时区,与项目其他 UTC 时间不一致
- **F (review 修复)**:加 `statuses.append(cache_status)`、cutoff 改 `datetime.now(timezone.utc).date()`、扩展现有测试增加 4 行 assertion 防回归
- **commit `dbdc6fe`** 推上 GitHub,109/109 单测通过

#### 0.15.3 C subagent:CRM 单票 E2E 实测

把 watchlist 临时改 `CRM + QQQ`,run-daily 一次跑通。**所有 7 个关键阶段全过**:

| 阶段 | 验证项 | 结果 |
|---|---|---|
| 1 | 缓存兜底点亮 | ✅ CRM + QQQ 两个 `daily_prices_cache` source_health 项都是 `partial=True`,message 含 `"using cached prices through 2026-05-13"` |
| 2 | trigger 触发 | ✅ 3 个 reason:10日跌 8.49% + 相对跌 18.75% + fresh_low |
| 3 | SEC 拉数据 | ✅ 营收同比 +19.1%,经营现金流 +46.6% |
| 4 | filing_summary_cache 填充 | ✅ CRM 写入约 963 行,横跨 Form 4 / 144 / 10-Q / 8-K / S-8 五种类型 |
| 5 | Reddit 抓帖 | ✅ 16 条真帖 |
| 6 | DeepSeek 真判 | ✅ social_post_cache 入库 7 bull / 6 bear / 3 neutral,**没有 `unknown` stub**,DeepSeek 确实跑通 |
| 7 | report.md 写出 | ✅ CRM 分章生成,bucket_scores 5/6 维度填齐,基础总分 83/100(fundamentals 30,sentiment 9,chain_confirmation 20,price_flow 7,risk_red_flags 17,social_rebound 0),state = Watch |

E2E 测试报告落 `data/diagnostics/crm_e2e_test.{json,md}`。

#### 0.15.4 已知小问题(非 bug,记录用)

**social_rebound = 0/10 的原因**:CRM 总共 16 条帖里,**只有 2 条落在近 72h 窗口**(`social.lookback_hours = 72`),低于 `social.min_recent_posts = 10` 门槛。social_rebound 评分本身要求"近期样本足够",2 条不够算,直接返回 0。

- 这是社交评分算法的设计,不是 pipeline 或 DeepSeek 的 bug
- 主 agent(Claude/Codex)读 review packet 时,看 `social_summary.recent_stance = 0.58`(轻度偏多)仍然可以做出有信息量的判断
- 后续可能要做的调整:把 lookback_hours 拉宽到 168h(7 天)或把 min_recent_posts 降到 5,**等多积累几天数据后再校准**

#### 0.15.5 本轮派工总结

orchestrator(我)→ Haiku subagent 矩阵:

| Subagent | 类型 | 任务 | 结果 |
|---|---|---|---|
| A | haiku 诊断 | Stooq 失败根因 | 找到 → API key 政策变更 |
| B | haiku survey | pipeline 是否有缓存价格 fallback | 没有 → "trivial 20 行能加" |
| D | haiku impl | 加缓存 fallback | 改对,但漏了 1 个 BLOCKER |
| (review) | haiku review | 审 D 的 diff | 抓到 BLOCKER + WARN |
| F | haiku fix | 补 BLOCKER + WARN + 防回归测试 | 落地,109/109 通过 |
| C | haiku 测试 | CRM 单票 E2E | 7/7 阶段全过 |

**关键收获**:
- haiku impl 经常**遗漏副效应**(像 D 没 append cache_status),所以**配对 review subagent 是值得的**——这一对组合的失误率明显低于"单 impl 不审计"
- orchestrator 写 spec 时把"必须有"的具体字符串(`"using cached prices through"`)写死,review 和测试两端都好对照
- haiku 给的"诊断结论"在确认前必须 orchestrator 亲自复核——A 报告"Stooq 要 API key",我亲自 curl 三遍才信

---

### 0.11 P0 step 8 完成(2026-05-14)—— SEC filing 缓存接入

#### 0.11.1 干了什么

**派工**:Haiku impl + Haiku review,串行执行,我做 orchestrator。

**代码改动**(`sources/sec.py`,commit `74a77dc`):

1. `fetch_recent_events` 在 events 列表构建后追加缓存写入逻辑:
   - 先 `storage.get_filing_summaries_for_ticker(ticker)` 拉已缓存的 accession_number 集合
   - 只把 **不在缓存里** 的 filing 构建成 `FilingSummaryCacheRow`
   - 调 `storage.upsert_filing_summary_cache(new_rows)`,`INSERT OR IGNORE` 兜底
2. `SourceStatus.message` 末尾追加 `"; cache_hit={hit}/{total}"`,review packet 里能直接看
3. **副作用**:把 `items = zip(...)` 改成 `items = list(zip(...))`——因为 zip 是单次迭代器,新代码要二次遍历,不 list 化会导致缓存全空(review subagent 重点核对了这点)

**未做(故意)**:
- HTTP fetch 仍每次都打。submissions index 是"目前有哪些 filing"的唯一真源,必须每次拉
- `summary` / `sentiment` / `key_metrics_json` 现在是占位(`event.title` / `"unknown"` / `"{}"`),等 Phase 2 加 LLM 总结再填
- "命中缓存就跳过昂贵步骤"的收益要等 Phase 2 才显现;Phase 1 只是把持久化通道接通

#### 0.11.2 测试

新增 2 个用例在 `test_sources.py`:
- `test_sec_filing_cache_first_run_writes_all`:首跑 3 条 filing,期望全部写入,message 含 `cache_hit=0/3`
- `test_sec_filing_cache_second_run_dedupes`:预置 2 条缓存,期望只新写 1 条,message 含 `cache_hit=2/3`

总测试:**107/107 通过**。

#### 0.11.3 验过的事 + 没验的事

| 检查项 | 结果 |
|---|---|
| 单测覆盖首跑/dedupe 路径 | ✅ |
| 实跑 run-daily 看真实缓存填充 | ⊝ 本日 watchlist 无 ticker 触发,没看到 |
| `social_post_cache` 在新 DeepSeek 下的填充 | ⊝ 同上,需要触发才能验 |

#### 0.11.4 派工流程留档

- impl subagent(haiku):按 orchestrator 写好的精确 spec(包括字段映射、命名、`zip→list` 提示)直接改,**不发挥**
- review subagent(haiku):独立审计,按 A-F 6 类(正确性 / scope / 副作用 / 测试质量 / 边界 / pytest)逐项打分
- 两个 subagent **并不互相对话**,只通过 orchestrator(我)中转结论
- haiku 这次没翻车——前提是 orchestrator 把 spec 写得足够精确(字段、命名、行号都给到)。如果只给"实现 SEC 缓存"这种粗指令,haiku 大概率会发挥过头

---

### 0.12 下一步(2026-05-14 当前)

#### 0.12.1 立即可做的

| 任务 | 价值 | 工作量 | 注意点 |
|---|---|---|---|
| **P0 step 9:Alpha Vantage 价格缓存** | 高(25 次/天配额是最大风险) | 2-3 小时 | 参照 filing_summary_cache 模板,新建 `daily_price_cache(ticker, run_date, payload)` |
| **临时降阈值跑一次 run-daily 验社交链路** | 中 | 30 分钟 | 把 `config/watchlist.toml` 里 `drawdown_10d` 从 -0.08 降到 -0.02 跑一次,看 social_rebound 是不是非零 + social_post_cache 是否写入真情绪;跑完恢复 |
| **P1-1:社交配置缺失主动报警** | 中 | 1 小时 | Reddit 单 subreddit 挂会静默,导致 social_rebound 0;preflight 阶段直接 fail-fast 或 WARN |
| **P1-3:数据缺失保底分调整** | 中 | 1 小时 | 把 fundamentals 缺数据保底分从 8 降到 4,sentiment 从 5 降到 3 |

#### 0.12.2 推荐顺序

1. **先做"临时降阈值实跑"**(0.5 小时)——把当前所有未真正验证的环节(DeepSeek→social_post_cache→social_rebound)一起跑通,这是最便宜的端到端验证手段。**不需要 subagent,直接改 watchlist.toml 跑一次再改回来**
2. **然后做 P0 step 9 Alpha Vantage 缓存**(2-3 小时,派 haiku 双 subagent,模式同 step 8)
3. **再做 P1-1 + P1-3**(快速改动,可一起派一个 haiku impl + 一个 review)

#### 0.12.3 等用户决策的事(从第七章)

- **#1** 单帖 body 截断字数(500-800 之间挑一个)
- **#4** 数据缺失保底分给多少(P1-3 直接影响)
- **#8** `official_events` 列表从 8 降到 5 还是别的

---

## 一、项目概览

本项目是一套**美股深度研究系统**，基于 SEC 财务数据、官方披露、估值模型和规则引擎为单只股票生成证据包并输出投资决策建议，不限制价格水平或跌幅触发条件。

### 两个 Skill 定位

| Skill | 目录 | 定位 | 输出档位 |
|---|---|---|---|
| `equity-research` | `skills/equity-research/` | **单股深度研究**：任意价格水平，完整跑 5 条证据 lane + 12 模型估值层，输出带 `invalidate_if` / `rerate_if` 的行动建议 | Reject / Watch / Starter / Add / Exit（5 档） |
| `us-smallmid-dislocation` | `skills/us-smallmid-dislocation/` | **小中盘宽表筛选**：在候选股票宇宙中批量过滤，找出值得深入研究的错价标的，输出候选排名 | Pass / Watchlist / Investigate（3 档） |

两个 skill 共享 `sharing-resources/` 下的全部 Python 库、配置、测试和文档。

---

## 二、详细目录结构

```
市场情绪/
├── README.md                          # 项目入口说明
├── pyproject.toml                     # Python 包定义（含 market_sentiment 源码路径）
├── .gitignore
├── config/
│   └── watchlist.toml                 # 监控股票列表（ticker + 基准 ETF 映射）
│
├── skills/
│   ├── equity-research/
│   │   ├── SKILL.md                   # Skill 说明（Claude Code 读取）
│   │   ├── defaults/                  # 默认配置
│   │   ├── references/                # Skill 级别参考文档
│   │   ├── docs/                      # 设计笔记
│   │   └── scripts/
│   │
│   └── us-smallmid-dislocation/
│       ├── SKILL.md                   # Skill 说明
│       ├── defaults/universe.toml     # 小中盘候选宇宙配置
│       ├── references/                # Skill 级别参考文档
│       └── scripts/
│           └── screen_candidates.py   # 批量筛选脚本（414 行）
│
└── sharing-resources/
    ├── docs/
    │   ├── architecture.md            # 整体架构说明
    │   ├── design_notes.md            # 设计决策笔记
    │   └── local_operations.md        # 本地运维操作手册
    ├── references/
    │   ├── configuration_and_secrets.md   # 所有 API key 配置说明
    │   ├── runtime_and_shared_scripts.md  # 运行时脚本说明
    │   └── data_and_outputs.md            # 输出文件格式说明
    ├── secrets/
    │   ├── README.md
    │   ├── market_sentiment.secrets.example.sh  # 密钥模板（不含真实值）
    │   └── market_sentiment.secrets.sh          # 真实密钥（已在 .gitignore）
    ├── examples/
    │   └── sample_daily_report.md     # 示例日报输出
    ├── scripts/
    │   └── redact_sensitive_json.py   # 脱敏工具：在分享 raw JSON 前移除敏感字段
    ├── tests/                         # 单元测试
    │   ├── test_pipeline.py
    │   ├── test_scoring.py
    │   ├── test_triggers.py
    │   ├── test_social_service.py
    │   ├── test_sources.py
    │   ├── test_config.py
    │   └── test_storage.py
    └── src/market_sentiment/          # 核心 Python 库
        ├── __main__.py                # 包入口
        ├── cli.py                     # CLI 命令：init-db / preflight / review / review-ticker / valuation-order / show-report / cleanup-data
        ├── config.py                  # 配置解析（读 TOML + 环境变量）
        ├── models.py                  # 数据模型（dataclass：PipelineContext, ScoreCard, SocialPost …）
        ├── pipeline.py                # 主 pipeline 编排（触发→采集→评分→输出）
        ├── triggers.py                # 触发检测逻辑（相对跌幅/绝对跌幅/新低判断）
        ├── scoring.py                 # 6 维度打分 + 决策映射（349 行）
        ├── review_packets.py          # 组装 JSON review packet 交给 Agent（含 official_events[:8]）
        ├── manual_agent_report.py     # 人工报告生成辅助
        ├── reporting.py               # Markdown 日报生成
        ├── storage.py                 # SQLite 持久化层
        ├── social_service.py          # 社交信号采集服务（101 行，含 _cap_posts 截断）
        ├── social_rebound.py          # 社交情绪分析（stance delta / breadth 计算）
        ├── http.py                    # 统一 HTTP 客户端
        ├── email_delivery.py          # 邮件推送
        ├── runtime_preflight.py       # 启动前环境检查
        └── sources/                   # 各数据源适配器
            ├── base.py                # 基类 + SourcePayload / SourceStatus 定义
            ├── sec.py                 # SEC EDGAR（submissions + company facts XBRL）
            ├── alpha_vantage.py       # Alpha Vantage 日线价格（TIME_SERIES_DAILY）
            ├── options_alpha_vantage.py  # Alpha Vantage 期权链（QUERY_OPTION_CHAIN，默认 disabled）
            ├── stooq.py               # Stooq 价格（Alpha Vantage 备用）
            ├── fred.py                # FRED 宏观数据（DGS10, DFF）
            ├── eia.py                 # EIA 能源数据（天然气）
            ├── social_base.py         # 社交基类 + 去重逻辑
            ├── social_registry.py     # 社交 provider 注册表
            ├── reddit.py              # Reddit 采集（stocks/investing/wallstreetbets 等 subreddit）
            ├── discourse.py           # Discourse 论坛采集（可选）
            ├── x.py                   # X/Twitter 入口适配器
            └── x_provider.py          # X 双引擎实现：twscrape + twikit（591 行）
```

---

## 三、核心数据流

### 3.1 主流程（文字版）

```
① 触发检测（triggers.py）
   │  输入：过去 21 条日线（yfinance 缓存 / Alpha Vantage / Stooq）
   │  条件：10 日回撤 OR 20 日回撤 OR 相对行业 ETF 显著跑输 OR 新低
   ▼
② 多源并行采集（pipeline.py 调度各 sources/）
   ├─ 官方披露   → SEC EDGAR submissions（10-Q / 10-K / 8-K）
   ├─ 财报数字   → SEC company facts（XBRL：Revenue / OperCF / CapEx / Cash / Debt）
   ├─ 宏观利率   → FRED（DGS10 / DFF）
   ├─ 能源价格   → EIA（天然气，可选）
   ├─ 社交情绪   → Reddit + Discourse + X（均可按配置开关）
   ├─ 分析师目标价 → Yahoo quoteSummary / Finnhub（触发票，非阻塞，→ analyst_summary）
   └─ 期权数据   → Alpha Vantage 期权链（默认 disabled，max_contracts=80）
   ▼
③ 事件标签（scoring.classify_event_tag）
   │  判断：COMPANY_SPECIFIC / SECTOR_WIDE / MARKET_WIDE
   ▼
④ 6 维度打分（scoring.build_scorecard）
   │  见第四节
   ▼
⑤ 组装 Review Packet（review_packets.build_review_packet）
   │  输出：JSON（含 official_events[:8] + 社交代表帖 + 财报快照 + 宏观摘要）
   ▼
⑥ Agent 判断 / 日报输出（reporting.py + email_delivery.py）
   │  输出：report.md + review_packets/<ticker>.json + 邮件
```

### 3.2 数据源汇总表

| 数据源 | 用途 | 主/备 | 限制 |
|---|---|---|---|
| Yahoo Chart（yfinance 缓存脚本） | 日线价格缓存 | 缓存主力 | 无 API key，但非实时 |
| Alpha Vantage `TIME_SERIES_DAILY` | 实时日线价格 | 主 | 25 次/天（免费层） |
| Stooq | 日线价格 | Alpha Vantage 备用 | 无 retry，无 key |
| SEC EDGAR `submissions` | 10-Q/10-K/8-K 元数据 | 唯一 | 无 fallback |
| SEC EDGAR `company_facts` | XBRL 财报数字 | 唯一 | 无 fallback |
| FRED | DGS10 / DFF 宏观利率 | 唯一 | 无 fallback |
| EIA | 天然气价格 | 唯一（可选） | 无 fallback |
| Reddit | 社交情绪（5 个 subreddit，每源最多 75 帖） | 社交主力 | 需 CLIENT_ID + SECRET |
| Discourse | 社交情绪（可选论坛） | 社交辅助 | base_url 配置失误会静默 skip |
| X/Twitter（twscrape / twikit） | 社交情绪 | 社交辅助，**默认 disabled** | 需账号/cookies，国内受限 |
| Alpha Vantage 期权链 | 期权 put/call 比率 | 辅助确认，**默认 disabled** | 占 Alpha Vantage 25 次配额 |
| Yahoo quoteSummary（分析师）/ Finnhub | 分析师目标价 + 评级升降级动量（Layer 2 仅供参考，不进打分） | Yahoo 主 / Finnhub 备 | Finnhub 需可选 key；非美股可能两者皆空 |

---

## 四、打分系统详解

### 4.1 六维度权重与满分

| 维度 | 满分 | 文件位置 | 说明 |
|---|---|---|---|
| Fundamentals（基本面） | 30 | `scoring.py:101` | 财报数字新鲜度 + Revenue/OCF/现金/债务 + SEC 归档表单 |
| Sentiment（披露情绪） | 15 | `scoring.py:160` | SEC 表单关键词 + 负面披露扣分 + 表单新鲜度 |
| Chain Confirmation（链确认） | 20 | `scoring.py:214` | 公司特有 vs 行业 vs 市场宽幅 + 宏观上下文 + 期权信号 |
| Price Flow（价格流） | 15 | `scoring.py:239` | 10 日/20 日回撤幅度 + 相对跑输程度 + 是否在新低 |
| Risk（风险红旗） | 20 | `scoring.py:257` | 含两个 hard veto 触发点（见下） |
| Social Rebound（社交反弹） | 10（可为 -6） | `scoring.py:188` | 近期 stance delta + breadth score |

**总分上限：100（含 Social）；基础上限：100（不含 Social 负分）**

### 4.2 决策阈值映射（`scoring.py:302`）

| 总分 | 状态 | 备注 |
|---|---|---|
| 有 veto | REJECT | 无条件 |
| < 65 | REJECT | |
| 65–71 | WATCH | |
| 72–79 且处于新低（`fresh_low`） | WATCH | 下调一档 |
| 72–79 无新低 | STARTER | |
| ≥ 80 | ADD | |

### 4.3 Hard Veto 触发条件（`scoring.py:266–291`）

| Veto 名称 | 触发条件 |
|---|---|
| `negative_official_keyword` | SEC 披露标题含 bankruptcy / fraud / restatement / delisting / default / investigation / guidance cut |
| `companyfacts_structural_break` | Revenue 同比 < -35% **且** OCF < 0 **且** 处于新低 |

### 4.4 社交 Guardrail（`scoring.py:316`）

社交分 > 0 时，若社交加分将候选从非 ADD 状态拉升到 ADD，则**强制降回 STARTER**（若基础状态为 REJECT 则仅升至 WATCH）。
社交分 ≤ 0 时，guardrail **不介入**（负分已直接反映在总分中）。

> **漏洞**：当 `partial_coverage=True` 时，社交正分被清零（`scoring.py:208`），但社交负分（最低 -6）仍然有效，这是非对称的处理，可能过度惩罚有数据缺失但社交悲观的个股。

---

## 五、当前已识别的问题

### 5.1 上下文撑爆问题

#### 问题 A：Reddit / Discourse 帖子全文无有效截断

| 项目 | 内容 |
|---|---|
| **症状** | 对社交帖子丰富的股票（如 NVDA、TSLA），一次采集可能拉取 Reddit 4-5 个 subreddit × 75 帖 = 300+ 帖，加上 Discourse；每帖 body 完整入内存，传入 LLM 上下文轻松超过 100k token |
| **位置** | `social_service.py:64`（`_cap_posts` 仅按数量截断，不截断单帖字数）；`social_rebound.py:103`（全文传入关键词匹配） |
| **根因** | `_cap_posts` 只做帖子数量上限（`max_posts_per_source`），未对单帖 `body` 字段设字数上限 |
| **影响** | **高**：可触发 API 报错或超时，或拖慢整个 pipeline |

#### 问题 B：Review Packet 全量序列化

| 项目 | 内容 |
|---|---|
| **症状** | 生成的 `review_packets/<ticker>.json` 包含 `official_events[:8]`（每条含完整 title + body）+ `social_summary`（含 representative_posts 全文）+ 21 条价格棒 + 财报快照；大股票可能 MB 级 |
| **位置** | `review_packets.py:73`（`official_events[:8]`）、`review_packets.py:76`（`_serialize_social_summary`） |
| **根因** | 没有对 `event.body` 做字数截断，SEC 8-K 正文可能数千词 |
| **影响** | **高**：直接影响 Agent 调用的 context window |

---

### 5.2 数据获取不到问题

#### 问题 C：Alpha Vantage 25 次/天限额

| 项目 | 内容 |
|---|---|
| **症状** | watchlist 超过 25 个股票时，当天第 26 个起价格数据拉取失败；Stooq fallback 无 retry 机制，网络抖动时也会静默返回空 |
| **位置** | `sources/alpha_vantage.py`（无配额计数器）；`sources/stooq.py`（无 retry） |
| **根因** | 免费 API key 限制；没有统一的 rate-limit 管理层 |
| **影响** | **高**：价格数据缺失时 `price_flow` 维度得分为 0，`triggers.py` 可能误判触发或漏触发 |

#### 问题 D：X/Twitter 采集国内基本不可用

| 项目 | 内容 |
|---|---|
| **症状** | 国内网络 twscrape / twikit 均需代理；cookies 过期后 twikit 尝试重新登录可能触发 Twitter 风控；即便登录成功，搜索 API 限制也随时可能改变 |
| **位置** | `sources/x_provider.py:164–167`（twscrape 异常被 `_unavailable` 吞掉，返回 `success=False`）；`x_provider.py:283–286`（twikit 同）|
| **根因** | 依赖 Twitter 非官方抓取库，平台反爬措施不稳定；无官方 API key |
| **影响** | **中**：X 默认 disabled，不影响主流程；但若开启且失败，社交分降为 0 |

#### 问题 E：SEC EDGAR / FRED / EIA 无 fallback

| 项目 | 内容 |
|---|---|
| **症状** | SEC 宕机或返回 429 时，`fundamentals` 和 `sentiment` 两个维度数据全空；FRED 宕机时宏观数据缺失 |
| **位置** | `sources/sec.py`、`sources/fred.py`、`sources/eia.py` |
| **根因** | 均为唯一数据源，无本地缓存或备用源 |
| **影响** | **中**：`score_fundamentals` 无数据时返回固定 8/30（`scoring.py:155`），可能虚高 |

---

### 5.3 静默失败问题

#### 问题 F：配置错误导致源被静默跳过

| 项目 | 内容 |
|---|---|
| **症状** | Discourse `base_urls` 未配置、Reddit `CLIENT_ID` 缺失时，对应 provider 直接 skip，日志里可能只有一行 warning，用户不会发现社交数据从未采集 |
| **位置** | `social_service.py:38–42`（`provider.is_enabled()` 为 False 时直接 continue）|
| **根因** | preflight 检查（`runtime_preflight.py`）未对社交配置做强验证 |
| **影响** | **高**：社交分默认为 0，若 guardrail 未激活，评分结论仍可能是 ADD |

#### 问题 G：所有源失败时 pipeline 仍继续执行

| 项目 | 内容 |
|---|---|
| **症状** | 极端情况（API 全挂）下，pipeline 跑完出一个评分，但 6 个维度大多是默认/保底分，输出结论毫无依据 |
| **位置** | `pipeline.py`（无全局数据完整性门控）；`scoring.py:58`（`partial_coverage` 只影响社交正分） |
| **根因** | 没有设计"最低数据完整度"阈值；`partial_coverage` 只影响社交维度而非整体结论 |
| **影响** | **高**：可能输出误导性的 STARTER/ADD 决策 |

---

### 5.4 评分鲁棒性问题

#### 问题 H：数据缺失时部分维度给保底分而非惩罚分

| 项目 | 内容 |
|---|---|
| **症状** | SEC 数据全空时，`score_fundamentals` 返回 8/30（`scoring.py:155`）；情绪数据全空时，`score_sentiment` 返回 5/15（`scoring.py:162`）；两个维度合计保底 13/45，大约是满分的 29%——不低 |
| **位置** | `scoring.py:155`（`return BucketScore("fundamentals", 8, 30, ...)`）；`scoring.py:162`（`return BucketScore("sentiment", 5, 15, ...)`） |
| **根因** | 保底分设计初衷是"数据缺失不该直接 REJECT"，但当前值偏高，可能掩盖真实数据质量问题 |
| **影响** | **中**：与问题 G 叠加时，缺数据个股可能被误升档 |

#### 问题 I：社交 guardrail 有非对称漏洞

| 项目 | 内容 |
|---|---|
| **症状** | `partial_coverage=True` 时，社交正分清零但负分（-6）保留（`scoring.py:208`）；在数据不完整的情况下，社交情绪会单向拉低分数 |
| **位置** | `scoring.py:208–210` |
| **根因** | 保守设计：正分不信任，负分信任；但当数据本身不可靠时，负分同样不应完全信任 |
| **影响** | **低**：边缘情形，但会导致 partial coverage 个股被额外惩罚 |

---

### 5.5 工程化问题

#### 问题 J：无统一 rate-limit 与 retry 层

| 项目 | 内容 |
|---|---|
| **症状** | Alpha Vantage、SEC EDGAR、FRED、EIA 各自处理错误，无统一指数退避 retry；Alpha Vantage 配额耗尽时不会提前中止，而是逐个失败 |
| **位置** | `http.py`（无 retry 中间件）；各 `sources/*.py` |
| **根因** | HTTP 客户端未集成 retry / backoff 策略 |
| **影响** | **中**：网络抖动导致大量源数据不稳定 |

#### 问题 K：X 分页无 early-abort，低命中率时浪费配额

| 项目 | 内容 |
|---|---|
| **症状** | twikit 分页循环（`x_provider.py:231`）在 `dropped` 计数持续增长时仍继续翻页，直到凑满 `limit` 条匹配帖或页面耗尽 |
| **位置** | `sources/x_provider.py:231–280` |
| **根因** | 没有 `drop_rate > threshold → abort` 逻辑 |
| **影响** | **低**：X 默认 disabled；开启后消耗时间和账号配额 |

#### 问题 L：无本地数据缓存层（除价格外）

| 项目 | 内容 |
|---|---|
| **症状** | SEC、FRED、EIA 数据每次 pipeline 运行都重新请求；同一天重跑时重复消耗 API 配额 |
| **位置** | `sources/sec.py`、`sources/fred.py`、`sources/eia.py` |
| **根因** | 仅 `update_price_cache.py` 实现了本地缓存，其他源无缓存设计 |
| **影响** | **低**：当前规模下尚可接受，扩大 watchlist 后会成问题 |

---

## 六、下一步改进路线图

### P0 紧急（撑爆 / 关键源失败导致决策失真）

#### P0-1：给社交帖子 body 加字数截断

- **任务**：在 `_cap_posts`（`social_service.py:96`）中，对每帖的 `body` 字段截断到 N 字符（建议 500–800 字符，待用户决策）
- **涉及文件**：`sharing-resources/src/market_sentiment/social_service.py`
- **验收标准**：对任意股票跑 pipeline，`social_summary` 总 token 估算 < 8000；单帖 body 不超过设定上限

#### P0-2：给 Review Packet 里的 `event.body` 加截断

- **任务**：在 `review_packets.py` 的 `_serialize_event` 函数中，将 `body` 截断到 1000 字符；`official_events` 上限从 8 条降至 5 条（或由配置控制）
- **涉及文件**：`sharing-resources/src/market_sentiment/review_packets.py`
- **验收标准**：生成的 `review_packets/<ticker>.json` 文件大小 < 50KB

#### P0-3：增加"最低数据完整度门控"

- **任务**：在 `pipeline.py` 中，统计成功 source 数量；若核心源（SEC + 价格）均失败，将状态强制锁定为 `WATCH` 并在输出中明确标注 `data_quality: insufficient`，禁止输出 STARTER/ADD
- **涉及文件**：`sharing-resources/src/market_sentiment/pipeline.py`、`sharing-resources/src/market_sentiment/scoring.py`
- **验收标准**：模拟所有源失败场景，pipeline 输出状态为 WATCH，report.md 包含"数据不足"警告

---

### P1 高优（数据质量、决策可靠性）

#### P1-1：社交配置缺失时主动报警

- **任务**：在 `runtime_preflight.py` 中，对 `social.enabled=True` 但所有 provider 实际不可用的情况发出 WARNING 级别输出（而非静默跳过）；在 report.md 中增加"社交数据采集状态"摘要
- **涉及文件**：`sharing-resources/src/market_sentiment/runtime_preflight.py`、`sharing-resources/src/market_sentiment/reporting.py`
- **验收标准**：Reddit 凭证缺失时，CLI 输出明确 WARNING，report.md 顶部有"社交数据：未采集"标注

#### P1-2：Alpha Vantage 配额耗尽时 fail-fast

- **任务**：在 `sources/alpha_vantage.py` 中，检测 API 返回的 `"Note"` 或 `"Information"` 字段（Alpha Vantage 限额响应标志），立即抛出 `RateLimitError`；pipeline 收到后对该 ticker 改用 Stooq 并记录日志
- **涉及文件**：`sharing-resources/src/market_sentiment/sources/alpha_vantage.py`、`sharing-resources/src/market_sentiment/pipeline.py`
- **验收标准**：模拟配额响应，pipeline 自动切换 Stooq 并在 `source_health` 中记录 `rate_limited: true`

#### P1-3：调整数据缺失保底分

- **任务**：将 `fundamentals` 无数据时的保底分从 8 降至 4，`sentiment` 从 5 降至 3；同时在 ScoreCard 增加 `data_completeness_pct` 字段
- **涉及文件**：`sharing-resources/src/market_sentiment/scoring.py:155,162`
- **验收标准**：全空数据下总分 < 55，强制落入 REJECT；已有测试 `test_scoring.py` 全部通过

#### P1-4：SEC 数据增加本地日级缓存

- **任务**：在 `storage.py` 中新增 `sec_cache` 表（ticker + run_date + payload JSON）；`sources/sec.py` 请求前先查缓存，命中则跳过 HTTP 调用
- **涉及文件**：`sharing-resources/src/market_sentiment/storage.py`、`sharing-resources/src/market_sentiment/sources/sec.py`
- **验收标准**：同一 ticker 同一天第二次运行，SEC 不发出任何 HTTP 请求

---

### P2 中优（扩展数据源、增强鲁棒性）

#### P2-1：http.py 增加统一 retry / backoff 中间件

- **任务**：在 `http.py` 中封装指数退避 retry（建议 3 次，间隔 1/2/4 秒），对所有 5xx 和网络超时自动重试；各 source 适配器无需改动
- **涉及文件**：`sharing-resources/src/market_sentiment/http.py`
- **验收标准**：模拟第一次 503、第二次成功场景，pipeline 正常完成且 `source_health` 显示 `retried: 1`

#### P2-2：考虑 StockTwits 作为 X 替代源

- **任务**：在 `sources/` 下新增 `stocktwits.py`，调用 StockTwits 公开 API（无需账号）获取 `$TICKER` 流；注册到 `social_registry.py`
- **涉及文件**：`sharing-resources/src/market_sentiment/sources/stocktwits.py`（新建）、`sharing-resources/src/market_sentiment/sources/social_registry.py`
- **验收标准**：StockTwits 数据作为新社交源出现在 `source_health`；X disabled 时社交维度仍有数据

#### P2-3：X provider 加 early-abort 逻辑

- **任务**：在 `x_provider.py` 的 twikit 翻页循环中，若累计 `dropped / (posts + dropped) > 0.9` 且已翻 3 页，则 break
- **涉及文件**：`sharing-resources/src/market_sentiment/sources/x_provider.py`
- **验收标准**：模拟低命中率场景，翻页次数 ≤ 3

---

### P3 低优（代码组织、文档、测试）

#### P3-1：social_rebound 负分在 partial coverage 下对称处理

- **任务**：在 `scoring.py:208` 处，`partial_coverage=True` 时同时将负分截断到 0（或减半），与正分处理对称
- **涉及文件**：`sharing-resources/src/market_sentiment/scoring.py`
- **验收标准**：partial coverage 场景下 social_rebound 分数为 0（不扣分）；补充 `test_scoring.py` 用例

#### P3-2：补充集成测试：全源失败场景

- **任务**：在 `tests/test_pipeline.py` 中增加"所有源返回失败"的 fixture，断言输出 state != ADD/STARTER
- **涉及文件**：`sharing-resources/tests/test_pipeline.py`
- **验收标准**：新增测试通过

#### P3-3：将 API key 配置校验集中到 preflight

- **任务**：`runtime_preflight.py` 中增加对 `ALPHA_VANTAGE_KEY`、`REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET` 的存在性检查，输出清晰的配置状态表格
- **涉及文件**：`sharing-resources/src/market_sentiment/runtime_preflight.py`
- **验收标准**：`market-sentiment preflight` 命令输出每个 API key 的 `✓ 已配置 / ✗ 缺失` 状态

---

## 七、待用户决策的问题

以下设计决策需要用户拍板后才能动工：

| # | 问题 | 选项 / 背景 |
|---|---|---|
| 1 | **社交帖子单帖 body 截断到多少字符？** | 建议 500–800 字符；过短会丢失上下文，过长继续撑爆 context |
| 2 | **Alpha Vantage 限额耗尽后：fail-fast 还是仅警告继续？** | fail-fast 更保守（价格数据不可靠时不出结论）；仅警告适合容忍 Stooq 备用 |
| 3 | **X 数据国内难以获取，是否用 StockTwits 替代？还是放弃社交 X 源？** | StockTwits 有公开 API，无需账号；雪球/东方财富需要中文 NLP 适配 |
| 4 | **数据缺失时打分策略：给中位分 vs 给 0 vs 直接触发降档？** | 当前是保底分（偏高）；给 0 过于激进；建议给 1/4 满分但强制标注 partial |
| 5 | **社交文本要不要走 LLM 摘要子模块，而不是原文塞进 context？** | LLM 摘要可大幅压缩 token，但增加一次 API 调用成本和延迟 |
| 6 | **SEC company_facts 要缓存多久？** | 财报 90 天发一次；建议缓存 7 天；但需要手动失效机制 |
| 7 | **pipeline 全源失败时：静默输出 WATCH 还是直接报错退出？** | 静默 WATCH 保证 pipeline 不中断；报错退出更能暴露问题 |
| 8 | **`official_events[:8]` 要降到几条？body 要截断到多少字？** | 5 条 + 1000 字 body 是一个起点，需结合实际 Agent 测试确认 |
| 9 | **是否为 Discourse 配置强制验证（非法 base_url 直接报错）？** | 当前静默 skip 会掩盖配置失误，建议至少 WARNING |
| 10 | **watchlist 规模预期多大？是否需要支持批量并发采集？** | 当前为串行；> 50 股时建议并发 + 本地缓存，但设计复杂度上升 |

---

*本文档基于代码版本 `0b64e9c`，结合源码文件 `scoring.py`、`social_service.py`、`x_provider.py`、`social_rebound.py`、`review_packets.py` 的直接阅读生成。*

---

## 八、2026-05-15 量化研究视角审计（Sonnet）

派 Sonnet subagent 以 **1 周 – 1 个月持仓周期的量化研究员视角** 全量阅读项目，从 6 个维度审计严谨性。结论按优先级整理如下。

### 已完成

#### Freshness block（commit `6bde150`）
- 在 review packet 顶层增加 `freshness` 字段，预计算 `fundamentals_age_days`、`fundamentals_filed_age_days`、`official_event_age_days`，并按阈值生成 `caveats` 字符串数组
- > 100 天：`current quarter likely unreported`；60–100 天：`verify whether next quarter has been reported`；> 30 天事件：`no recent disclosures`
- 138/138 测试通过；2 文件 +264 行
- 解决问题：主 agent 不再容易把数月前的财报数字当作"当前财务状况"

### 进行中 — Round 1（impl 已派 Subagent U，pending review）

#### A. 价格源调整一致性（P0，audit 首要发现）
- 问题：5 层 fallback 链中 Tiger 默认 `RightOption.no_right`（不复权），AV 用 `TIME_SERIES_DAILY` + `"4. close"`（未调整），Yahoo 用 `quote.close`（仅 split 调整，无 dividend），Stooq 已调整。源切换时（如 Tiger 失败转 Yahoo）若期间发生拆股/分红，会产生 phantom drawdown 触发假信号
- 修复：
  - `sources/tiger.py`：`get_bars(..., right=RightOption.br_forward)`
  - `sources/alpha_vantage.py`：endpoint 换 `TIME_SERIES_DAILY_ADJUSTED`，close 字段换 `"5. adjusted close"`
  - `sources/yahoo_finance.py`：从 `quote.close` 换到 `adjclose.adjclose`，保留 fallback

#### B. Pharma layer 重分类（P1）
- 问题：`config/watchlist.toml` 把 LLY/JNJ/MRK/ABBV/PFE/BMY/AMGN/GILD/REGN/VRTX 10 只医药股标为 `layer = "ai_applications"`（注释明确说是 workaround）。这导致两个错误：
  1. 用 ai_applications 的高 beta 阈值（10d drawdown -8%），但医药股低 beta，容易在 PDUFA / 管线消息日触发假信号
  2. `chain_confirmation` 用 same-layer peer 比较——LLY 被拿来跟 CRM / IOT 比，板块性 selloff 会被错误归类为 `COMPANY_SPECIFIC`，反而 +6 分加到 chain_confirmation
- 修复：
  - `models.py`：`Layer` 加 `pharma` 成员
  - `config/watchlist.toml`：新增 `[pharma]` 层（thresholds: -6% / -10% / -5%，benchmark `PPH`），10 只股迁过去

### 待启动 — Round 2（Round 1 落地后派单独 Haiku）

#### C. Next-earnings 字段（P1）
- 问题：SKILL.md 要求 `[T-3, T+5]` 事件窗口检查，但 Python pipeline 没有任何 next-earnings 查询。主 agent 只能从 SEC 已发布的 10-Q 标题里推断，无法识别"5 天后要财报"这种 1 周持仓决定性的情境
- 计划：
  - 新建 `sources/earnings_calendar.py`，调 Yahoo `/v10/finance/quoteSummary/{ticker}?modules=calendarEvents`，免费免 key
  - 在 PipelineContext 中携带 `next_earnings_date: date | None`
  - review packet 的 `freshness` 块（或新顶层字段）暴露 `next_earnings_date` 和 `days_to_next_earnings`
  - **先暴露字段，不加硬 cap**，让主 agent 自己决定是否降级——保守起步，留观察空间
  - 数据源失败时给 `None` 而非阻塞 pipeline

#### D. 重新设计 `price_flow` 桶（P0）— ✅ 已完成（commit `a8fc983` + `52476ce`）
- 问题：旧 `score_price_flow(trigger)` 直接 scale 触发器用的 `ten_day_drawdown / twenty_day_drawdown / relative_underperformance`，与触发器**完全是同一组变量**——双重计分，机械地给跌得最惨的票最高分（飞刀拿高分）
- 修复：签名改为 `score_price_flow(security_prices: list[PriceBar])`，与触发器完全正交，改为度量"回撤后的价格行为"。15 分拆 3 个独立组件：
  - **① 企稳（0-6）**：最近 3 日最低收盘是否 ≥ 前 17 日最低（不创新低，+3）；离 20 日低点反弹幅度（`min(3, round(bounce_pct×60))`）
  - **② 短均线收复（0-5）**：收盘站上 5d SMA +2、站上 10d SMA +2、5d SMA 上穿 10d SMA +1
  - **③ 日内收盘强度（0-4）**：最近 5 日 `(收盘-最低)/(最高-最低)` 均值，按 `min(4, round((avg-0.3)×10))` 计分
- 量能组件**暂不加**（fallback 链各源成交量口径不一致，会脏）
- `len(bars) < 21` 时返回 0 + `insufficient_price_history` note
- reviewer 验证：公式逐条核对、正交性确认、basing 票得 13 分 / 飞刀票得 2 分、155/155 测试通过
- 配套：`test_social_rebound.py` 一个 fixture 从 1 根 bar 扩到 25 根真实序列（旧测试靠 `trigger` 间接喂分，新签名需要真实 OHLCV）

### 已完成

#### F. 社交分数 0 vs None 区分（P2）✅ 已完成
- 问题：NRG 类冷门票 0 帖被打 0 分，与"中性观望"的 0 分无法区分
- 修复：
  - `score_social_rebound()` 现在在无社交样本时返回社交桶、`max_score=0` 和 `note="no_social_sample"`；有样本但中性时返回 `max_score=10, score=0`，清晰区分"无数据"和"已知中性"
  - `map_state()` 新增阈值缩放：`candidate_state` 的 3 个分界（65/72/80）基于桶的 `achievable_max / 110` 比例调整，使无社交票（achievable_max=100）按其真实最高分判段，无结构性惩罚
  - 实现曲折：第一版（`b7f27c0`）误将 achievable_max 也传入 base_state 调用，导致所有票的基础阈值都被错误缩放；测试失败后在 `e167fd3` 修正——base_state（0-100 固定）不缩放，仅 candidate_state（社交/情绪等可变桶）缩放
- 涉及文件：`sharing-resources/src/market_sentiment/scoring.py`（`score_social_rebound`、`map_state`）、测试覆盖
- Commits：`b7f27c0`（社交桶逻辑）、`e167fd3`（base_state 回归修复）

#### G. `invalidate_if` / `rerate_if` 改为机器可读（P2）✅ 已完成
- 原问题：主 agent 输出的条件是 LLM 文本，无法在后续日自动求值
- 实现：全链条 5 commit 决策跟踪系统
  - **G1-Schema（`6f3a2b8`）**：`models.py` 新增 `DecisionCondition` dataclass；`decision_schema.py` 定义 6 个可机器读的 metric（`close`, `pct_from_reference`, `close_vs_sma20`, `new_low_20d`, `days_held`, `days_to_earnings`）；SKILL.md 定约：主 agent 每条 Watch/Starter/Add 建议需输出 `data/decisions/<run_date>/<TICKER>.decision.json`，含结构化 `invalidate_conditions[]` 和 `rerate_conditions[]`，并自验证
  - **G2-Storage（`274877f`）**：`storage.py` 新增 `active_decisions` SQLite 表（ticker, decision_date, status, conditions JSON, last_checked, created_at）；status 枚举 `active | invalidated | rerated | expired | superseded`；条件存 JSON blob；自动清理 180 天前非 active 行
  - **G2-Tracker（`51eb947`）**：`decision_tracker.py` 纯 Python 决策求值引擎。`load_decision_files()` 扫决策文件、验证 schema；`evaluate_decision()` 检查有效期（>30 交易日→expired）→invalidate 条件→rerate 条件；price metrics 要求 N 连续交易日满足；零 LLM 调用
  - **G2-Wiring（`1b0f3f7`）**：`pipeline.py` `_track_decisions()` 于每日 run-daily 执行：ingest 新决策文件（已存在的 (ticker, decision_date) 跳过，不复活已 invalidated 决策）→对每条 active 决策用新鲜价格/财报数据求值→更新状态→生成"持仓条件监控"人类报告段。全程防御，决策追踪故障不阻塞 daily
  - 当前决策跟踪日常工作流：
    1. 主 agent 出 Watch/Starter/Add 建议时，写一份结构化 `.decision.json` 件（含可机器读的 invalidate/rerate 条件）
    2. 次日 `run-daily` 启动 pipeline，新决策文件 ingest 进 `active_decisions` 表（已存在跳过，杜绝复活失效决策）
    3. 对每条 active 决策纯 Python 拉最新价格/财报数据，求值条件，零 LLM 上下文消耗
    4. 命中 invalidate/rerate 条件或满 30 交易日 → 状态终结；否则保持 active、更新 last_checked
    5. 结果进人类报告的「持仓条件监控」段
    - **重要澄清**：`status="active"` 表示"建议仍在监控中"，非"已买入"——本项目无自动交易接口，入场仍由用户手动操作。`note` 是给人看的非操作注释，求值只认结构化 `metric / comparator / threshold`
- 涉及文件：`models.py`、`decision_schema.py`（新）、`decision_tracker.py`（新）、`storage.py`、`pipeline.py`、`manual_agent_report.py`、SKILL.md
- Commits：`6f3a2b8`、`274877f`、`51eb947`、`1b0f3f7`

### 已认领但延后

#### E. 回测脚手架（P0，独立工程）
- audit 把"零 backtest"列为单项最大缺口，但定位需要先想清楚：本 skill 是 LLM 主 agent 决策框架，不是自动交易策略。回测能验证的是规则引擎的 trigger frequency 和 bucket → forward return 关系，**不能**验证主 agent 的最终决策
- 计划：新建 `sharing-resources/scripts/backtest_triggers.py`，walk-forward 跑 2 年历史数据，对每个 trigger 日记录 1w/2w/4w forward return，按 ActionState bucket 出命中率分布
- 工作量估算：2-3 天，与主 pipeline 解耦，无回滚风险
- 现状：待用户决定是否启动

### 主动决定不做（audit 提到但 noted）

- **Watchlist 静态/survivorship**：本 skill 定位是 pullback research（用户拿着标的来用），不是 screening。screening 是 `$us-smallmid-dislocation` 那个 skill 的事
- **作者 / bot 过滤**（账号 age / karma）：数据采集层增强，价值真实但 ROI 低
- **SEC 重述防 look-ahead**（取 earliest-filed 而非 latest）：真实问题但发生频率低，1w-1m 投资者影响有限
- **Macro regime 调节评分**：建议把 macro 信号留给 LLM Layer 2 自行权衡，不嵌入规则引擎以免越权
- **HK 微观结构**（交易时段、HKD 标准化）：独立审计议题，与本 skill 主流程解耦

### 量化视角下系统现存优点（不要打破）

- **Hard veto**（破产/欺诈/营收结构性断裂）客观、绑 SEC 文件，正确覆盖一切其他分数（`scoring.py:263-305`）
- **`cap_state_if_data_insufficient`** 在 SEC 或新鲜价格缺失时把 Add/Starter 降到 Watch，保守且测试覆盖充分
- **`apply_social_guardrail`** 阻止稀疏社交数据单独把 Reject 升级，防止社交噪声决定 margin（`scoring.py:322-329`）
- **Layer 1 / Layer 2 分离**：规则引擎纯 advisory，主 agent 必须先独立阅读 Layer 2 raw evidence 再回头看 bucket scores，正确地把规则引擎定位为 sanity check
- **Freshness block** 让 stale data 公开可见，主 agent 无法"无意中"把陈旧数字当 current

---

*本节基于 commit `6bde150`，结合 Sonnet 中长线量化视角审计报告（2026-05-15）整理。F 与 G 更新基于 commit `b7f27c0`、`e167fd3`、`6f3a2b8`、`274877f`、`51eb947`、`1b0f3f7`。*
