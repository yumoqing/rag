#!/usr/bin/env python3
"""RBAC path registration for rag module.

在宿主应用根目录执行：py3/bin/python pkgs/rag/scripts/load_path.py
pipeline-app 的 load_path.sh 会自动扫描 pkgs/*/scripts/load_path.py 调用本脚本。
"""
import subprocess

MOD = "rag"

# 静态资源 + 免登录可访问
PATHS_ANY = [
    f"/{MOD}/knowledge_bases_list/upload.js",
]

# 登录用户可访问（知识库管理全部端点）
PATHS_LOGINED = [
    f"/{MOD}",
    # 知识库列表与详情
    f"/{MOD}/knowledge_bases_list/index.ui",
    f"/{MOD}/knowledge_bases_list/detail.ui",
    f"/{MOD}/knowledge_bases_list/search.ui",
    f"/{MOD}/knowledge_bases_list/new_kb_form.ui",
    f"/{MOD}/knowledge_bases_list/kb_list.dspy",
    f"/{MOD}/knowledge_bases_list/kb_options.dspy",
    f"/{MOD}/knowledge_bases_list/create_kb.dspy",
    f"/{MOD}/knowledge_bases_list/rename_kb.dspy",
    f"/{MOD}/knowledge_bases_list/rename_kb_form.dspy",
    f"/{MOD}/knowledge_bases_list/delete_kb.dspy",
    f"/{MOD}/knowledge_bases_list/confirm_delete_form.dspy",
    f"/{MOD}/knowledge_bases_list/embedding_options.dspy",
    # 文件与目录树
    f"/{MOD}/knowledge_bases_list/file_list.dspy",
    f"/{MOD}/knowledge_bases_list/upload.dspy",
    f"/{MOD}/knowledge_bases_list/upload_file.dspy",
    f"/{MOD}/knowledge_bases_list/delete_file.dspy",
    f"/{MOD}/knowledge_bases_list/batch_ingest.dspy",
    f"/{MOD}/knowledge_bases_list/get_tree_data.dspy",
    f"/{MOD}/knowledge_bases_list/new_tree_item.dspy",
    f"/{MOD}/knowledge_bases_list/update_tree_item.dspy",
    f"/{MOD}/knowledge_bases_list/delete_tree_item.dspy",
    # 检索与分析
    f"/{MOD}/knowledge_bases_list/search_result.dspy",
    f"/{MOD}/knowledge_bases_list/analysis.dspy",
    f"/{MOD}/knowledge_bases_list/media_cards.dspy",
    # 标签
    f"/{MOD}/knowledge_bases_list/tag_form.dspy",
    f"/{MOD}/knowledge_bases_list/tag_options.dspy",
    f"/{MOD}/knowledge_bases_list/add_tag.dspy",
    f"/{MOD}/knowledge_bases_list/save_tags.dspy",
    # 存储用量
    f"/{MOD}/knowledge_bases_list/storage_card.dspy",
    f"/{MOD}/knowledge_bases_list/storage_stats.dspy",
    # CRUD 管理页
    f"/{MOD}/documents_list/index.ui",
    f"/{MOD}/engine_configs_list/index.ui",
    f"/{MOD}/subscriptions_list/index.ui",
]


def register_paths():
    for path in PATHS_ANY:
        subprocess.run(["py3/bin/python", "set_role_perm.py", "any", path])
        print(f"  any: {path}")
    for path in PATHS_LOGINED:
        subprocess.run(["py3/bin/python", "set_role_perm.py", "logined", path])
        print(f"  logined: {path}")


if __name__ == "__main__":
    print(f"=== {MOD} RBAC registration ===")
    register_paths()
    print(f"Done. any={len(PATHS_ANY)} logined={len(PATHS_LOGINED)}")
