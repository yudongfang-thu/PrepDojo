# 快速开始指南

本指南帮助您在5分钟内开始使用AI Literature Analyzer。

## 📥 1. 下载和安装

```bash
# 克隆项目
git clone https://github.com/your-username/AI-Literature-Analyzer.git
cd AI-Literature-Analyzer

# 安装Python依赖
pip install -r requirements.txt
```

## ⚙️ 2. 配置API密钥

编辑 `config/config.yaml` 文件：

```yaml
api:
  # 将这里替换为您的真实API密钥
  api_key: "sk-your-real-api-key-here"
  
  # 选择模型版本
  model: "Pro/deepseek-ai/DeepSeek-R1"  # 付费版，速度更快
  # model: "deepseek-ai/DeepSeek-R1"    # 免费版，可能需要排队
```

## 📄 3. 添加PDF文献

将您的PDF文献文件复制到输入目录：

```bash
# 创建输入目录（如果不存在）
mkdir -p data/input

# 复制PDF文件
cp /path/to/your/papers/*.pdf data/input/
```

## 🧪 4. 测试分析

首先测试单篇文献分析：

```bash
python main.py --test
```

如果看到 "✅ 测试成功！"，说明系统配置正确。

## 🚀 5. 批量分析

开始分析所有文献：

```bash
# 分析所有PDF
python main.py --analyze

# 或者先分析前3篇测试
python main.py --analyze --limit 3
```

## 📊 6. 查看结果

分析完成后，结果保存在：

- `data/output/summaries/` - 详细的分析报告
- `data/output/method_cards/` - 简洁的方法卡片
- `data/output/batch_reports/` - 批量分析统计

## 🔍 7. 监控进度

在分析过程中，您可以监控进度：

```bash
# 查看当前进度
python main.py --progress

# 持续监控（另开终端窗口）
python main.py --monitor
```

## 💡 快速提示

### API密钥获取
- 访问 [硅基流动](https://siliconflow.cn/) 注册账号
- 在控制台获取API密钥
- 付费版响应更快，免费版可能需要排队

### 常见问题
- **分析速度慢**：使用付费版模型或减少输入文本长度
- **API超时**：增加配置中的 `timeout` 值
- **内存不足**：减少 `max_text_length` 配置值

### 自定义分析
- 编辑 `prompts/analysis_template.txt` 自定义分析内容
- 修改 `config/config.yaml` 调整系统行为
- 查看 `examples/example_usage.py` 了解编程接口

## 🎯 下一步

完成基础使用后，您可以：

1. 阅读完整的 [README.md](../README.md) 了解所有功能
2. 查看 [examples/](../examples/) 目录学习高级用法
3. 自定义分析提示词以适应您的研究领域
4. 集成到您的研究工作流程中

祝您使用愉快！🎉
