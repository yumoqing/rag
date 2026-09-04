# -*- coding:utf-8 -*-
"""RAG 对外 API 核心（B2B 机器接口）— RBAC 会话鉴权 + dapi 平台 key 管理。

认证架构（对齐 sage 范式，rag 不自建 key 体系）：
- API 端点路由权限 = **logined**（见 scripts/load_path.py），鉴权统一收敛在 RBAC 中间件：
  浏览器 cookie 会话 或 `Authorization: Bearer <downapikey>` 都能过门
  （dapi.load_dapi() 注册 'Bearer '→bearer_auth，认证通过后 dapi 建立会话）；
- 未认证 → RBAC 直接 401/403，**不进 dspy**；
- dspy 用 session_env(request) 从请求会话取 org/user，机构隔离在 rag 边界强制。

key 生命周期（生成、过期、IP 白名单）全归平台 dapi 模块
（downapp/downapikey 表 + key 申请管理 UI），rag 只消费认证结果。
downappuser 角色不授 rag 权限，仅作为该调用方的 RBAC 身份。

org 注入方式（session_env）：返回真实请求 _run_ns，get_userorgid/get_user 走会话读取
（auth.remember 的签名 cookie，bearer_auth 在权限校验阶段已建立）—— 与 UI 通道同一
机制，复用 init.py 底层能力（_resolve_search_kbs/_build_search_vector/_call_uapi 等）。

历史：2026-09-03 初版在 dspy 内用 rag 自建 rag_api_keys 表校验 Bearer Key；
按「apikey 统一由 dapi 管理 + 路由至少 logined」重构为本方案，verify_api_key 已删除。
"""
import json
import os
from appPublic.uniqueID import getID

from ahserver.serverenv import ServerEnv
from appPublic.dictObject import DictObject
from sqlor.dbpools import get_sor_context


# ────────────────────────── 会话 env（org/user 取自认证会话） ──────────────────────────

def session_env(request):
    """API 端点的业务 env：直接返回真实请求的运行 env（org 从会话取）。

    未认证请求到不了这里（RBAC logined 在路由层把关）。保留本函数作为统一入口，
    将来需要加 API 侧横切（限流头/审计标签）时只改这里。
    """
    return request._run_ns


async def read_json_body(request, params_kw):
    """三级兜底取请求体：JSON body → raw text JSON → query/form params_kw。"""
    payload = None
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        try:
            raw = await request.text()
            payload = json.loads(raw) if raw and raw.strip().startswith('{') else None
        except Exception:
            payload = None
    if isinstance(payload, dict) and payload:
        return payload
    return dict(params_kw or {})


# ────────────────────────── org 注入 env（内部 tools 通道专用） ──────────────────────────

def make_api_env(ctx):
    """轻量 env：org 由调用方（内部助手宿主）注入，其余透传全局 ServerEnv。

    HTTP API 通道用 session_env(request)（会话身份）；内部 tools 通道没有 HTTP
    请求，宿主助手注入可信 org_id 走本函数。
    """
    g = ServerEnv()

    async def _orgid():
        return ctx['org_id']

    async def _user():
        return ctx.get('user_id') or ''

    return DictObject(
        get_userorgid=_orgid,
        get_user=_user,
        get_module_dbname=g.get_module_dbname,
    )


def _ok(**kw):
    """统一成功格式：{"status":"ok","data":{...}}"""
    return json.dumps({"status": "ok", "data": kw}, ensure_ascii=False, default=str)


def _err(message, **kw):
    """统一错误格式：{"status":"error","message":...,"data":null}"""
    out = {"status": "error", "message": message, "data": None}
    if kw:
        out.update(kw)
    return json.dumps(out, ensure_ascii=False, default=str)


# ────────────────────────── 业务核心（org 来自 key） ──────────────────────────

_ENGINES = ("bge-m3", "clip-vith14", "qwen3-vl-embedding")


