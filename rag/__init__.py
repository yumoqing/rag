# -*- coding:utf-8 -*-
"""
RAG业务模块 - 知识库、文档、引擎配置、订阅管理
作为独立模块被ragserver或其他应用引用
"""

from .init import init_rag_module

__all__ = ['init_rag_module']
