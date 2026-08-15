# AI Literature Analyzer

🤖 **AI驱动的学术文献分析系统** - 读文献偷懒工具，使用大语言模型对学术论文进行分析

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 🌟 项目特色

- 🧠 **AI驱动分析**: 使用DeepSeek-R1等先进大语言模型进行深度文献分析
- 📊 **结构化输出**: 生成标准化的分析报告和方法卡片
- ⚙️ **高度可配置**: 支持自定义API、模型、分析提示词等
- 📁 **模块化设计**: 清晰的代码结构，易于扩展和维护
- 🔄 **批量处理**: 支持批量分析大量文献
- 📈 **进度监控**: 实时监控分析进度

## 📋 目录结构

```
AI-Literature-Analyzer/
├── main.py                 # 主程序入口
├── requirements.txt        # Python依赖
├── config/
│   └── config.yaml        # 主配置文件
├── src/
│   ├── core/              # 核心模块
│   │   ├── config_manager.py  # 配置管理
│   │   └── analyzer.py        # AI分析器
│   └── utils/             # 工具模块
│       └── progress_monitor.py # 进度监控
├── prompts/
│   ├── analysis_template.txt      # 分析提示词模板
│   ├── method_card_template.txt   # 方法卡片模板
│   └── custom_template_example.txt # 自定义模板示例
├── data/
│   ├── input/             # PDF文献输入目录
│   └── output/            # 分析结果输出目录
│       ├── summaries/     # 深度分析报告
│       ├── method_cards/  # 方法卡片
│       └── batch_reports/ # 批量分析报告
├── docs/                  # 文档目录
└── examples/              # 示例文件
```

## 🚀 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/yudongfang-thu/AI-Literature-Analyzer.git
cd AI-Literature-Analyzer

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置设置

⚠️ **重要安全提示**：请妥善保管您的API密钥，不要提交到代码仓库！

编辑 `config/config.yaml` 文件：

```yaml
api:
  # 设置您的API密钥 - 请替换为您的真实API密钥
  api_key: "your-api-key-here"
  
  # 选择模型 (免费版（硅基流动的赠送额度可用）: deepseek-ai/DeepSeek-R1, 付费版: Pro/deepseek-ai/DeepSeek-R1)
  model: "Pro/deepseek-ai/DeepSeek-R1"
```

💡 **安全建议**：
- 使用环境变量：`export API_KEY="your-key"` 然后在配置中使用 `${API_KEY}`
- 或者创建 `config/my_config.yaml` 并使用 `--config` 参数指定

### 3. 添加文献

将PDF文献文件放入 `data/input/` 目录：

```bash
cp your-papers/*.pdf data/input/
```

### 4. 开始分析

```bash
# 测试单篇论文分析
python main.py --test

# 批量分析所有论文
python main.py --analyze

# 分析前5篇论文
python main.py --analyze --limit 5
```

## 📖 详细使用指南

### 命令行选项

| 选项 | 说明 | 示例 |
|------|------|------|
| `--test` | 测试模式，分析单篇论文 | `python main.py --test` |
| `--analyze` | 批量分析模式 | `python main.py --analyze` |
| `--limit N` | 限制分析数量 | `python main.py --analyze --limit 10` |
| `--progress` | 检查分析进度 | `python main.py --progress` |
| `--monitor` | 持续监控进度 | `python main.py --monitor` |
| `--config` | 指定配置文件 | `python main.py --config my_config.yaml` |
| `--verbose` | 详细输出模式 | `python main.py --analyze --verbose` |

### 配置文件详解

#### API配置
```yaml
api:
  api_key: "your-api-key"           # API密钥
  base_url: "https://api.siliconflow.cn/v1"  # API基础URL
  model: "Pro/deepseek-ai/DeepSeek-R1"       # 使用的模型
  timeout: 180                      # 超时时间(秒)
  max_retries: 3                   # 最大重试次数
  temperature: 0.3                 # 生成温度
  max_tokens: 3000                 # 最大输出长度
```

#### 路径配置
```yaml
paths:
  input_dir: "data/input"          # PDF输入目录
  output_dir: "data/output"        # 结果输出目录
  summaries_dir: "data/output/summaries"      # 分析报告目录
  method_cards_dir: "data/output/method_cards" # 方法卡片目录
```

