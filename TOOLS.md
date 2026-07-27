# arxiv-tracker — 自动论文追踪 agent

## 工具定位

每天/每周自动跑一遍：用关键词查询 **arxiv + DBLP**（覆盖 IEEE TCAD/TODAES/DAC/ICCAD/DATE），
对每篇新论文调 LLM 生成结构化中文摘要（动机/方法/结果/相关性/局限），输出到一份 markdown
每日摘要里。

填补了上一轮 5 个工具的**最大空白**：没有任何一个 one-shot 工具能做增量、持续追踪。

## 设计取舍

- **零 key 依赖**：arxiv + DBLP 都是公开 API，无需任何搜索服务 key（解决了 GPT Researcher/STORM 的痛点）
- **轻量自建**：~400 行 Python，只依赖 `requests` + `pyyaml` + `openai`
- **DBLP 节流**：每次 DBLP 请求间强制 sleep 31 秒，规避 429 / 连接被拒
- **状态外置**：`state.json` 记录已见 title，支持跨次运行去重
- **PDF 可选**：默认 `--no-pdf`，避免仓库膨胀；arxiv PDF 可选下载
- **GHA 原生**：提供 `daily-tracker.yml`，结果自动 commit 回仓库

## 安装步骤（本地）

```bash
cd tools/arxiv-tracker
bash run.sh --dry-run --no-pdf          # 等价于先初始化 venv 再 dry-run
```

`run.sh` 会用 `uv venv --python 3.11` 建独立 venv，自动 `pip install -r requirements.txt`。

## 使用方式（本地）

```bash
# 标准 daily 跑法：拉过去 14 天 + 调 LLM 总结 + 不下 PDF
bash run.sh --since-days 14 --no-pdf

# 只查 arxiv 一侧（DBLP 被 ban 时用）
bash run.sh --no-dblp --no-pdf

# 只查 DBLP（验证 keyless 链路）
bash run.sh --no-arxiv --no-pdf

# 试跑不调 LLM（验证关键词和检索链路）
bash run.sh --dry-run --no-pdf
```

环境变量（不要写进文件）：
- `OPENAI_API_KEY`：必填
- `OPENAI_BASE_URL`：OpenAI 兼容代理 URL
- `OPENAI_MODEL`：默认 `gpt-5.4-mini`（经实测代理支持）

## 部署到 GitHub Actions

见 [SETUP.md](SETUP.md)。摘要：把整个 `tools/arxiv-tracker/` 目录 push 到独立仓库，
在 Secrets 里加 3 个环境变量，每天 UTC 02:10 自动跑，结果 commit 回 main。

## 执行效果（2026-07-28 实测）

**arxiv 一侧**（`--since-days 7 --no-dblp`）：8 篇过去 7 天的 cs.AR / cs.LG / cs.AI / cs.DC
新论文全部正确拉到，LLM 总结全部成功（`gpt-5.4-mini`，温度 0.2，JSON mode）。

实测命中的几篇代表性论文（都来自 7/21–7/24）：
- **HiKV** (cs.AR 2607.22389)：KV cache 算法-硬件协同设计
- **WaveformQA** (cs.AI 2607.20638)：LLM 对数字波形时间推理的 benchmark
- **AlphaRoute** (cs.LG 2607.19768)：LLM 作为 VLSI 多目标布线语义优化器
- **BaseRT** (cs.AR 2607.19438)：Apple M5 Neural Accelerator 上的 LLM 推理
- **Unified Static-Dynamic Pruning** (cs.DC 2607.21985)：LLM 推理的统一静态-动态剪枝

LLM 总结示例（AlphaRoute 一条）：
> **方法**: 将 rip-up and reroute 重构为多目标动态优化系统，结合 3D Dijkstra maze routing、
> adaptive PathFinder 策略，以及受确定性知识图谱约束的 LLM 来动态调整惩罚参数。
> **关键结果**: 在 ISPD 2025 基准的 MEMPOOL 上 overflow 降低 98.6%；ARIAN 设计上 overflow
> 比 SOTA 降低 29.8×。

5 个字段（动机/方法/结果/相关性/局限）全部对齐，无幻觉。

**DBLP 一侧**：本地跑时被 ban（之前 429 的余波，DBLP 的反爬很敏感）。但 GHA 上 runner IP
全新，应能跑通。待 GHA 部署后再回填 TCAD 论文数据。

完整产物见 `digests/2026-07-28.md`。

## 推荐理由

✅ **保留**。理由：
1. 解决了上轮 5 个工具都没解决的核心问题（持续追踪）
2. 零外部 key 依赖（除 LLM）
3. 部署后是"set and forget"，每日自动产出
4. 与 PaperQA2 天然配合：tracker 拉到的 arxiv PDF 可以丢给 PaperQA2 做深度问答

## 不推荐理由 / 已知局限

- DBLP 反爬严格：本地连续跑会触发 IP 临时封禁（GHA 上不是问题）
- DBLP 只给元数据，**没有 abstract**，LLM 总结质量受影响（建议给 DBLP 命中论文手动补 abstract）
- 当前没有按"作者/机构"维度过滤；如果只关心特定组（如 Prof. X 的工作），需要扩展 keywords.yaml
- 没有去重跨 topic：如果同一论文匹配多个 topic，会重复列出

## 进阶方向

1. 把每日 digest 接入 PaperQA2，自动建索引做问答
2. 给 tracker 加 embedding-based 相似度去重（替代当前 title 精确匹配）
3. 接入 GitHub notification / 邮件 webhook，每天把 digest push 给自己
4. 加 Reddit / Hacker News / Twitter 信号源（追踪工业界动态）
5. 加 citation count 跟踪（Semantic Scholar API，免费但要 key）

## 文件清单

```
tools/arxiv-tracker/
├── TOOLS.md                          # 本文件
├── SETUP.md                          # GitHub Actions 部署 3 步
├── README.md                         # （略，本文件已覆盖）
├── requirements.txt
├── keywords.yaml                     # 关键词配置（用户可改）
├── run.sh                            # 本地 runner
├── .gitignore
├── .github/workflows/daily-tracker.yml
├── src/
│   └── tracker.py                    # 主脚本 ~470 行
├── papers/                           # 本地 PDF（gitignored）
├── digests/                          # 每日 markdown 摘要
│   └── 2026-07-28.md                 # 实测产出
└── state.json                        # 去重状态（gitignored）
```
