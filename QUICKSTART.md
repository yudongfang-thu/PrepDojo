# PrepDojo 内测快速开始（给朋友的三行说明）

> 适用：macOS / Linux（Windows 请用 WSL）。只需要 Python 3.10+（最好再有 clang++，没有也能刷 Python 题）。

```bash
# 1. 解压后进入目录，装依赖（约 30 秒）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 启动（首次会自动导入 20 道种子题）
.venv/bin/python -m prepdojo.cli serve

# 3. 浏览器打开
#    http://localhost:8686
```

## 不配 AI 也能玩

- ✅ 刷 20 道代码题（本地沙箱判题：AC/WA/TLE/RE）
- ✅ 错题本（没 AC 的自动进，AC 自动出）+ 错题重练
- ✅ 学习模式浏览种子题卡（本包不含八股库，可自己接入，见下）

## 想要 AI 功能（点评 / 讲题教练 / 八股打分）

启动一次后会生成 `data/config.yaml`，打开填入你的 DeepSeek key（https://platform.deepseek.com 申请）：

```yaml
llm:
  api_key: "sk-你的key"
```

重启 serve 即可。然后试试：随便开一道题 → 点「🧑‍🏫 问 AI 教练」→ 输入"我的代码哪里有边界问题，跑个输入验证一下"——AI 会真的在沙箱里跑代码验证给你看。

## 想接入自己的八股资料（可选）

```bash
# 先看抽取效果（不花一分钱）
.venv/bin/python -m prepdojo.cli ingest ~/你的八股目录 --dry-run
# 真正接入（调用 LLM，92 个 PDF 约 ¥4）
.venv/bin/python -m prepdojo.cli ingest ~/你的八股目录
```

接入后八股页就有「学习 → 测验 → 打分 → 追问」闭环。注意：你的资料只留在你本机 data/ 目录，不会进任何仓库。

## 遇到问题

把终端报错截图发给我就行。判题结果不对、AI 讲得不对、UI 不顺手——都欢迎吐槽，内测就是要找问题。