async def kb_create(env, ns):
    """创建知识库（embedding_engine 创建时定死，知识库级选：bge-m3文本 / clip-vith14多媒体 / qwen3-vl-embedding多模态在线）。"""
    name = (ns.get('name') or '').strip()
    if not name:
        return _err("name required")
    emb = (ns.get('embedding_engine') or ns.get('embedding_type') or 'bge-m3').strip()
    if emb not in _ENGINES:
        return _err("embedding_engine must be one of " + "/".join(_ENGINES))
    org_id = await env.get_userorgid()
    kb_id = getID()
    from rag.init import get_rags_base, ensure_kb_dir
    async with get_sor_context(env, 'rag') as sor:
        dup = await sor.sqlExe(
            "SELECT id FROM rag_knowledge_bases WHERE org_id=${o}$ AND name=${n}$ LIMIT 1",
            {"o": org_id, "n": name})
        await sor.sqlExe("COMMIT", {})
        if dup:
            return _err("知识库名称已存在: " + name)
        await sor.sqlExe(
            "INSERT INTO rag_knowledge_bases (id, name, description, org_id, embedding_engine, "
            "vdb_collection, doc_count, total_size, chunk_count, status, maintain_roles, search_roles, created_at) "
            "VALUES (${id}$, ${name}$, ${desc}$, ${org_id}$, ${emb}$, 'rag_collection', 0, 0, 0, 'active', '', '', NOW())",
            {"id": kb_id, "name": name, "desc": ns.get('description') or '',
             "org_id": org_id, "emb": emb})
        await sor.sqlExe("COMMIT", {})
        rags_base = await get_rags_base(sor)
    ensure_kb_dir(rags_base, org_id, kb_id)
    return _ok(kb_id=kb_id, name=name, embedding_engine=emb)


async def _kb_owned(sor, env, kb_id):
    org_id = await env.get_userorgid()
    recs = await sor.sqlExe(
        "SELECT * FROM rag_knowledge_bases WHERE id=${k}$ LIMIT 1", {"k": kb_id})
    await sor.sqlExe("COMMIT", {})
    if not recs:
        return None, "知识库不存在"
    kb = recs[0]
    if str(getattr(kb, 'org_id', '') or '') != str(org_id or ''):
        return None, "无权操作其他机构的知识库"
    return kb, None


async def kb_delete(env, ns):
    """删除知识库：VDB 集合级删除 + 图 + DB 记录 + 磁盘文件 全清理。"""
    kb_id = (ns.get('kb_id') or '').strip()
    if not kb_id:
        return _err("kb_id required")
    from rag.init import _call_uapi, get_rags_base
    vdb_err = graph_err = None
    async with get_sor_context(env, 'rag') as sor:
        kb, err = await _kb_owned(sor, env, kb_id)
        if err:
            return _err(err)
        docs = await sor.sqlExe(
            "SELECT id, file_path, file_size FROM rag_documents WHERE kb_id=${k}$", {"k": kb_id})
        docs = list(docs or [])
        doc_ids = [d.id for d in docs]

        # VDB：整集合清理（集合名=kb_id，见 create 与 ingest 约定）
        try:
            chunks = await sor.sqlExe(
                "SELECT vector_id FROM rag_document_chunks WHERE kb_id=${k}$ "
                "AND vector_id IS NOT NULL AND vector_id != ''", {"k": kb_id})
            vids = [c.vector_id for c in (chunks or [])]
            if vids:
                await _call_uapi("rag-vdb", "delete", {"colname": kb_id, "ids": vids})
        except Exception as e:
            vdb_err = str(e)[:200]

        # 图
        try:
            await _call_uapi("rag-graph", "delete", {"graph": kb_id})
        except Exception as e:
            graph_err = str(e)[:200]

        # DB 记录
        if doc_ids:
            await sor.sqlExe("DELETE FROM rag_document_chunks WHERE doc_id IN (${ids}$)", {"ids": doc_ids})
        await sor.sqlExe("DELETE FROM rag_entities WHERE kb_id=${k}$", {"k": kb_id})
        await sor.sqlExe("DELETE FROM rag_entity_relations WHERE kb_id=${k}$", {"k": kb_id})
        await sor.sqlExe("DELETE FROM rag_media_tags WHERE kb_id=${k}$", {"k": kb_id})
        await sor.sqlExe("DELETE FROM rag_tags WHERE kb_id=${k}$", {"k": kb_id})
        await sor.sqlExe("DELETE FROM rag_documents WHERE kb_id=${k}$", {"k": kb_id})
        await sor.sqlExe("DELETE FROM rag_knowledge_bases WHERE id=${k}$", {"k": kb_id})
        await sor.sqlExe("COMMIT", {})
        rags_base = await get_rags_base(sor)

    # 磁盘文件（/rags/ 新布局 + /idfile/ 旧布局兼容删除）
    files_removed = 0
    for d in docs:
        fp = getattr(d, 'file_path', '') or ''
        try:
            if fp.startswith('/rags/'):
                parts = [p for p in fp.split('/') if p]
                if len(parts) >= 4:
                    real = os.path.join(rags_base, parts[1], 'rags', parts[2], parts[3])
                    if os.path.isfile(real):
                        os.remove(real)
                        files_removed += 1
            else:
                g = ServerEnv()
                real = g.realpath(fp) if callable(getattr(g, 'realpath', None)) else None
                if real and os.path.isfile(real):
                    os.remove(real)
                    files_removed += 1
        except Exception:
            pass
    # 知识库目录整体清理（rags/{org}/{kb}/ 空壳）
    try:
        import shutil
        kdir = os.path.join(rags_base, str(await env.get_userorgid() or '0'), 'rags', kb_id)
        if os.path.isdir(kdir):
            shutil.rmtree(kdir, ignore_errors=True)
    except Exception:
        pass
    return _ok(kb_id=kb_id, documents=len(docs), files_removed=files_removed,
               vdb_cleanup=vdb_err or 'done', graph_cleanup=graph_err or 'done')


