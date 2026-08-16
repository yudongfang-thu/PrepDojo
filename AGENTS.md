# AGENTS.md — PrepDojo 工作指引

本地优先（local-first）的 AI 刷题工具：本地沙箱判题（AC/WA/TLE/MLE/RE/CE）+ AI 讲题教练（沙箱作为可调用工具）+ 八股 AI 面试官 + 知识目录结构化流水线。前身项目存于 `legacy/`（只读存档，勿改）。macOS/Linux only（判题用 POSIX rlimit），Windows 需 WSL。

## 常用命令

```bash
.venv/bin/python -m pytest tests/ -q          # 全部测试（tmp_path 隔离，无需 LLM/网络）
.venv/bin/python -m pytest tests/test_judge.py -q   # 单文件
.venv/bin/python -m prepdojo.cli serve        # Web UI: http://localhost:8686
.venv/bin/python -m prepdojo.cli seed         # 导入种子题（serve 首次启动也会自动导入）
.venv/bin/python -m prepdojo.cli ingest <dir> --dry-run  # 知识接入预览（不调 LLM）
.venv/bin/python scripts/gen_seeds.py         # 重新生成种子题（期望输出由参考解运行生成 + 双语言交叉验证）
```

- 纯 Python 项目（3.10+），无 node/npm/构建步骤；依赖装在 `.venv`（fastapi/uvicorn/pypdf/httpx/pyyaml/pydantic/pytest）。
- 不是安装包（无 pyproject）：测试文件各自 `sys.path.insert` 仓库根目录，没有 conftest.py。
- 文档/UI 字符串/代码注释均用中文，与现有风格保持一致。

## 红线（违反即事故）

1. **`data/` 永不入库**（.gitignore 已排除）：内含用户知识库、`config.yaml`（API key）、`prepdojo.db`。绝不提交、绝不负责任删除。
2. **仓库不含第三方内容**：不爬取/内置 LeetCode、牛客等平台题面；种子题题面与用例全部自写（`seeds/coding/`，改题请走 `scripts/gen_seeds.py`）。
3. **权威判定只来自沙箱**：AI 的结论永远不能覆盖 `judge.py` 的沙箱结果——这是产品设计原则，不是实现细节。

## 架构

```
prepdojo/
├── cli.py        # 入口：seed / ingest / quiz / serve / stats
├── config.py     # 配置优先级：环境变量 > data/config.yaml > DEFAULTS
├── db.py         # SQLite 层（sqlite3 + threading.Lock）
├── judge.py      # 判题沙箱：subprocess + rlimit + wall-clock 超时
├── llm.py        # OpenAI 兼容客户端（默认 DeepSeek；function calling + 流式）
├── chat.py       # AI 教练/判题工具循环（MAX_TOOL_ROUNDS=8）
├── ingest.py     # 知识结构化流水线（SHA-256 增量去重、dry-run）
├── extract.py    # PDF/MD/TXT 抽取 + Q&A 分块
├── quiz.py / review.py / problem_gen.py / seed_loader.py
└── web/server.py # FastAPI（localhost 单用户）+ web/static/ 无构建前端
```

- **Web 层**：`web/server.py` 是唯一 HTTP 入口；前端是 `web/static/` 下的原生 HTML/JS，CodeMirror 已本地打包在 `web/static/vendor/`（**禁用 CDN**，须离线可用）。静态资源走 no-cache 中间件（防浏览器旧版前端）。
- **LLM 分层**：所有 AI 功能必须优雅处理 `LLMNotConfigured` / `cfg.llm_ready == False`（无 key 时判题可用，AI 功能返回明确提示）。
- **DB 模式**：连接与锁封装在 `DB` 类内；取 `scalar()` 值须在锁内完成并做 None 防御（历史 bug，见 commit 80dfaa8）。

## 已知坑

- Apple clang 无 `bits/stdc++.h`：C++ 编译时用 `prepdojo/cpp_include/bits/` 下的本地副本。
- 沙箱威胁模型是"防事故"（死循环/爆内存/写盘）而非"防恶意"，不要试图把它加固成安全沙箱。
- 前端无构建无版本号，改动 `web/static/` 后浏览器可能缓存旧版——依赖 no-cache 中间件，勿移除。
- 修改配置项时同步改三处：`config.py` 的 `DEFAULTS`、`example_config_yaml()`、`data/config.yaml` 加载逻辑（仅接受已知 key）。
- macOS 双击启动脚本 `启动PrepDojo.command` 硬编码端口 8686 与 `data/serve.log`，改端口需一并处理。
