#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Literature Analyzer - 主程序入口
AI驱动的学术文献分析系统

Copyright (c) 2024 Yudong Fang (yudongfang55@gmail.com)
Licensed under the MIT License. See LICENSE file for details.

项目地址: https://github.com/yudongfang/AI-Literature-Analyzer
"""

import sys
import argparse
from pathlib import Path

# 添加src目录到Python路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.config_manager import ConfigManager
from core.analyzer import AILiteratureAnalyzer
from utils.progress_monitor import check_progress, ProgressMonitor


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="AI Literature Analyzer - AI驱动的文献分析系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python main.py --test                    # 测试分析单篇论文
  python main.py --analyze                 # 分析所有论文
  python main.py --analyze --limit 5       # 分析前5篇论文
  python main.py --progress                # 检查分析进度
  python main.py --monitor                 # 持续监控进度
  python main.py --config custom.yaml     # 使用自定义配置
        """
    )
    
    # 配置选项
    parser.add_argument("--config", "-c", 
                       help="配置文件路径 (默认: config/config.yaml)")
    
    # 分析选项
    parser.add_argument("--analyze", "-a", action="store_true",
                       help="开始批量分析论文")
    parser.add_argument("--test", "-t", action="store_true",
                       help="测试模式：分析单篇论文")
    parser.add_argument("--limit", "-l", type=int,
                       help="限制分析的论文数量")
    
    # 监控选项
    parser.add_argument("--progress", "-p", action="store_true",
                       help="检查当前分析进度")
    parser.add_argument("--monitor", "-m", action="store_true",
                       help="持续监控分析进度")
    parser.add_argument("--interval", type=int, default=30,
                       help="监控检查间隔(秒，默认30)")
    
    # 其他选项
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="详细输出模式")
    
    args = parser.parse_args()
    
    try:
        # 加载配置
        config_manager = ConfigManager(args.config)
        
        if args.verbose:
            print(f"📁 项目根目录: {config_manager.project_root}")
            print(f"⚙️ 配置文件: {config_manager.config_path}")
        
        # 验证API密钥
        api_key = config_manager.get('api.api_key')
        if not api_key or api_key == 'your-api-key-here':
            print("❌ 错误: 请在配置文件中设置有效的API密钥")
            print("编辑 config/config.yaml 文件，将 api.api_key 设置为您的API密钥")
            return
        
        # 执行相应的操作
        if args.progress:
            # 检查进度
            paths_config = config_manager.get_paths_config()
            check_progress(paths_config['input_dir'], paths_config['output_dir'])
            
        elif args.monitor:
            # 持续监控
            paths_config = config_manager.get_paths_config()
            monitor = ProgressMonitor(
                paths_config['input_dir'], 
                paths_config['output_dir'], 
                args.interval
            )
            try:
                monitor.monitor_continuously()
            except KeyboardInterrupt:
                print("\n👋 监控已停止")
                
        elif args.test:
            # 测试模式
            analyzer = AILiteratureAnalyzer(config_manager)
            
            # 查找第一个PDF文件进行测试
            pdf_files = list(analyzer.input_dir.glob("*.pdf"))
            if not pdf_files:
                print(f"❌ 在输入目录中未找到PDF文件: {analyzer.input_dir}")
                print("请将PDF文件放入 data/input/ 目录")
                return
            
            print("🧪 测试模式：分析第一篇论文")
            result = analyzer.analyze_single_paper(pdf_files[0])
            
            if result.get("success"):
                print("✅ 测试成功！")
            elif result.get("skipped"):
                print("⏭️ 文件已分析过，跳过")
            else:
                print("❌ 测试失败！")
                if "error" in result:
                    print(f"错误: {result['error']}")
                    
        elif args.analyze:
            # 批量分析
            analyzer = AILiteratureAnalyzer(config_manager)
            
            # 检查输入目录是否有PDF文件
            pdf_files = list(analyzer.input_dir.glob("*.pdf"))
            if not pdf_files:
                print(f"❌ 在输入目录中未找到PDF文件: {analyzer.input_dir}")
                print("请将PDF文件放入 data/input/ 目录")
                return
            
            print(f"📚 在输入目录中找到 {len(pdf_files)} 个PDF文件")
            if args.limit:
                print(f"🔢 限制分析数量: {args.limit}")
            
            # 开始批量分析
            results = analyzer.batch_analyze_papers(args.limit)
            
            # 显示结果摘要
            print(f"\n📊 分析完成摘要:")
            print(f"✅ 成功: {results['successful']} 篇")
            print(f"⏭️ 跳过: {results['skipped']} 篇")
            print(f"❌ 失败: {results['failed']} 篇")
            
        else:
            # 显示帮助信息
            parser.print_help()
            print(f"\n📁 当前项目目录: {config_manager.project_root}")
            
            # 显示配置信息
            paths_config = config_manager.get_paths_config()
            print(f"📁 输入目录: {paths_config['input_dir']}")
            print(f"📁 输出目录: {paths_config['output_dir']}")
            
            # 检查输入目录中的文件
            input_dir = Path(paths_config['input_dir'])
            if input_dir.exists():
                pdf_files = list(input_dir.glob("*.pdf"))
                print(f"📚 发现 {len(pdf_files)} 个PDF文件")
            else:
                print("⚠️ 输入目录不存在，请先创建并添加PDF文件")
                
    except Exception as e:
        print(f"❌ 程序执行错误: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