async def doc_delete(env, ns):
    """删除文档：VDB 向量 → chunks/entities DB → 磁盘文件 → KB 统计回退。"""
    doc_id = (ns.get('doc_id') or '').strip()
    if not doc_id:
        return _err("doc_id required")
    from rag.init import _call_uapi, get_rags_base
    async with get_sor_context(env, 'rag') as sor:
        recs = await sor.R("rag_documents", {"id": doc_id})
        if not recs:
            return _err("document not found")
        doc = recs[0]
        _kb, err = await _kb_owned(sor, env, doc.kb_id)
        if err:
            return _err(err)
        chunks = await sor.R("rag_document_chunks", {"doc_id": doc_id})
        vids = [c.vector_id for c in (chunks or []) if getattr(c, 'vector_id', '')]
        if vids:
            try:
                await _call_uapi("rag-vdb", "delete", {"colname": doc.kb_id, "ids": vids})
            except Exception:
                pass
        await sor.sqlExe("DELETE FROM rag_document_chunks WHERE doc_id=${i}$", {"i": doc_id})
        await sor.sqlExe("DELETE FROM rag_media_tags WHERE media_type='document' AND media_id=${i}$", {"i": doc_id})
        await sor.sqlExe("DELETE FROM rag_documents WHERE id=${i}$", {"i": doc_id})
        await sor.sqlExe(
            "UPDATE rag_knowledge_bases SET doc_count=GREATEST(doc_count-1,0), "
            "total_size=GREATEST(total_size-${s}$,0), chunk_count=GREATEST(chunk_count-${n}$,0) "
            "WHERE id=${k}$",
            {"s": getattr(doc, 'file_size', 0) or 0, "n": len(chunks or []), "k": doc.kb_id})
        await sor.sqlExe("COMMIT", {})
        rags_base = await get_rags_base(sor)
        kb_id, file_path = doc.kb_id, getattr(doc, 'file_path', '') or ''

    removed = False
    if file_path.startswith('/rags/'):
        parts = [p for p in file_path.split('/') if p]
        if len(parts) >= 4:
            real = os.path.join(rags_base, parts[1], 'rags', parts[2], parts[3])
            if os.path.isfile(real):
                os.remove(real)
                removed = True
    else:
        try:
            g = ServerEnv()
            real = g.realpath(file_path) if callable(getattr(g, 'realpath', None)) else None
            if real and os.path.isfile(real):
                os.remove(real)
                removed = True
        except Exception:
            pass
    return _ok(doc_id=doc_id, kb_id=kb_id, chunks_deleted=len(chunks or []), file_removed=removed)


