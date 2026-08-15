#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理器
负责加载和管理系统配置

Copyright (c) 2024 Yudong Fang (yudongfang55@gmail.com)
Licensed under the MIT License. See LICENSE file for details.
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional

class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置管理器
        
        Args:
            config_path: 配置文件路径，默认为 config/config.yaml
        """
        if config_path is None:
            # 获取项目根目录
            self.project_root = Path(__file__).parent.parent.parent
            config_path = self.project_root / "config" / "config.yaml"
        else:
            config_path = Path(config_path)
            self.project_root = config_path.parent.parent
        
        self.config_path = config_path
        self.config = self._load_config()
        self._validate_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            return config
        except FileNotFoundError:
            raise FileNotFoundError(f"配置文件未找到: {self.config_path}")
        except yaml.YAMLError as e:
            raise ValueError(f"配置文件格式错误: {e}")
    
    def _validate_config(self):
        """验证配置的完整性"""
        required_sections = ['api', 'paths', 'processing', 'output', 'prompts']
        for section in required_sections:
            if section not in self.config:
                raise ValueError(f"配置文件缺少必要部分: {section}")
        
        # 检查API密钥
        api_key = self.config['api'].get('api_key')
        if not api_key or api_key == 'your-api-key-here':
            print("⚠️ 警告: 请在config.yaml中设置您的API密钥")
    
    def get(self, key: str, default=None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键，支持点分隔的嵌套键，如 'api.model'
            default: 默认值
        
        Returns:
            配置值
        """
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def get_api_config(self) -> Dict[str, Any]:
        """获取API配置"""
        return self.config['api']
    
    def get_paths_config(self) -> Dict[str, str]:
        """获取路径配置，并转换为绝对路径"""
        paths = self.config['paths'].copy()
        
        # 转换为绝对路径
        for key, path in paths.items():
            if not os.path.isabs(path):
                paths[key] = str(self.project_root / path)
        
        return paths
    
    def get_processing_config(self) -> Dict[str, Any]:
        """获取处理配置"""
        return self.config['processing']
    
    def get_output_config(self) -> Dict[str, Any]:
        """获取输出配置"""
        return self.config['output']
    
    def get_prompts_config(self) -> Dict[str, Any]:
        """获取提示词配置"""
        return self.config['prompts']
    
    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.config.get('logging', {})
    
    def load_prompt_template(self, template_type: str) -> str:
        """
        加载提示词模板
        
        Args:
            template_type: 模板类型 ('analysis' 或 'method_card')
        
        Returns:
            模板内容
        """
        prompts_config = self.get_prompts_config()
        
        if template_type == 'analysis':
            template_file = prompts_config.get('analysis_template', 'prompts/analysis_template.txt')
        elif template_type == 'method_card':
            template_file = prompts_config.get('method_card_template', 'prompts/method_card_template.txt')
        else:
            raise ValueError(f"未知的模板类型: {template_type}")
        
        # 转换为绝对路径
        if not os.path.isabs(template_file):
            template_file = self.project_root / template_file
        
        try:
            with open(template_file, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"提示词模板文件未找到: {template_file}")
    
    def create_directories(self):
        """创建必要的目录"""
        paths = self.get_paths_config()
        
        directories = [
            paths['input_dir'],
            paths['output_dir'],
            paths['summaries_dir'],
            paths['method_cards_dir'],
            paths['batch_reports_dir']
        ]
        
        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)
    
    def __str__(self) -> str:
        """返回配置的字符串表示"""
        return f"ConfigManager(config_path={self.config_path})"
