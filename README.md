# rag — RAG 知识库管理模块

产线平台的 RAG（Retrieval-Augmented Generation）知识库管理模块。提供知识库全生命周期管理：建库、文档/多媒体上传入库（分片 → 向量化 → VDB 入库 → NER → 图谱）、统一检索（文本 + 图/音/视多媒体）、标签管理、租户订阅与用量计费。

模块以 `pkgs/rag` 形式挂载到宿主应用（ragserver、pipeline-app 等），同一份代码在不同宿主各自建表、各自持有数据。

## 目录结构

```
models/          # 表定义 JSON（11 张 rag_ 前缀表）
json/            # 部分表的 UI/CRUD JSON 定义
init/            # uapi_seed.sql（uapi 种子）、migrate_rag_prefix.sql（旧表加前缀迁移，幂等）
rag/             # Python 包：init.py（DSPY handler 注册）、api_core.py、pipeline.py、
                 #   ingest.py、vl_online.py（在线 embedding/rerank + VDB 直调）等
scripts/         # load_path.py（RBAC 路径注册，宿主 load_path.sh 自动扫描调用）
wwwroot/         # 前端页面与 DSPY 端点
  api/           #   对外 B2B API（kb_create/doc_upload/search/tag_* 等 .dspy）
  knowledge_bases_list/  # 知识库管理主页面（index.ui + 全套 .dspy）
  documents_list/ engine_configs_list/ subscriptions_list/
build/           # 打包产物（勿手改）
```

后端注册：`rag/init.py` 通过 `appPublic.registerfunction.RegisterFunction` 注册全部 DSPY handler（`kb_list`、`search`、`doc_upload`、`doc_delete`、`dir_*`、`tag_*`、`engine_options`、`status` 等）。宿主入口调用规范名 `load_rag()`（旧名 `init_rag_module` 保留为别名，ragserver 等旧宿主无需改动）。

## 表清单（rag_ 前缀）

模块跨应用复用，所有表统一加 `rag_` 前缀，避免与宿主业务表冲突（旧无前缀表可用 `init/migrate_rag_prefix.sql` 幂等改名迁移）。

| 表名 | 用途 |
|------|------|
| `rag_knowledge_bases` | 知识库主表（含 `embedding_engine`、`vdb_collection`、存储占用等） |
| `rag_documents` | 文档/媒体文件（含 metadata JSON，存标签、声纹状态、位置信息等） |
| `rag_document_chunks` | 文档分片（metadata 存 bbox、start_time/end_time 等位置信息） |
| `rag_engine_configs` | 引擎配置（engine_type/endpoint_url/api_key/model_name，按 org 隔离） |
| `rag_entities` | NER 抽取的实体 |
| `rag_entity_relations` | 实体关系（图谱边） |
| `rag_tags` | 标签定义 |
| `rag_media_tags` | 媒体-标签关联（与 documents.metadata.tags 双源存储，展示需去重） |
| `rag_subscriptions` | 租户订阅（磁盘/文档/知识库/API 调用四类配额与用量） |
| `rag_org_storage_limits` | 机构存储限额 |
| `rag_usage_logs` | 用量日志（计费/审计） |

## embedding_engine 机制

向量化引擎在**创建知识库时选定**（`rag_knowledge_bases.embedding_engine`），库内所有文档统一使用该引擎：

- `bge-m3` — 文本知识库，1024 维，走阿里在线服务；
- `clip-vith14` / `qwen3-vl-embedding` — 多媒体知识库（图/音/视），后者 2560 维。

**GPU 已于 2026-09 退役**，embedding / rerank 全部走阿里在线接口（`rag/vl_online.py`），不再依赖本地 CLIP/BGE GPU 服务。字段默认值 `clip-vith14` 是历史遗留，新建库应显式选择在线引擎。

## 与 uapi 模块的依赖和建表顺序

rag 的外部服务（embedding、rerank、VDB、NER、人脸、图谱、声纹）统一通过宿主的 **uapi 模块**路由，服务地址配置在 `upapp` / `uapi` / `uapiio` 表中。因此**必须先有 uapi 表，才能插入 rag 的服务种子**。

标准 build.sh 顺序：

1. 先建 uapi 模块的表（`upapp`/`uapi`/`uapiio` 等）；
2. 再建 rag 模块的表（`models/*.json`）；
3. 最后导入 `init/uapi_seed.sql`（`INSERT IGNORE` 幂等），写入 `rag-embedding`、`rag-reranker`、`rag-vdb`、`rag-ner`、`rag-face`、`rag-graph`、`rag-voiceprint` 等服务配置。

顺序颠倒会导致 uapi_seed.sql 因目标表不存在而失败。

## VDB 连接配置

VDB（Milvus 向量库）地址**不硬编码**，运行时从 `upapp` 表读取：

```sql
SELECT baseurl FROM upapp WHERE id='rag-vdb';
```

见 `rag/vl_online.py` 的 `vdb_baseurl()` / `vdb_call()`（HTTP 直调 `/v1/createcollection`、`/v1/upsert`、`/v1/search`、`/v1/delete`）。

- 生产内网：`192.168.16.10:9186`
- 测试内网：`192.168.16.10:9187`

`uapi_seed.sql` 中种子默认是公网域名（`vectordb.opencomputing.net:10443`），部署到新环境后需按实际网络把 `upapp.rag-vdb` 的 baseurl 更新为对应内网地址。

## 与 ragserver 的关系

- **ragserver** 是一个宿主**应用**（`/d/rag/ragserver`，进程 `app/ragserver.py`，域名 rag.opencomputing.cn），本仓库作为 `pkgs/rag` 挂载其中；
- 同一份代码也挂载到 **pipeline-app**（`pkgs/rag`，DB `pipeline`，菜单「知识库」）；
- **同代码、不同宿主、各自数据**：每个宿主用自己的库建 rag_ 表和 uapi 种子，数据互不共享。

## 部署注意

1. **RBAC 路径注册**：部署后在宿主根目录执行 `py3/bin/python pkgs/rag/scripts/load_path.py`（pipeline-app 的 load_path.sh 会自动扫描）。对外 B2B API 端点走 cookie 会话或 `Authorization: Bearer <dapi key>` 鉴权，org 隔离在 `rag/api_core.py` 内强制。
2. **新增 .dspy 必须补 RBAC**：`permission` 表 + `rolepermission` 表（`roleid='any'`，外键列名是 `permid`）两条都缺一个就 403，补完重启宿主进程。
3. **建表顺序**：uapi 表 → rag 表 → `uapi_seed.sql`（见上文）。
4. **DSPY 热加载**：宿主 `hot_reload=True` 时改 .dspy 无需重启；改 Python 包（rag/*.py）需重启进程。
5. **旧环境迁移**：无前缀旧表用 `init/migrate_rag_prefix.sql` 幂等改名，不要手工 RENAME。
6. **删除知识库/文档要清全链路**：DB 行、VDB 向量、图数据库、实体/关系、物理文件都要清理，只删 DB 会留脏数据。
7. **sqlor 陷阱**：SQL 中字面 `%` 需写成 `%%`；`IN (${ids}$)` 列表展开在 rag 服务器的 sqlor 版本不可用，需手工拼引号字符串。
8. **代理陷阱**：`StreamHttpClient` 默认走 `socks5://127.0.0.1:1086`，测试/生产机无此代理时直连超时，排查 VDB/在线服务不可达先确认代理。
