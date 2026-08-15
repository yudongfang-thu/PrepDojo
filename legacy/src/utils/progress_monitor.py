#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进度监控工具
"""

import time
from pathlib import Path
from typing import Optional


class ProgressMonitor:
    """进度监控器"""
    
    def __init__(self, input_dir: str, output_dir: str, check_interval: int = 30):
        """
        初始化进度监控器
        
        Args:
            input_dir: 输入目录路径
            output_dir: 输出目录路径  
            check_interval: 检查间隔(秒)
        """
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.summaries_dir = self.output_dir / "summaries"
        self.check_interval = check_interval
    
    def get_progress(self) -> dict:
        """获取当前进度"""
        # 统计PDF文件数量
        pdf_files = list(self.input_dir.glob("*.pdf"))
        total_papers = len(pdf_files)
        
        # 统计已分析的文件数量
        ai_analysis_files = list(self.summaries_dir.glob("*_ai_analysis.md"))
        analyzed_count = len(ai_analysis_files)
        
        progress_pct = (analyzed_count / total_papers * 100) if total_papers > 0 else 0
        
        return {
            "total_papers": total_papers,
            "analyzed_count": analyzed_count,
            "progress_percentage": progress_pct,
            "remaining": total_papers - analyzed_count,
            "recent_files": self._get_recent_files(ai_analysis_files)
        }
    
    def _get_recent_files(self, files: list, limit: int = 3) -> list:
        """获取最近的文件"""
        if not files:
            return []
        
        # 按修改时间排序
        recent_files = sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)
        return [f.name for f in recent_files[:limit]]
    
    def print_progress(self):
        """打印当前进度"""
        progress = self.get_progress()
        
        print(f"📊 分析进度监控")
        print(f"📚 总论文数: {progress['total_papers']}")
        print(f"✅ 已分析: {progress['analyzed_count']}")
        print(f"🔄 进度: {progress['analyzed_count']}/{progress['total_papers']} ({progress['progress_percentage']:.1f}%)")
        print(f"⏳ 剩余: {progress['remaining']}")
        
        if progress['recent_files']:
            print(f"\n📝 最新分析的文件:")
            for file in progress['recent_files']:
                print(f"  - {file}")
    
    def monitor_continuously(self, max_duration: Optional[int] = None):
        """
        持续监控进度
        
        Args:
            max_duration: 最大监控时长(秒)，None表示无限制
        """
        print(f"📊 开始监控AI分析进度")
        print("=" * 50)
        
        start_time = time.time()
        last_count = 0
        
        while True:
            progress = self.get_progress()
            current_count = progress['analyzed_count']
            total_papers = progress['total_papers']
            
            # 检查是否有进度更新
            if current_count != last_count:
                print(f"🔄 进度更新: {current_count}/{total_papers} ({progress['progress_percentage']:.1f}%)")
                
                if current_count > last_count:
                    # 显示新完成的文件
                    new_files_count = current_count - last_count
                    recent_files = progress['recent_files'][:new_files_count]
                    for file in recent_files:
                        print(f"  ✅ 新完成: {file}")
                
                last_count = current_count
                
                # 检查是否完成
                if current_count >= total_papers:
                    print("🎉 所有文献分析完成！")
                    break
            
            # 检查最大监控时长
            if max_duration and (time.time() - start_time) > max_duration:
                print(f"⏰ 监控时长达到限制 ({max_duration}秒)，停止监控")
                break
            
            # 等待下次检查
            time.sleep(self.check_interval)


def check_progress(input_dir: str, output_dir: str):
    """快速检查进度的便捷函数"""
    monitor = ProgressMonitor(input_dir, output_dir)
    monitor.print_progress()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="AI文献分析进度监控")
    parser.add_argument("--input-dir", required=True, help="输入目录路径")
    parser.add_argument("--output-dir", required=True, help="输出目录路径")
    parser.add_argument("--monitor", action="store_true", help="持续监控模式")
    parser.add_argument("--interval", type=int, default=30, help="检查间隔(秒)")
    parser.add_argument("--max-duration", type=int, help="最大监控时长(秒)")
    
    args = parser.parse_args()
    
    if args.monitor:
        monitor = ProgressMonitor(args.input_dir, args.output_dir, args.interval)
        try:
            monitor.monitor_continuously(args.max_duration)
        except KeyboardInterrupt:
            print("\n👋 监控已停止")
    else:
        check_progress(args.input_dir, args.output_dir)
