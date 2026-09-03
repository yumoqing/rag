# -*- coding:utf-8 -*-
"""RAG tools — 与对外 API 相同功能的函数包装，供内部助手（agent）调用。

对外 API（wwwroot/api/*.dspy → api_core.py）是给其他系统的 HTTP 机器接口；
内部助手（本进程/同生态的 LLM agent）不必绕 HTTP + API Key，直接调本模块：

    from rag.tools import RAG_TOOL_SCHEMAS, exec_rag_tool
    # 助手注册工具时注入 schema；执行时：
    result = await exec_rag_tool("rag_search", {"query": "...", "kb_id": "..."}, org_id=my_org)

约定：
- schema 为 OpenAI function-calling 格式（RAG_TOOL_SCHEMAS 列表）；
- exec_rag_tool 返回 dict：成功 {"status":"ok","data":{...}}，失败 {"status":"error","message":...,"data":null}
  —— 与对外 API 的 JSON 完全同构，助手侧处理逻辑可共用；
- org 隔离与 api_core 一致：以传入的 org_id 为身份边界（内部助手由宿主注入可信 org，不走 key）；
- rag_doc_upload 与 HTTP 版的差异：HTTP 收原始字节；tools 收 file_path（服务端可读路径）或 file_base64。

2026-09-03 新增（配合对外 API，避免"API 一套、助手又写一套"的分叉）。
"""
import base64
import json
import os

from rag import api_core as C


# ────────────────────────── OpenAI tool schema ──────────────────────────

RAG_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "rag_kb_create",
            "description": "创建知识库。embedding_engine 创建时定死：bge-m3(纯文本) / clip-vith14(多媒体) / qwen3-vl-embedding(多模态在线)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "知识库名称"},
                    "description": {"type": "string", "description": "描述（可选）"},
                    "embedding_engine": {"type": "string", "enum": ["bge-m3", "clip-vith14", "qwen3-vl-embedding"], "description": "向量引擎，默认 bge-m3"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_kb_delete",
            "description": "删除知识库（级联清理向量、分块、文件，不可恢复）。",
            "parameters": {
                "type": "object",
                "properties": {"kb_id": {"type": "string", "description": "知识库ID"}},
                "required": ["kb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_doc_upload",
            "description": "上传文件入库（异步：返回后后台解析+向量化，可稍后用 rag_search 验证）。file_path 与 file_base64 二选一。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "目标知识库ID"},
                    "file_name": {"type": "string", "description": "文件名（含扩展名）"},
                    "file_path": {"type": "string", "description": "服务端可读的文件绝对路径（与 file_base64 二选一）"},
                    "file_base64": {"type": "string", "description": "文件内容 base64（与 file_path 二选一）"},
                },
                "required": ["kb_id", "file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_doc_delete",
            "description": "删除文档（级联清理向量、分块、磁盘文件）。",
            "parameters": {
                "type": "object",
                "properties": {"doc_id": {"type": "string", "description": "文档ID"}},
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_tag_create",
            "description": "创建标签（同名幂等，已存在则返回原标签）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "知识库ID"},
                    "name": {"type": "string", "description": "标签名"},
                    "color": {"type": "string", "description": "颜色 #rrggbb（可选）"},
                },
                "required": ["kb_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_doc_set_tags",
            "description": "给文档设置标签（全量语义：传入的集合即最终标签，未传的解绑；空集合=清空）。标签名不存在时自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "知识库ID"},
                    "doc_id": {"type": "string", "description": "文档ID"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "标签名列表"},
                },
                "required": ["kb_id", "doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "知识库检索（向量召回+重排）。不传 kb_id 则检索本机构全部知识库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题/关键词"},
                    "kb_id": {"type": "string", "description": "限定知识库ID（可选）"},
                    "top_k": {"type": "integer", "description": "返回条数，默认10"},
                },
                "required": ["query"],
            },
        },
    },
]

RAG_TOOL_NAMES = [t["function"]["name"] for t in RAG_TOOL_SCHEMAS]


# ────────────────────────── 分发执行 ──────────────────────────

async def exec_rag_tool(tool_name: str, params: dict, org_id: str) -> dict:
    """执行一个 rag tool。params 与 schema 对齐；org_id 为调用方可操作机构（宿主注入）。

    返回 dict：{"status":"ok","data":{...}} 或 {"status":"error","message":...,"data":null}
    """
    if tool_name not in RAG_TOOL_NAMES:
        return {"status": "error", "message": f"unknown rag tool: {tool_name}", "data": None}
    if not org_id:
        return {"status": "error", "message": "org_id required", "data": None}
    ns = dict(params or {})
    env = C.make_api_env({"org_id": org_id, "user_id": ns.pop("_user_id", "") or ""})

    if tool_name == "rag_doc_upload":
        file_data = None
        if ns.get("file_path"):
            try:
                with open(ns["file_path"], "rb") as f:
                    file_data = f.read()
            except Exception as e:
                return {"status": "error", "message": f"read file_path failed: {e}", "data": None}
        elif ns.get("file_base64"):
            try:
                file_data = base64.b64decode(ns["file_base64"])
            except Exception as e:
                return {"status": "error", "message": f"base64 decode failed: {e}", "data": None}
        else:
            return {"status": "error", "message": "file_path or file_base64 required", "data": None}
        ns.pop("file_path", None)
        ns.pop("file_base64", None)
        raw = await C.doc_upload(env, ns, file_data, ns.get("file_name", "upload.bin"))
    else:
        fn = {
            "rag_kb_create": C.kb_create,
            "rag_kb_delete": C.kb_delete,
            "rag_doc_delete": C.doc_delete,
            "rag_tag_create": C.tag_create,
            "rag_doc_set_tags": C.doc_set_tags,
            "rag_search": C.search,
        }[tool_name]
        raw = await fn(env, ns)

    try:
        return json.loads(raw)
    except Exception:
        return {"status": "error", "message": f"tool result not JSON: {str(raw)[:200]}", "data": None}
