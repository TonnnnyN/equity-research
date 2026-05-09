# Market Sentiment Skills

这是一个多 Skill 项目，不再把根目录当成单一 Agent Skill。

现在有两个并列 Skill：

- `skills/market-sentiment-research/`：日终市场情绪、单票/短名单 selloff 复核、pipeline 报告与 review packets。
- `skills/us-smallmid-dislocation/`：U.S. small/mid-cap 宽表候选筛选，输出 `Pass / Watchlist / Investigate`。

能共用的运行引擎、测试、密钥模板、共享脚本和项目文档放在 `sharing-resources/`。

## 项目结构

```text
skills/
  market-sentiment-research/
    SKILL.md
    agents/openai.yaml
    docs/
    references/
    scripts/
    defaults/
  us-smallmid-dislocation/
    SKILL.md
    agents/openai.yaml
    docs/
    references/
    scripts/
    defaults/
sharing-resources/
  docs/                         # 项目级架构、设计、操作说明
  references/                   # 共享配置、密钥、运行产物、CLI 参考
  scripts/                      # 两个 Skill 都可能用到的脚本
  src/market_sentiment/         # Python runtime engine
  tests/                        # 回归测试
  examples/                     # 示例文件
  secrets/                      # 本地密钥目录，真实文件不提交
config/watchlist.toml           # 本地运行配置
data/                           # 运行生成数据，不提交
pyproject.toml                  # Python package 配置
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
sharing-resources/secrets/market_sentiment.secrets.sh
```

使用前加载：

```bash
source sharing-resources/secrets/market_sentiment.secrets.sh
```

模板在：

```bash
sharing-resources/secrets/market_sentiment.secrets.example.sh
```

`sharing-resources/secrets/` 下真实密钥文件已被 `.gitignore` 忽略。

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
python3 skills/market-sentiment-research/scripts/update_price_cache.py
```

筛选准备好的 U.S. small/mid-cap CSV：

```bash
python3 skills/us-smallmid-dislocation/scripts/screen_candidates.py input.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

脱敏本地 JSON 运行产物：

```bash
python3 sharing-resources/scripts/redact_sensitive_json.py data
```

脚本详情分别看每个 Skill 自己的 `references/script_reference.md`，共享运行脚本看 `sharing-resources/references/runtime_and_shared_scripts.md`。

## 测试

```bash
python3 -m pytest sharing-resources/tests
```

## 更多文档

- `skills/market-sentiment-research/SKILL.md`
- `skills/us-smallmid-dislocation/SKILL.md`
- `sharing-resources/docs/architecture.md`
- `sharing-resources/docs/design_notes.md`
- `sharing-resources/docs/local_operations.md`
- `sharing-resources/references/configuration_and_secrets.md`
- `sharing-resources/references/data_and_outputs.md`
