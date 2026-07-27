# 部署到 GitHub Actions（3 步）

## 1. 把 tracker push 到一个独立 GitHub 仓库

```bash
cd /Users/ayao/code/github_project/research_workflow/tools/arxiv-tracker

# 初始化为一个独立 repo（不要直接 push 整个 github_project，里面有 yosys/OpenSTA 等无关大目录）
git init -b main
git add .
git commit -m "init: arxiv+DBLP daily tracker"

# 在 GitHub 上手动新建一个空仓库（例如 arxiv-tracker），然后：
git remote add origin git@github.com:<your-username>/arxiv-tracker.git
git push -u origin main
```

## 2. 在仓库 Settings → Secrets and variables → Actions 添加三条 Secret

| Name | Value |
|---|---|
| `OPENAI_API_KEY` | 你的代理 key |
| `OPENAI_BASE_URL` | 你的 OpenAI 兼容端点 URL |
| `OPENAI_MODEL` | （可选）默认填 `gpt-5.4-mini` |

⚠ Secrets 不会进 git 历史，比写 `.env` 更安全。

## 3. 启用 Actions 并手动试跑一次

- GitHub 仓库页面 → **Actions** 标签 → 第一次会让你"enable workflows"，点同意
- 左侧选择 **Daily Paper Tracker** → 右侧 **Run workflow** 按钮 → 选 main 分支 → Run
- 等 ~5 分钟，跑通后查看 `digests/<date>.md` 是否被自动 commit

跑通后，之后每天 UTC 02:10（北京时间 10:10）自动触发。

---

## 常见问题

**Q: DBLP 那批查询会不会被 GitHub runner IP 限速？**
A: 不会，因为脚本里有 31 秒节流。GHA 的 IP 是全新的、没历史 ban 记录。但 GHA 单 job 上限 6 小时，4 个 DBLP 查询总共 ~2.5 分钟，绝对够。

**Q: 想换/增加关键词怎么办？**
A: 直接编辑 `keywords.yaml`，commit 回 main。下一次 cron 触发就用新关键词。

**Q: 想存 PDF 全文吗？**
A: 工作流里我故意 `--no-pdf` 了。PDF 长期会撑爆 git 仓库。如果要存：
- 短期：去掉 `--no-pdf`，但加 `git lfs track "papers/*.pdf"`
- 长期：上传到 S3/R2/oss，仓库里只存 metadata

**Q: cron 没触发怎么办？**
A: GitHub 的 schedule trigger 在仓库**两周内没有任何 commit**时会自动停。每月随便 push 一次空 commit 保活即可：`git commit --allow-empty -m "keepalive" && git push`