async def tag_create(env, ns):
    """创建标签（同名幂等返回已有标签）。"""
    kb_id = (ns.get('kb_id') or '').strip()
    name = (ns.get('name') or '').strip()
    color = (ns.get('color') or '#3b82f6').strip()
    if not kb_id or not name:
        return _err("kb_id and name required")
    async with get_sor_context(env, 'rag') as sor:
        _kb, err = await _kb_owned(sor, env, kb_id)
        if err:
            return _err(err)
        org_id = await env.get_userorgid()
        existing = await sor.sqlExe(
            "SELECT id, name, color FROM rag_tags WHERE kb_id=${k}$ AND name=${n}$ AND org_id=${o}$",
            {"k": kb_id, "n": name, "o": org_id})
        if existing:
            r = existing[0]
            return _ok(tag_id=r.id, name=r.name, color=r.color, duplicate=True)
        tag_id = getID()
        await sor.sqlExe(
            "INSERT INTO rag_tags (id, kb_id, name, color, org_id, created_at) "
            "VALUES (${id}$, ${k}$, ${n}$, ${c}$, ${o}$, NOW())",
            {"id": tag_id, "k": kb_id, "n": name, "c": color, "o": org_id})
        await sor.sqlExe("COMMIT", {})
    return _ok(tag_id=tag_id, name=name, color=color)


def _as_list(v):
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v or '').split(',') if s.strip()]


async def doc_set_tags(env, ns):
    """文件设置标签（全量语义：传入的即最终集合，多余的删、缺的补）。

    入参：kb_id, doc_id, tags（名称数组或逗号串）或 tag_ids（id 数组或逗号串）。
    标签不存在时自动在本知识库创建。
    """
    kb_id = (ns.get('kb_id') or '').strip()
    doc_id = (ns.get('doc_id') or '').strip()
    if not kb_id or not doc_id:
        return _err("kb_id and doc_id required")
    tag_ids_in = _as_list(ns.get('tag_ids'))
    tag_names = _as_list(ns.get('tags'))
    async with get_sor_context(env, 'rag') as sor:
        _kb, err = await _kb_owned(sor, env, kb_id)
        if err:
            return _err(err)
        docs = await sor.sqlExe(
            "SELECT id FROM rag_documents WHERE id=${d}$ AND kb_id=${k}$", {"d": doc_id, "k": kb_id})
        if not docs:
            return _err("document not found in this kb")
        org_id = await env.get_userorgid()

        # 名称 → id（不存在自动建）
        for nm in tag_names:
            recs = await sor.sqlExe(
                "SELECT id FROM rag_tags WHERE kb_id=${k}$ AND name=${n}$", {"k": kb_id, "n": nm})
            if recs:
                tag_ids_in.append(recs[0].id)
            else:
                tid = getID()
                await sor.sqlExe(
                    "INSERT INTO rag_tags (id, kb_id, name, color, org_id, created_at) "
                    "VALUES (${id}$, ${k}$, ${n}$, '#3b82f6', ${o}$, NOW())",
                    {"id": tid, "k": kb_id, "n": nm, "o": org_id})
                tag_ids_in.append(tid)
        wanted = list(dict.fromkeys(tag_ids_in))  # 去重保序

        # 校验 tag_id 属于本知识库（wanted 为空=清空全部标签，合法）
        if wanted:
            nsmap = {("t%d" % i): t for i, t in enumerate(wanted)}
            placeholders = ",".join("${" + k + "}$" for k in nsmap)
            recs = await sor.sqlExe(
                "SELECT id FROM rag_tags WHERE id IN (" + placeholders + ")", nsmap)
            valid = {r.id for r in (recs or [])}
            bad = [t for t in wanted if t not in valid]
            if bad:
                return _err("tag not found in this kb: " + ",".join(bad))

        cur = await sor.sqlExe(
            "SELECT id, tag_id FROM rag_media_tags WHERE media_type='document' AND media_id=${d}$",
            {"d": doc_id})
        current = {r.tag_id: r.id for r in (cur or [])}
        removed = added = 0
        for tid, mt_id in current.items():
            if tid not in wanted:
                await sor.sqlExe("DELETE FROM rag_media_tags WHERE id=${i}$", {"i": mt_id})
                removed += 1
        for tid in wanted:
            if tid not in current:
                await sor.sqlExe(
                    "INSERT INTO rag_media_tags (id, kb_id, media_type, media_id, tag_id, created_at) "
                    "VALUES (${id}$, ${k}$, 'document', ${d}$, ${t}$, NOW())",
                    {"id": getID(), "k": kb_id, "d": doc_id, "t": tid})
                added += 1
        await sor.sqlExe("COMMIT", {})
        # 回读最终标签列表
        recs = await sor.sqlExe(
            "SELECT t.id, t.name, t.color FROM rag_media_tags mt JOIN rag_tags t ON mt.tag_id=t.id "
            "WHERE mt.media_type='document' AND mt.media_id=${d}$", {"d": doc_id})
        tags = [{"id": r.id, "name": r.name, "color": r.color} for r in (recs or [])]
    return _ok(doc_id=doc_id, added=added, removed=removed, tags=tags)