#### 处理配置
```yaml
processing:
  max_text_length: 4000           # 发送给AI的最大文本长度
  extract_pages: 10               # 最多提取的PDF页数
  skip_analyzed: true             # 跳过已分析的文件
```

### 自定义分析提示词

您可以自定义分析提示词来适应不同的研究领域：

1. **复制模板**：
   ```bash
   cp prompts/custom_template_example.txt prompts/my_template.txt
   ```

2. **编辑模板**：
   根据您的需求修改分析结构和要求

3. **更新配置**：
   ```yaml
   prompts:
     analysis_template: "prompts/my_template.txt"
   ```

### 分析输出说明

#### 深度分析报告 (`data/output/summaries/`)
- 包含8个维度的深度分析
- 论文基本信息、技术分类、核心贡献
- 技术方法、实验验证、质量评估
- 优势局限性、学习价值建议

#### 方法卡片 (`data/output/method_cards/`)
- 简洁的方法总结
- 核心技术、性能表现、使用建议
- 便于快速查阅和比较

#### 批量报告 (`data/output/batch_reports/`)
- 批量分析统计信息
- 成功/失败详情
- 系统配置信息

## 🛠️ 高级用法

### 1. 多领域配置

为不同研究领域创建专门的配置：

```bash
# 复制基础配置
cp config/config.yaml config/nlp_config.yaml

# 修改领域特定设置
# 编辑 nlp_config.yaml，调整 domain_keywords 等配置

# 使用专门配置
python main.py --config config/nlp_config.yaml --analyze
```

### 2. 并行处理

对于大量文献，可以分批并行处理：

```bash
# 分批处理
python main.py --analyze --limit 10  # 第一批
# 等待完成后
python main.py --analyze --limit 10  # 第二批
```

### 3. 结果后处理

```python
# 示例：统计分析结果
import json
from pathlib import Path

summaries_dir = Path("data/output/summaries")
analysis_files = list(summaries_dir.glob("*_ai_analysis.md"))

print(f"总共分析了 {len(analysis_files)} 篇论文")
```

## 🔧 常见问题

### Q: API调用超时怎么办？
A: 可以在配置文件中增加超时时间和重试次数：
```yaml
api:
  timeout: 300  # 增加到5分钟
  max_retries: 5  # 增加重试次数
```

### Q: 如何跳过已分析的文件？
A: 在配置文件中设置：
```yaml
processing:
  skip_analyzed: true
```

### Q: 如何修改分析输出格式？
A: 编辑 `prompts/analysis_template.txt` 文件，调整分析结构和要求。

### Q: 支持哪些PDF格式？
A: 支持标准的文本PDF，不支持扫描版或图片PDF。建议使用OCR工具先转换。

### Q: 如何处理大文件？
A: 可以在配置中限制提取页数：
```yaml
processing:
  extract_pages: 5  # 只提取前5页
```

## 📊 性能优化

### 1. API使用优化
- 使用付费版模型获得更快响应
- 合理设置重试参数
- 避免过长的输入文本

### 2. 批量处理优化
- 分批处理大量文件
- 启用跳过已分析功能
- 监控API使用配额

### 3. 存储优化
- 定期清理临时文件
- 压缩历史分析结果
- 使用版本控制管理配置

## 🤝 贡献指南

欢迎贡献代码和建议！

1. Fork项目
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启Pull Request


## 🙏 致谢

- [DeepSeek](https://www.deepseek.com/) - 提供强大的AI模型
- [硅基流动](https://siliconflow.cn/) - 提供API服务
- [PyPDF2](https://pypdf2.readthedocs.io/) & [pdfplumber](https://github.com/jsvine/pdfplumber) - PDF处理库

## 📮 联系方式

如有问题或建议，欢迎：
- 提交 [Issue](https://github.com/yudongfang/AI-Literature-Analyzer/issues)
- 发送邮件至：ydf24@mails.tsinghua.edu.cn

## 📄 许可证与版权

Copyright (c) 2024 Yudong Fang (yudongfang55@gmail.com)

本项目采用 [MIT License](LICENSE) 开源许可证。

---

⭐ 如果这个项目对您有帮助，请给个Star！
