# Market Sentiment Agent Skill

这是一个本地优先的 Agent Skill 项目，用来做美股日终市场情绪监督、异常回撤复核、候选股票筛选，以及相关 Python pipeline 的维护。

根目录本身就是主 Skill。`SKILL.md` 是 Agent 入口，`src/market_sentiment/` 是运行引擎，`scripts/`、`references/`、`docs/`、`defaults/` 是随 Skill 一起使用的资源。

## 结构

```text
SKILL.md                    # Agent 主导航
agents/openai.yaml          # Skill UI 元数据
docs/                       # 设计思路、架构、操作说明
references/                 # 工作流、脚本、配置、输出格式参考
scripts/                    # 可执行辅助脚本
defaults/                   # 可随 Skill 携带的默认配置
secrets/                    # 本地密钥目录，真实文件不提交
src/market_sentiment/       # Python runtime engine
config/watchlist.toml       # 本地运行配置
tests/                      # 测试
data/                       # 运行生成数据，不提交
legacy_skills/              # 旧独立 Skill 归档
```

## 安装

```bash
python3 -m pip install -e .
```

可选 X provider：

```bash
python3 -m pip install -e ".[social]"
```

## 密钥

真实 API key 和账号凭据放在：

```bash
secrets/market_sentiment.secrets.sh
```

使用前加载：

```bash
source secrets/market_sentiment.secrets.sh
```

模板在：

```bash
secrets/market_sentiment.secrets.example.sh
```

`secrets/` 下真实密钥文件已被 `.gitignore` 忽略。

## 日终运行

```bash
market-sentiment --config config/watchlist.toml preflight
market-sentiment --config config/watchlist.toml run-daily
```

指定日期：

```bash
market-sentiment --config config/watchlist.toml run-daily --date 2026-03-26
```

常见输出：

```text
data/reports/<date>/report.md
data/reports/<date>/report.json
data/reports/<date>/review_queue.md
data/reports/<date>/review_packets/
data/reports/<date>/manual_agent_report.zh.md
```

## 辅助脚本

刷新目标池价格缓存：

```bash
python3 scripts/update_price_cache.py
```

筛选准备好的 U.S. small/mid-cap CSV：

```bash
python3 scripts/screen_candidates.py input.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

脱敏本地 JSON 运行产物：

```bash
python3 scripts/redact_sensitive_json.py data
```

脚本详情看 `references/script_detailed_reference.md`。

## 测试

```bash
python3 -m pytest
```

## 更多文档

- `docs/architecture.md`
- `docs/design_notes.md`
- `docs/local_operations.md`
- `references/research_workflows.md`
- `references/target_pool.md`
- `references/configuration_and_secrets.md`
- `references/data_and_outputs.md`