async def doc_upload(env, ns, file_data, file_name):
    """文件上传：落盘 {workspace_base}/{org}/rags/{kb}/ + 文档记录 + 后台入库（与 UI 同一引擎 rag.ingest）。"""
    kb_id = (ns.get('kb_id') or '').strip()
    if not kb_id:
        return _err("kb_id required")
    if not file_data:
        return _err("empty file body")
    file_name = file_name or 'upload.bin'
    org_id = await env.get_userorgid()
    from rag.init import get_rags_base, ensure_kb_dir, _detect_file_type, _get_param_value, _fmt_bytes
    from ahserver.globalEnv import background_reco
    from rag.ingest import ingest_one

    async with get_sor_context(env, 'rag') as sor:
        _kb, err = await _kb_owned(sor, env, kb_id)
        if err:
            return _err(err)
        check_quota = str(await _get_param_value(sor, 'rag_check_storage_quota', '0')).strip().lower()
        quota_msg = None
        if check_quota in ('1', 'true', 'yes', 'on'):
            rec = await sor.sqlExe(
                "SELECT COALESCE(SUM(file_size),0) AS used FROM rag_documents WHERE org_id=${o}$",
                {"o": org_id})
            used = int(rec[0].used) if rec else 0
            lim = await sor.sqlExe(
                "SELECT limit_bytes FROM rag_org_storage_limits WHERE org_id=${o}$", {"o": org_id})
            quota = int(lim[0].limit_bytes) if lim else 104857600
            if used + len(file_data) > quota:
                quota_msg = ("存储配额超限：机构已用 " + _fmt_bytes(used) + "，限额 "
                             + _fmt_bytes(quota) + "，本文件 " + _fmt_bytes(len(file_data)))
        if quota_msg:
            return _err(quota_msg, code="storage_quota_exceeded")
        rags_base = await get_rags_base(sor)

    doc_id = getID()
    kb_dir = ensure_kb_dir(rags_base, org_id, kb_id)
    safe_name = file_name.replace('/', '_').replace('\\', '_')
    disk_name = doc_id[:8] + '_' + safe_name
    with open(os.path.join(kb_dir, disk_name), 'wb') as f:
        f.write(file_data)
    web_path = '/rags/' + str(org_id or '0') + '/' + str(kb_id) + '/' + disk_name
    file_type = _detect_file_type(file_name, "application/octet-stream")

    async with get_sor_context(env, 'rag') as sor:
        await sor.sqlExe(
            "INSERT INTO rag_documents (id, kb_id, folder_id, file_name, file_type, file_size, "
            "file_path, mime_type, status, chunk_count, metadata, org_id, created_at, updated_at) "
            "VALUES (${id}$, ${k}$, '', ${fn}$, 'other', ${sz}$, ${fp}$, 'application/octet-stream', "
            "'pending', 0, '{}', ${o}$, NOW(), NOW())",
            {"id": doc_id, "k": kb_id, "fn": safe_name, "sz": len(file_data),
             "fp": web_path, "o": org_id})
        await sor.sqlExe(
            "UPDATE rag_knowledge_bases SET doc_count=doc_count+1, total_size=total_size+${s}$ WHERE id=${k}$",
            {"s": len(file_data), "k": kb_id})
        await sor.sqlExe("COMMIT", {})

    ext = ('.' + file_name.rsplit('.', 1)[1]) if '.' in file_name else '.bin'
    background_reco(ingest_one, doc_id, kb_id, safe_name, ext.lower(),
                    os.path.join(kb_dir, disk_name))
    return _ok(doc_id=doc_id, kb_id=kb_id, file_name=safe_name, file_size=len(file_data),
               status='pending', ingest='running_in_background')


