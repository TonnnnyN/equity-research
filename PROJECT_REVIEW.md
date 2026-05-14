# 市场情绪感知项目 —— 深度评审报告

> 初版生成日期：2026-05-12
> 最新更新：2026-05-14（0.9 新增：DeepSeek v4-flash 真验证 + 缓存盘点 + P0 收尾路线）
> 当前代码版本：`9dd26b2` + SSL/v4-flash 补丁（本轮 commit）

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

## 一、项目概览

本项目是一套**美股回调复核系统**，用于在市场收盘后检测持仓或候选股是否出现相对同行/基准的异常跌幅，若触发阈值则自动采集多维度证据（官方披露、财报数据、宏观指标、社交情绪、期权数据），经规则引擎打分后输出可执行的投资决策建议。

### 两个 Skill 定位

| Skill | 目录 | 定位 | 输出档位 |
|---|---|---|---|
| `market-sentiment-research` | `skills/market-sentiment-research/` | **单股深度复核**：针对已触发异常跌幅的特定股票，完整跑 5 条证据 lane，输出带 `invalidate_if` / `rerate_if` 的行动建议 | Reject / Watch / Starter / Add / Exit（5 档） |
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
│   ├── market-sentiment-research/
│   │   ├── SKILL.md                   # Skill 说明（Claude Code 读取）
│   │   ├── defaults/targets.toml      # 默认研究对象配置
│   │   ├── references/                # Skill 级别参考文档
│   │   └── scripts/
│   │       └── update_price_cache.py  # 用 Yahoo Chart API 拉取日线缓存（371 行，无需 key）
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
        ├── cli.py                     # CLI 命令：init-db / preflight / run-daily / show-report / cleanup-data
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
