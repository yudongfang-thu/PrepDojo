#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Literature Analyzer 使用示例
"""

import sys
from pathlib import Path

# 添加src目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.config_manager import ConfigManager
from core.analyzer import AILiteratureAnalyzer
from utils.progress_monitor import ProgressMonitor


def example_basic_usage():
    """基础使用示例"""
    print("🔬 基础使用示例")
    
    # 1. 加载配置
    config = ConfigManager()
    
    # 2. 创建分析器
    analyzer = AILiteratureAnalyzer(config)
    
    # 3. 检查输入目录
    pdf_files = list(analyzer.input_dir.glob("*.pdf"))
    print(f"📚 发现 {len(pdf_files)} 个PDF文件")
    
    if pdf_files:
        # 4. 分析第一篇论文
        print("🧪 分析第一篇论文...")
        result = analyzer.analyze_single_paper(pdf_files[0])
        
        if result.get("success"):
            print("✅ 分析成功！")
        else:
            print(f"❌ 分析失败: {result.get('error', '未知错误')}")
    else:
        print("⚠️ 请先将PDF文件放入 data/input/ 目录")


def example_custom_config():
    """自定义配置示例"""
    print("\n⚙️ 自定义配置示例")
    
    # 创建自定义配置
    custom_config = {
        'api': {
            'api_key': 'your-api-key',
            'model': 'deepseek-ai/DeepSeek-R1',  # 使用免费版
            'temperature': 0.2,  # 更低的温度
            'max_tokens': 2000   # 更短的输出
        },
        'processing': {
            'max_text_length': 3000,  # 更短的输入
            'extract_pages': 5,       # 只提取前5页
            'skip_analyzed': True     # 跳过已分析的文件
        }
    }
    
    print(f"📊 自定义配置示例：")
    print(f"  - 模型: {custom_config['api']['model']}")
    print(f"  - 温度: {custom_config['api']['temperature']}")
    print(f"  - 最大输入: {custom_config['processing']['max_text_length']} 字符")


def example_progress_monitoring():
    """进度监控示例"""
    print("\n📈 进度监控示例")
    
    config = ConfigManager()
    paths = config.get_paths_config()
    
    # 创建进度监控器
    monitor = ProgressMonitor(
        input_dir=paths['input_dir'],
        output_dir=paths['output_dir'],
        check_interval=10  # 10秒检查一次
    )
    
    # 获取当前进度
    progress = monitor.get_progress()
    print(f"📊 当前进度: {progress['analyzed_count']}/{progress['total_papers']} ({progress['progress_percentage']:.1f}%)")
    
    if progress['recent_files']:
        print("📝 最近分析的文件:")
        for file in progress['recent_files']:
            print(f"  - {file}")


def example_batch_analysis():
    """批量分析示例"""
    print("\n🚀 批量分析示例")
    
    config = ConfigManager()
    analyzer = AILiteratureAnalyzer(config)
    
    # 检查可分析的文件
    pdf_files = list(analyzer.input_dir.glob("*.pdf"))
    print(f"📚 找到 {len(pdf_files)} 个PDF文件")
    
    if len(pdf_files) > 0:
        print("💡 批量分析选项:")
        print(f"  - 分析所有文件: analyzer.batch_analyze_papers()")
        print(f"  - 分析前3篇: analyzer.batch_analyze_papers(max_papers=3)")
        print(f"  - 跳过已分析: 在配置中设置 skip_analyzed: true")
        
        # 模拟批量分析（不实际执行）
        print("📋 将会生成以下文件:")
        for pdf_file in pdf_files[:3]:  # 只显示前3个
            safe_name = analyzer._safe_filename(pdf_file.stem)
            print(f"  - {safe_name}_ai_analysis.md")
            print(f"  - {safe_name}_method_card.md")
    else:
        print("⚠️ 没有找到PDF文件，请先添加文件到 data/input/ 目录")


def main():
    """主函数"""
    print("🤖 AI Literature Analyzer 使用示例")
    print("=" * 50)
    
    try:
        # 运行各种示例
        example_basic_usage()
        example_custom_config()
        example_progress_monitoring()
        example_batch_analysis()
        
        print("\n✅ 示例运行完成！")
        print("💡 提示：运行 python main.py --help 查看所有可用选项")
        
    except Exception as e:
        print(f"❌ 示例运行出错: {e}")
        print("请检查配置文件和依赖安装是否正确")


if __name__ == "__main__":
    main()
