# PrepDojo 内测快速开始（给朋友的三行说明）

> 适用：macOS / Linux（Windows 请用 WSL）。只需要 Python 3.10+（最好再有 clang++，没有也能刷 Python 题）。

```bash
# 1. 解压后进入目录，装依赖
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip==26.2.1
.venv/bin/python -m pip install -r requirements.txt

# 2. 启动（首次会自动导入 20 道种子题）
.venv/bin/python -m prepdojo.cli serve

# 3. 浏览器打开
#    http://localhost:8686
```

macOS 完成第 1 步后也可双击 `启动PrepDojo.command`；脚本只有在本地健康检查通过后才会打开浏览器。

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
# 真正接入（调用 LLM；费用取决于资料量和服务商）
.venv/bin/python -m prepdojo.cli ingest ~/你的八股目录
```

接入后八股页就有「学习 → 测验 → 打分 → 追问」闭环。原始资料和数据库保存在本机 `data/` 且不会进入 Git；启用 AI 时，待处理文本和相关上下文会发送到你配置的 LLM 服务商。

## 遇到问题

把终端报错截图发给我就行。判题结果不对、AI 讲得不对、UI 不顺手——都欢迎吐槽，内测就是要找问题。

## 想搭一个给实验室/团队共用的版本？

不要直接监听 `0.0.0.0`。多用户部署还必须配置 Docker 判题、HTTPS、Secure Cookie、Host 白名单、配额、私有权限和备份。请完整执行 [多用户 HTTPS 部署手册](deploy/README-server.md)，不要把本地开发启动方式直接暴露到公网。
