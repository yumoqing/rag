# RAG 对外 API

供其他系统（非 Bricks 前端）以 HTTP JSON 调用的知识库接口。与 UI 通道共用同一套
RBAC 鉴权、机构隔离与底层入库/检索实现（`rag/api_core.py` + `rag/ingest.py`）。

## 鉴权

二选一，均由 RBAC 中间件统一把关（端点注册为 `logined` 权限）：

1. **Cookie 会话**：先 `POST /rbac/user/up_login.dspy` 登录，携带 `AIOHTTP_SESSION` cookie。
2. **Bearer API Key**：`Authorization: Bearer <key>`。
   Key 由平台 **dapi 模块**统一管理（`downapp` + `downapikey` 表），不在本模块自建。
   发放方式：
   - UI：API Key 管理页（dapi 的 `apikey_manage.ui`）为目标用户按 downapp 创建；
   - 代码：`from dapi import create_user_apikey` → 传入 `(sor, dappid, user_id, user_orgid)`，
     返回 `{'status','apikey',...}`。`create_user_apikey` 内部按 `(dappid,userid)` 幂等，
     已存在则返回现有 key。
   - 校验链：`register_auth_method('Bearer ', bearer_auth)` 在 `load_dapi()` 时挂载；
     key 命中后解析出绑定的 RBAC 用户与机构，**org 身份与 UI 通道完全一致**
     （`session_env(request)` 统一注入），因此知识库/标签/文档天然按机构隔离。

匿名请求返回 401；跨机构访问他人 kb_id 返回业务 error（查不到即拒绝，不泄露存在性）。

## 通用返回格式

```json
{"status": "ok",    "data": { ... }}
{"status": "error", "message": "原因", "data": null}
```

HTTP 状态码恒为 200（除鉴权失败 401），业务成败看 `status` 字段。

## 端点一览（POST，参数走 JSON body，也兼容 query）

| 端点 | 参数 | 返回 data |
|---|---|---|
| `/rag/api/kb_create.dspy` | `name`*, `description?`, `embedding_engine?`(默认 bge-m3) | `{kb_id, name, embedding_engine}` |
| `/rag/api/kb_delete.dspy` | `kb_id`* | `{kb_id, documents, files_removed, ...}`（级联删 chunks/向量/文件） |
| `/rag/api/doc_upload.dspy` | query: `kb_id`*, `file_name`*, `folder_id?`；body=文件**原始字节**（Content-Type: application/octet-stream） | `{doc_id, kb_id, file_name, file_size, status:"pending", ingest:"running_in_background"}` |
| `/rag/api/doc_delete.dspy` | `doc_id`* | `{doc_id, kb_id, chunks_deleted, file_removed}` |
| `/rag/api/tag_create.dspy` | `kb_id`*, `name`*, `color?` | `{tag_id, name, color}`（同名幂等返回已有并带 `duplicate:true`） |
| `/rag/api/doc_set_tags.dspy` | `kb_id`*, `doc_id`*, `tags`(名称列表) \| `tag_ids`(ID列表) | `{doc_id, added, removed, tags:[{id,name,color}]}`（全量语义，空数组=清空） |
| `/rag/api/search.dspy` | `query`*, `kb_id?`(缺省=本机构全部KB), `top_k?`(默认10), `recall_k?`(默认top_k*3) | `{results:[{chunk_id, text, score, kb_id, doc:{id,file_name,file_type,kb_id}}], total, recall, kbs_searched}` |

`*` 为必填。缺失/非法一律返回 `{"status":"error","message":...}`，不会产生 500。

## 调用示例

```bash
BASE=https://<host>/rag/api
KEY=*** 1. 建知识库
curl -s -X POST $BASE/kb_create.dspy -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"name":"标书资料库","embedding_engine":"bge-m3"}'

# 2. 上传文件（原始字节；kb_id/file_name 走 query）
curl -s -X POST "$BASE/doc_upload.dspy?kb_id=$KB&file_name=tender.pdf" \
     -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/octet-stream' \
     --data-binary @tender.pdf

# 3. 检索（上传后等待后台入库完成，status 变 done 才可被召回）
curl -s -X POST $BASE/search.dspy -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"query":"投标人资质要求","kb_id":"'$KB'","top_k":5}'

# 4. 标签：创建 + 全量设置（tags 里的新名字自动建标签；空数组=清空）
curl -s -X POST $BASE/tag_create.dspy -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"kb_id":"'$KB'","name":"技术标"}'
curl -s -X POST $BASE/doc_set_tags.dspy -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"kb_id":"'$KB'","doc_id":"'$DOC'","tags":["技术标","2026"]}'

# 5. 删除
curl -s -X POST $BASE/doc_delete.dspy -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"doc_id":"'$DOC'"}'
curl -s -X POST $BASE/kb_delete.dspy -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"kb_id":"'$KB'"}'
```

## 入库与检索说明

- `doc_upload` 立即返回（`status:"pending"`），后台 asyncio 任务完成
  解析→分块→向量化→VDB upsert→chunks 落库；成功置 `done`，
  任何失败置 `failed` 并把真实原因写 `rag_documents.error_msg`（不静默）。
  文本类（txt/md/pdf/docx/pptx/xlsx/csv…）可被检索召回；图片/音视频走多模态/embedding 引擎配置。
- 检索向量引擎按知识库 `embedding_engine` 选择（`bge-m3` 在线文本 / `qwen3-vl-embedding` 在线多模态），
  配置在 `rag_engine_configs`（endpoint + RC4 加密 key）。召回后自动重排（若 rerank 引擎可用）。
- 依赖基础设施：embedding/rerank 在线 API 与 VDB 服务
  （`upapp.rag-vdb` 的 baseurl）必须从部署机网络可达；不可达时 ingest/search 会明确报错而非假成功。

## 内部助手（非 HTTP）

同一套能力封装为 OpenAI function-calling schema 供宿主 Agent 直调，不绕 HTTP：
见 `rag/tools.py`（`rag_kb_create` 等 7 个）。宿主 env 需具备
`db` / `get_user` / `get_userorgid` / `password_encode` / `get_module_dbname`；
org 由宿主注入，与 API 通道的机构隔离语义一致。
