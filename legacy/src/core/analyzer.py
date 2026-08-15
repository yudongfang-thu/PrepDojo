#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI文献分析器核心类
重构版本 - 支持配置文件和自定义模板

Copyright (c) 2024 Yudong Fang (yudongfang55@gmail.com)
Licensed under the MIT License. See LICENSE file for details.
"""

import os
import json
import re
import requests
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
import PyPDF2
import pdfplumber
import logging

from .config_manager import ConfigManager


class AILiteratureAnalyzer:
    """AI驱动的文献分析器"""
    
    def __init__(self, config_manager: ConfigManager):
        """
        初始化分析器
        
        Args:
            config_manager: 配置管理器实例
        """
        self.config = config_manager
        
        # 获取配置
        api_config = self.config.get_api_config()
        paths_config = self.config.get_paths_config()
        
        # API配置
        self.api_key = api_config['api_key']
        self.api_base = api_config['base_url']
        self.model = api_config['model']
        self.timeout = api_config['timeout']
        self.max_retries = api_config['max_retries']
        self.retry_delay = api_config['retry_delay']
        self.temperature = api_config['temperature']
        self.max_tokens = api_config['max_tokens']
        
        # 路径配置
        self.input_dir = Path(paths_config['input_dir'])
        self.output_dir = Path(paths_config['output_dir'])
        self.summaries_dir = Path(paths_config['summaries_dir'])
        self.method_cards_dir = Path(paths_config['method_cards_dir'])
        self.batch_reports_dir = Path(paths_config['batch_reports_dir'])
        
        # 处理配置
        processing_config = self.config.get_processing_config()
        self.max_text_length = processing_config['max_text_length']
        self.extract_pages = processing_config['extract_pages']
        self.skip_analyzed = processing_config['skip_analyzed']
        
        # 创建必要目录
        self.config.create_directories()
        
        # 设置日志
        self._setup_logging()
        
        # 加载提示词模板
        self.analysis_template = self.config.load_prompt_template('analysis')
        self.method_card_template = self.config.load_prompt_template('method_card')
        
        self.logger.info(f"AI文献分析系统已启动")
        self.logger.info(f"使用模型: {self.model}")
        self.logger.info(f"输入目录: {self.input_dir}")
        self.logger.info(f"输出目录: {self.output_dir}")
    
    def _setup_logging(self):
        """设置日志"""
        logging_config = self.config.get_logging_config()
        
        # 创建logger
        self.logger = logging.getLogger('AILiteratureAnalyzer')
        self.logger.setLevel(getattr(logging, logging_config.get('level', 'INFO')))
        
        # 清除已有的处理器
        self.logger.handlers.clear()
        
        # 文件处理器
        paths_config = self.config.get_paths_config()
        log_file = paths_config.get('log_file')
        if log_file:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(logging_config.get('format', 
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        
        # 控制台处理器
        if logging_config.get('console_output', True):
            console_handler = logging.StreamHandler()
            console_handler.setLevel(getattr(logging, logging_config.get('level', 'INFO')))
            formatter = logging.Formatter('%(levelname)s - %(message)s')
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
    
    def extract_pdf_text(self, pdf_path: Path) -> str:
        """从PDF提取文本"""
        text = ""
        
        try:
            # 首先尝试pdfplumber（更精确）
            with pdfplumber.open(pdf_path) as pdf:
                pages_to_extract = len(pdf.pages)
                if self.extract_pages > 0:
                    pages_to_extract = min(self.extract_pages, len(pdf.pages))
                
                for i in range(pages_to_extract):
                    page_text = pdf.pages[i].extract_text()
                    if page_text:
                        text += page_text + "\n"
                        
            if not text.strip():
                raise Exception("pdfplumber未提取到文本")
                
        except Exception as e:
            self.logger.warning(f"pdfplumber失败，尝试PyPDF2: {e}")
            try:
                with open(pdf_path, 'rb') as file:
                    pdf_reader = PyPDF2.PdfReader(file)
                    pages_to_extract = len(pdf_reader.pages)
                    if self.extract_pages > 0:
                        pages_to_extract = min(self.extract_pages, len(pdf_reader.pages))
                    
                    for i in range(pages_to_extract):
                        text += pdf_reader.pages[i].extract_text() + "\n"
            except Exception as e2:
                self.logger.error(f"PDF文本提取完全失败: {e2}")
                return ""
        
        return text.strip()
    
    def call_ai_api(self, messages: List[Dict[str, str]]) -> str:
        """调用AI API"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }
        
        for attempt in range(self.max_retries):
            try:
                self.logger.info(f"API调用尝试 {attempt + 1}/{self.max_retries}")
                response = requests.post(
                    f"{self.api_base}/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    result = response.json()
                    return result["choices"][0]["message"]["content"]
                else:
                    self.logger.error(f"API调用失败: {response.status_code}")
                    self.logger.error(f"错误信息: {response.text}")
                    if attempt < self.max_retries - 1:
                        self.logger.info(f"等待{self.retry_delay}秒后重试...")
                        time.sleep(self.retry_delay)
                    continue
                    
            except Exception as e:
                self.logger.error(f"API调用异常 (尝试 {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    self.logger.info(f"等待{self.retry_delay}秒后重试...")
                    time.sleep(self.retry_delay)
                continue
        
        self.logger.error("所有重试都失败了")
        return ""
    
    def analyze_paper_with_ai(self, pdf_text: str, pdf_filename: str) -> Dict[str, Any]:
        """使用AI分析论文"""
        self.logger.info("正在进行AI深度分析...")
        
        # 限制文本长度
        content = pdf_text[:self.max_text_length]
        
        # 构建分析提示词
        analysis_prompt = self.analysis_template.format(
            filename=pdf_filename,
            content=content
        )
        
        messages = [
            {"role": "system", "content": "你是一个模型轻量化领域的专家，专门分析深度学习模型压缩和优化相关的学术论文。你的分析深入、客观、专业。"},
            {"role": "user", "content": analysis_prompt}
        ]
        
        ai_response = self.call_ai_api(messages)
        
        if not ai_response:
            return {"error": "AI分析失败"}
        
        return {"analysis": ai_response, "success": True}
    
    def generate_method_card_with_ai(self, analysis: str, pdf_filename: str) -> str:
        """使用AI生成方法卡片"""
        self.logger.info("生成方法卡片...")
        
        method_prompt = self.method_card_template.format(analysis=analysis)
        
        messages = [
            {"role": "system", "content": "你是一个技术文档专家，专门生成清晰简洁的技术方法卡片。"},
            {"role": "user", "content": method_prompt}
        ]
        
        return self.call_ai_api(messages)
    
    def is_already_analyzed(self, pdf_path: Path) -> bool:
        """检查文件是否已经分析过"""
        if not self.skip_analyzed:
            return False
        
        safe_name = self._safe_filename(pdf_path.stem)
        output_config = self.config.get_output_config()
        suffix = output_config['summary_suffix']
        
        analysis_file = self.summaries_dir / f"{safe_name}{suffix}.md"
        return analysis_file.exists()
    
    def analyze_single_paper(self, pdf_path: Path) -> Dict[str, Any]:
        """分析单篇论文"""
        self.logger.info(f"开始分析: {pdf_path.name}")
        
        # 检查是否已分析
        if self.is_already_analyzed(pdf_path):
            self.logger.info(f"跳过已分析的文件: {pdf_path.name}")
            return {"skipped": True, "file": pdf_path.name}
        
        # 提取PDF文本
        pdf_text = self.extract_pdf_text(pdf_path)
        if not pdf_text:
            self.logger.error("PDF文本提取失败")
            return {"error": "文本提取失败", "file": pdf_path.name}
        
        self.logger.info(f"文本提取成功，长度: {len(pdf_text)} 字符")
        
        # AI分析
        analysis_result = self.analyze_paper_with_ai(pdf_text, pdf_path.name)
        if "error" in analysis_result:
            self.logger.error("AI分析失败")
            return analysis_result
        
        # 生成方法卡片
        method_card = self.generate_method_card_with_ai(analysis_result["analysis"], pdf_path.name)
        
        # 保存结果
        result = {
            "file_path": str(pdf_path),
            "analysis": analysis_result["analysis"],
            "method_card": method_card,
            "analysis_date": datetime.now().isoformat(),
            "success": True
        }
        
        # 保存分析报告
        self._save_analysis_report(pdf_path, result)
        
        # 保存方法卡片
        self._save_method_card(pdf_path, method_card)
        
        self.logger.info(f"分析完成: {pdf_path.name}")
        return result
    
    def _save_analysis_report(self, pdf_path: Path, result: Dict[str, Any]):
        """保存分析报告"""
        safe_name = self._safe_filename(pdf_path.stem)
        output_config = self.config.get_output_config()
        suffix = output_config['summary_suffix']
        
        report_path = self.summaries_dir / f"{safe_name}{suffix}.md"
        
        # 构建内容
        content_parts = [f"# {pdf_path.name} - AI深度分析"]
        
        if output_config.get('include_metadata', True):
            content_parts.extend([
                "",
                f"**分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"**使用模型**: {self.model}",
                f"**原始文件**: {pdf_path.name}",
                ""
            ])
        
        content_parts.extend([
            "---",
            "",
            result['analysis'],
            "",
            "---",
            "",
            "*本分析由AI系统自动生成*"
        ])
        
        content = "\n".join(content_parts)
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        self.logger.info(f"分析报告已保存: {report_path.name}")
    
    def _save_method_card(self, pdf_path: Path, method_card: str):
        """保存方法卡片"""
        safe_name = self._safe_filename(pdf_path.stem)
        output_config = self.config.get_output_config()
        suffix = output_config['method_card_suffix']
        
        card_path = self.method_cards_dir / f"{safe_name}{suffix}.md"
        
        # 构建内容
        content_parts = [f"# {pdf_path.name} - 方法卡片"]
        
        if output_config.get('include_metadata', True):
            content_parts.extend([
                "",
                f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"**原始文件**: {pdf_path.name}",
                ""
            ])
        
        content_parts.extend([
            "---",
            "",
            method_card,
            "",
            "---",
            "",
            "*本卡片由AI系统自动生成*"
        ])
        
        content = "\n".join(content_parts)
        
        with open(card_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        self.logger.info(f"方法卡片已保存: {card_path.name}")
    
    def _safe_filename(self, filename: str) -> str:
        """生成安全的文件名"""
        safe_name = re.sub(r'[<>:"/\\|?*]', '', filename)
        safe_name = re.sub(r'\s+', '_', safe_name)
        return safe_name[:50]  # 限制长度
    
    def batch_analyze_papers(self, max_papers: int = None) -> Dict[str, Any]:
        """批量分析论文"""
        pdf_files = list(self.input_dir.glob("*.pdf"))
        
        if max_papers:
            pdf_files = pdf_files[:max_papers]
        
        self.logger.info(f"开始批量分析，共 {len(pdf_files)} 篇论文")
        
        results = {
            "total_papers": len(pdf_files),
            "successful": 0,
            "failed": 0,
            "skipped": 0,
            "results": [],
            "start_time": datetime.now().isoformat()
        }
        
        for i, pdf_path in enumerate(pdf_files, 1):
            self.logger.info(f"--- 处理 {i}/{len(pdf_files)} ---")
            
            try:
                result = self.analyze_single_paper(pdf_path)
                if result.get("success"):
                    results["successful"] += 1
                elif result.get("skipped"):
                    results["skipped"] += 1
                else:
                    results["failed"] += 1
                results["results"].append(result)
                
            except Exception as e:
                self.logger.error(f"处理 {pdf_path.name} 时出错: {e}")
                results["failed"] += 1
                results["results"].append({
                    "error": str(e),
                    "file": pdf_path.name
                })
        
        results["end_time"] = datetime.now().isoformat()
        
        # 生成批量分析报告
        self._save_batch_report(results)
        
        self.logger.info(f"批量分析完成！")
        self.logger.info(f"成功: {results['successful']} 篇")
        self.logger.info(f"跳过: {results['skipped']} 篇") 
        self.logger.info(f"失败: {results['failed']} 篇")
        
        return results
    
    def _save_batch_report(self, results: Dict[str, Any]):
        """保存批量分析报告"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.batch_reports_dir / f"ai_batch_analysis_{timestamp}.md"
        
        success_rate = (results["successful"] / results["total_papers"] * 100) if results["total_papers"] > 0 else 0
        
        content = f"""# AI驱动批量文献分析报告

## 📊 分析统计

- **分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **使用模型**: {self.model}
- **总论文数**: {results["total_papers"]}
- **成功分析**: {results["successful"]} 篇
- **跳过文件**: {results["skipped"]} 篇
- **分析失败**: {results["failed"]} 篇
- **成功率**: {success_rate:.1f}%

## 📋 分析详情

"""
        
        # 成功的分析
        successful_papers = [r for r in results["results"] if r.get("success")]
        if successful_papers:
            content += "### ✅ 成功分析的论文\n\n"
            for i, result in enumerate(successful_papers, 1):
                filename = Path(result["file_path"]).name
                content += f"{i}. **{filename}**\n"
            content += "\n"
        
        # 跳过的分析
        skipped_papers = [r for r in results["results"] if r.get("skipped")]
        if skipped_papers:
            content += "### ⏭️ 跳过的论文\n\n"
            for i, result in enumerate(skipped_papers, 1):
                filename = result.get("file", "未知文件")
                content += f"{i}. **{filename}** - 已存在分析结果\n"
            content += "\n"
        
        # 失败的分析
        failed_papers = [r for r in results["results"] if not r.get("success") and not r.get("skipped")]
        if failed_papers:
            content += "### ❌ 分析失败的论文\n\n"
            for i, result in enumerate(failed_papers, 1):
                filename = result.get("file", "未知文件")
                error = result.get("error", "未知错误")
                content += f"{i}. **{filename}** - {error}\n"
            content += "\n"
        
        content += f"""## 🎯 系统配置

- **AI模型**: {self.model}
- **最大文本长度**: {self.max_text_length} 字符
- **提取页数**: {self.extract_pages if self.extract_pages > 0 else '全部'}
- **跳过已分析**: {'是' if self.skip_analyzed else '否'}

## 📚 生成文件

- **分析报告**: `{self.summaries_dir.name}/*{self.config.get_output_config()['summary_suffix']}.md`
- **方法卡片**: `{self.method_cards_dir.name}/*{self.config.get_output_config()['method_card_suffix']}.md`
- **批量报告**: 本文件

---

*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
*AI驱动文献分析系统*
"""
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        self.logger.info(f"批量分析报告已保存: {report_path.name}")
