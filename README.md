# PrepDojo · 秋招刷题小助手

> **本地优先（local-first）的 AI 刷题工具**：代码题本地沙箱判题 + AI 点评；八股由 AI 出题、打分、追问；把你自己的知识目录（PDF / Markdown）一键结构化为题卡。
>
> 本仓库由 [AI-Literature-Analyzer](legacy/)（2025 年的 AI 文献分析工具，29 ⭐）演进而来——同一个「AI 驱动的学习工具」思路，从「读文献」进化为「备战面试」。旧代码保留在 [`legacy/`](legacy/)。

## 为什么是 local-first

- **你的知识库属于你**：八股 PDF、笔记、练习记录全部留在本机 `data/`（已被 `.gitignore` 排除），本仓库**永不包含第三方内容**。
- **判题不依赖任何服务**：本地沙箱执行，断网可用。
- **AI 模块可插拔**：默认 DeepSeek API；把 `base_url` 指向 [Ollama](https://ollama.com) 即可完全离线。

## 快速开始

```bash
git clone https://github.com/yudongfang-thu/PrepDojo.git
cd PrepDojo
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 导入 20 道内置代码题（题面与用例全部自写）
.venv/bin/python -m prepdojo.cli seed

# 2) 启动本地 Web UI
.venv/bin/python -m prepdojo.cli serve
# 打开 http://localhost:8686 —— 判题已可用
```

### 接入你自己的八股知识（可选，需 LLM）

```bash
# 先在 data/config.yaml 填入 api_key（见下），然后：
.venv/bin/python -m prepdojo.cli ingest ~/path/to/你的八股目录
# 不想调 API？先 dry-run 看抽取效果：
.venv/bin/python -m prepdojo.cli ingest ~/path/to/dir --dry-run
```

支持递归扫描 `.pdf`（需文字层；扫描图会提示跳过）/ `.md` / `.txt`，
按文件 SHA-256 增量去重，重复执行只处理新文件。

### LLM 配置（三选一，编辑 `data/config.yaml`）

| 方案 | base_url | model |
|---|---|---|
| DeepSeek 官方 | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` |
| Ollama 本地（完全离线） | `http://localhost:11434/v1` | `qwen2.5:7b` 等 |

也可用环境变量 `PREPDOJO_API_KEY` / `PREPDOJO_BASE_URL` / `PREPDOJO_MODEL`。

## 功能

### 代码题（判题 = 事实，AI = 教练）

- 本地沙箱：`subprocess` + POSIX `rlimit`（CPU / 文件 / 核转储；Linux 另有地址空间与进程数限制）+ wall-clock 超时 + 输出截断。
- 判定五态：**AC / WA / TLE / MLE / RE**（另加编译错误 CE）。**对错永远以测试用例为准，LLM 不参与判定**——这是刻意设计：模型判对错不可靠，但做教练很称职。
- AI 点评：思路、复杂度、边界条件、面试官可能的追问。
- 语言：Python 3 与 C++ 17（Apple clang 也支持 `#include <bits/stdc++.h>`，见 `prepdojo/cpp_include/`）。
- 内置 20 道种子题（数组/链表/栈队列/二叉树/图/DP/贪心/设计），题面与用例为仓库原创；期望输出由参考解实际运行生成并经双语言交叉验证（`scripts/gen_seeds.py`）。

### 八股陪练（出题 → 作答 → 打分 → 追问）

- 题卡 schema：问题 / 答案要点 / 追问 / 标签 / 难度 / 来源引用。
- 练习时先不展示答案要点，AI 打分后才给出对照（防偷看）。
- 评分标准写死在 prompt：宁可偏严；按要点覆盖度给分并逐条点评。
- 默认排除最近 3 天练过的卡，避免短期重复。

### 知识接入流水线（本项目的核心工程点）

```
用户目录（任意结构的 PDF/MD/TXT）
  → 抽取（pypdf；扫描图自动识别并跳过）
  → 分块（Q&A 边界启发式：编号问题 / 问号行 / 滑窗兜底）
  → LLM 结构化（JSON Schema 约束输出 + 解析失败重试）
  → SQLite（data/prepdojo.db，本地私产）
```

实测：92 个文字型 PDF（505 页 / 51 万字符）→ 790 个 Q&A 块，全量结构化约 100 万 token（DeepSeek 约 ¥4）。

## 隐私与版权承诺

1. **分发红线**：仓库只含代码与自写种子内容；`data/` 目录被 `.gitignore` 排除，请勿将第三方题库 / 八股资料提交进来。
2. **API 边界（诚实说明）**：ingest 与 AI 点评会把**所给文本**发送到你配置的 LLM 服务商。「不上传」指不公开分发、不入仓库，不是「不经网络」。需要完全离线请用 Ollama。
3. **不搬运题面**：不爬取、不内置 LeetCode / 牛客等平台的题目描述。想练平台原题，请在平台作答后把代码贴回来判题点评（BYO 模式）。

## 项目结构

```
prepdojo/
├── cli.py           # seed / ingest / quiz / serve / stats
├── config.py        # 环境变量 > data/config.yaml > 默认值
├── db.py            # SQLite：题卡 / 代码题 / 提交 / 练习记录
├── extract.py       # PDF/MD 抽取 + Q&A 分块
├── ingest.py        # LLM 结构化流水线（增量 / dry-run）
├── llm.py           # OpenAI 兼容客户端（DeepSeek/硅基流动/Ollama）
├── judge.py         # 本地判题沙箱（五态）
├── review.py        # 代码 AI 点评
├── quiz.py          # 八股打分 / 追问
└── web/             # FastAPI + 无构建静态前端（localhost:8686）
seeds/coding/        # 20 道自写种子题（JSON）
scripts/gen_seeds.py # 种子生成：参考解运行生成期望输出 + 双语言交叉验证
tests/               # pytest（判题五态 / 抽取 / 持久化 / Web API）
legacy/              # 前身项目 AI-Literature-Analyzer（保留存档）
```

## 开发

```bash
.venv/bin/python -m pytest tests/ -q        # 21 项测试
.venv/bin/python scripts/gen_seeds.py       # 重新生成并验证种子题
```

## License

MIT