async def search(env, ns):
    """知识库检索：query → embedding → 多 KB VDB 召回 → 重排 → 富化元数据。

    与 UI 检索共用底层能力（_resolve_search_kbs/_build_search_vector/_online_rerank），
    kb_id 缺省检索本机构全部知识库。
    """
    from rag.init import (_resolve_search_kbs, _build_search_vector, _parse_vdb_hits,
                          _apply_rerank, _enrich_search_results, _online_rerank, _call_uapi)
    org_id = await env.get_userorgid()
    query = (ns.get('query') or '').strip()
    kb_id = (ns.get('kb_id') or '').strip()
    try:
        top_k = int(ns.get('top_k') or 10)
    except (TypeError, ValueError):
        top_k = 10
    try:
        recall_k = int(ns.get('recall_k') or top_k * 3)
    except (TypeError, ValueError):
        recall_k = top_k * 3
    if not query:
        return _err("query required")

    kb_ids = await _resolve_search_kbs(env, org_id, kb_id)
    if not kb_ids:
        return _ok(results=[], total=0, message="no knowledge bases visible to this key")

    query_vec = await _build_search_vector(query, None, None, env, kb_id)
    if not query_vec:
        return _err("向量化失败：检查 embedding 引擎配置", code="embed_unavailable")

    all_hits = []
    for kid in kb_ids:
        try:
            vdb_resp = await _call_uapi("rag-vdb", "search", {
                "collection": kid, "vector": query_vec, "topK": recall_k})
            all_hits.extend(_parse_vdb_hits(vdb_resp, kid))
        except Exception:
            pass

    seen = set()
    unique = []
    for h in sorted(all_hits, key=lambda x: x.get("score", 0), reverse=True):
        hid = h.get("id", h.get("text", ""))
        if hid not in seen:
            seen.add(hid)
            unique.append(h)

    if unique:
        documents = [h.get("text", h.get("content", "")) for h in unique[:recall_k]]
        rerank_resp = await _online_rerank(env, query, documents)
        if rerank_resp:
            unique = _apply_rerank(unique[:recall_k], rerank_resp)

    final = unique[:top_k]
    enriched = await _enrich_search_results(env, final)
    # 输出瘦身：分片正文 + 分数 + 来源文档
    results = [{
        "chunk_id": h.get("id", ""),
        "text": h.get("text", h.get("content", "")),
        "score": h.get("rerank_score", h.get("score", 0)),
        "kb_id": h.get("kb_id", ""),
        "document": {k: v for k, v in (h.get("document") or {}).items()
                     if k in ("id", "file_name", "file_type", "kb_id")},
    } for h in enriched]
    return _ok(results=results, total=len(results),
               recall=len(all_hits), kbs_searched=len(kb_ids))


async def kb_list(env, ns):
    """列出调用者（按会话/Key 身份）可见的知识库：机构隔离 + search_roles 过滤。

    与检索同一权限解析（_resolve_search_kbs 缺省返回全部可见库），供宿主
    Agent 在检索/建议入库前先枚举可用知识库。
    """
    from rag.init import _resolve_search_kbs
    org_id = await env.get_userorgid()
    kb_ids = await _resolve_search_kbs(env, org_id, "")
    if not kb_ids:
        return _ok(kbs=[], total=0)
    async with get_sor_context(env, 'rag') as sor:
        nsmap = {("k%d" % i): k for i, k in enumerate(kb_ids)}
        placeholders = ",".join("${" + k + "}$" for k in nsmap)
        recs = await sor.sqlExe(
            "SELECT id, name, description, embedding_engine, doc_count, status "
            "FROM rag_knowledge_bases WHERE id IN (" + placeholders + ") "
            "ORDER BY created_at DESC", nsmap)
        await sor.sqlExe("COMMIT", {})
    kbs = [{
        "id": r.id, "name": getattr(r, 'name', '') or '',
        "description": getattr(r, 'description', '') or '',
        "embedding_engine": getattr(r, 'embedding_engine', '') or '',
        "doc_count": int(getattr(r, 'doc_count', 0) or 0),
        "status": getattr(r, 'status', '') or '',
    } for r in (recs or [])]
    return _ok(kbs=kbs, total=len(kbs))
