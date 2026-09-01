# -*- coding:utf-8 -*-
"""
RagServer DSPY Handlers - RAG 核心业务逻辑 + 文件上传/删除
"""
from traceback import format_exc
from ahserver.serverenv import ServerEnv
from appPublic.registerfunction import RegisterFunction
from appPublic.log import debug, exception
from sqlor.dbpools import get_sor_context
import json, os, uuid, time


async def status_handler(request, params_kw, *args, **kwargs):
    return json.dumps({
        "service": "ragserver", "version": "0.1.0",
        "endpoints": ["/api/status", "/api/kb/list", "/api/doc/upload",
                      "/api/doc/delete", "/api/search", "/api/engines",
                      "/api/dir/create", "/api/dir/delete", "/api/dir/list",
                      "/api/tag/create", "/api/tag/list", "/api/tag/delete",
                      "/api/tag/assign", "/api/tag/unassign", "/api/tag/media_tags",
                      "/api/tag/search"]
    }, indent=2, ensure_ascii=False)


async def kb_list_handler(request, params_kw, *args, **kwargs):
    env = request._run_ns
    try:
        userorgid = await env.get_userorgid()
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.R("rag_knowledge_bases", {})
            cards = []
            for r in recs:
                doc_recs = await sor.R("rag_documents", {"kb_id": r.id})
                doc_count = len(doc_recs)
                total_size = r.total_size or 0
                if total_size >= 1073741824:
                    size_str = f"{total_size/1073741824:.1f}GB"
                elif total_size >= 1048576:
                    size_str = f"{total_size/1048576:.0f}MB"
                elif total_size >= 1024:
                    size_str = f"{total_size/1024:.0f}KB"
                else:
                    size_str = f"{total_size}B"
                engine = {'bge-m3': '文本·在线', 'qwen3-vl-embedding': '多媒体·在线', 'clip-vith14': '多媒体·GPU CLIP', 'CLIP ViT-H-14': '多媒体·GPU CLIP'}.get((r.embedding_engine or '').strip(), (r.embedding_engine or '未配置'))
                dim = 2560 if (r.embedding_engine or '').strip() == 'qwen3-vl-embedding' else 1024
                card = {"widgettype":"VBox","options":{"cwidth":16,"cheight":12,"bgcolor":"#f0f7ff","padding":"16px","css":"card clickable","border":"1px solid #d0e4f7"},"subwidgets":[
                    {"widgettype":"Text","options":{"text":"📚 " + str(r.name),"cfontsize":16,"fontWeight":"bold"}},
                    {"widgettype":"Text","options":{"text":str(engine)+f" · {dim}维","cfontsize":12,"color":"#888"}},
                    {"widgettype":"HBox","options":{"spacing":"12px"},"subwidgets":[
                        {"widgettype":"Text","options":{"text":"📄 "+str(doc_count)+"文档","cfontsize":12,"color":"#666"}},
                        {"widgettype":"Text","options":{"text":"💾 "+size_str,"cfontsize":12,"color":"#666"}}]}],
                    "binds":[{"wid":"self","event":"click","actiontype":"urlwidget","target":"app.rag_main_content","mode":"replace","options":{"url":"/rag/knowledge_bases_list/detail.ui","params":{"kb_id":str(r.id),"kb_name":str(r.name)}}}]}
                cards.append(card)
            if not cards:
                cards.append({"widgettype":"Text","options":{"text":"暂无知识库","color":"#aaa","cfontsize":14,"halign":"center"}})
            result = {"widgettype":"HBox","options":{"width":"100%","spacing":"12px","wrap":True},"subwidgets":cards}
            return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        exception(f"kb_list: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def engines_handler(request, params_kw, *args, **kwargs):
    env = request._run_ns
    try:
        userorgid = await env.get_userorgid()
        async with get_sor_context(env, 'rag') as sor:
            sql = "SELECT * FROM rag_engine_configs WHERE status='active' AND (org_id IS NULL OR org_id=${org_id}$) ORDER BY engine_type, priority DESC"
            recs = await sor.sqlExe(sql, {"org_id": userorgid})
            rows = [dict(r) for r in recs]
            return json.dumps({"status": "SUCCEEDED", "data": {"rows": rows, "total": len(rows)}}, ensure_ascii=False, default=str)
    except Exception as e:
        exception(f"engines: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def search_handler(request, params_kw, *args, **kwargs):
    """统一检索：文本 + 多媒体(图/音/视/文) → embedding → VDB → 重排 → 返回"""
    env = request._run_ns
    try:
        userorgid = await env.get_userorgid()
        query = (params_kw.get("query") or "").strip()
        kb_id = (params_kw.get("kb_id") or "").strip()
        top_k = int(params_kw.get("top_k", 10))
        recall_k = int(params_kw.get("recall_k", top_k * 3))

        # Resolve KBs
        kb_ids = await _resolve_search_kbs(env, userorgid, kb_id)
        if not kb_ids:
            return json.dumps({"status": "SUCCEEDED", "data": {"results": [], "total": 0, "message": "no knowledge bases"}}, ensure_ascii=False)

        # Read uploaded file if present
        file_data = None
        file_name = None
        content_type = request.headers.get("Content-Type", "")
        if "multipart" in content_type:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "file" and part.filename:
                    file_data = await part.read()
                    file_name = part.filename
                    break
        if not file_data:
            # Try raw body as file
            body = await request.read()
            if body and len(body) > 10:
                # Check if it's a form-encoded request
                if not query and b'=' in body[:100]:
                    pass  # form data, not a file
                elif not body.startswith(b'{') and not body.startswith(b'['):
                    file_data = body
                    file_name = params_kw.get("file_name", "search_upload")

        # Process query + media → embedding vector
        query_vec = None
        if query or file_data:
            query_vec = await _build_search_vector(query, file_data, file_name, env, kb_id)

        if not query_vec:
            return json.dumps({"status": "SUCCEEDED", "data": {"results": [], "total": 0, "message": "no query or file provided"}}, ensure_ascii=False)

        # Multi-KB VDB search
        all_hits = []
        for kid in kb_ids:
            try:
                vdb_resp = await _call_uapi("rag-vdb", "search", {
                    "collection": kid,
                    "vector": query_vec,
                    "topK": recall_k
                })
                hits = _parse_vdb_hits(vdb_resp, kid)
                all_hits.extend(hits)
            except Exception as e:
                exception(f"vdb search kb={kid}: {e}")

        # Deduplicate + sort by score
        seen = set()
        unique_hits = []
        for h in sorted(all_hits, key=lambda x: x.get("score", 0), reverse=True):
            hid = h.get("id", h.get("text", ""))
            if hid not in seen:
                seen.add(hid)
                unique_hits.append(h)

        # Rerank if query text provided（在线 rerank；未配置则保持召回分排序）
        if query and unique_hits:
            documents = [h.get("text", h.get("content", "")) for h in unique_hits[:recall_k]]
            rerank_resp = await _online_rerank(env, query, documents)
            if rerank_resp:
                unique_hits = _apply_rerank(unique_hits[:recall_k], rerank_resp)

        # Limit + enrich with DB metadata
        final = unique_hits[:top_k]
        enriched = await _enrich_search_results(env, final)

        return json.dumps({
            "status": "SUCCEEDED",
            "data": {"results": enriched, "total": len(enriched),
                     "recall": len(all_hits), "kbs_searched": len(kb_ids)}
        }, ensure_ascii=False, default=str)

    except Exception as e:
        exception(f"search: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def _resolve_search_kbs(env, userorgid, kb_id):
    """Resolve KB IDs: specific or all org KBs"""
    async with get_sor_context(env, 'rag') as sor:
        if kb_id:
            recs = await sor.R("rag_knowledge_bases", {"id": kb_id})
            return [r.id for r in recs]
        # All org KBs (global + org-specific)
        sql = "SELECT id FROM rag_knowledge_bases WHERE org_id IS NULL OR org_id=${org_id}$"
        recs = await sor.sqlExe(sql, {"org_id": userorgid})
        return [r.id for r in recs]


async def _build_search_vector(query, file_data, file_name, env=None, kb_id=''):
    """Build search embedding from text query + media file（按 kb 向量引擎选端点）"""
    texts = []
    if query:
        texts.append(query)

    if file_data and file_name:
        ext = os.path.splitext(file_name)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'):
            # Image → embed as-is (CLIP multimodal)
            texts.append(f"[IMAGE:{file_name}]")
        elif ext in ('.txt', '.md', '.json', '.csv', '.html', '.py'):
            # Text file → extract content
            try:
                content = file_data.decode("utf-8", errors="replace")[:4000]
                texts.append(content)
            except Exception:
                pass
        elif ext in ('.mp3', '.wav', '.flac', '.ogg'):
            # Audio → placeholder (would need ASR service)
            texts.append(f"[AUDIO:{file_name}]")
        elif ext in ('.mp4', '.avi', '.mov', '.mkv'):
            texts.append(f"[VIDEO:{file_name}]")
        else:
            # Try as text
            try:
                texts.append(file_data.decode("utf-8", errors="replace")[:2000])
            except Exception:
                pass

    if not texts:
        return None

    combined = " ".join(texts)
    vecs = await _online_embed(env, [combined])
    return vecs[0] if vecs else None


def _parse_vdb_hits(vdb_resp, kb_id):
    """Parse VDB search response into uniform hit format"""
    hits = []
    data = vdb_resp
    if isinstance(data, dict):
        for key in ("results", "data", "hits", "rows"):
            candidates = data.get(key)
            if isinstance(candidates, list):
                data = candidates
                break
    if not isinstance(data, list):
        return hits
    for item in data:
        if isinstance(item, dict):
            hits.append({
                "id": item.get("id", item.get("doc_id", "")),
                "text": item.get("text", item.get("content", "")),
                "score": item.get("score", item.get("distance", 0)),
                "kb_id": kb_id,
                "metadata": item.get("metadata", item.get("meta", {}))
            })
    return hits


def _apply_rerank(hits, rerank_resp):
    """Apply reranker scores to reorder hits"""
    scores = []
    if isinstance(rerank_resp, dict):
        scores = rerank_resp.get("scores", rerank_resp.get("results", []))
    if isinstance(scores, list) and len(scores) == len(hits):
        for i, s in enumerate(scores):
            if isinstance(s, dict):
                hits[i]["rerank_score"] = s.get("score", s.get("relevance_score", 0))
            else:
                hits[i]["rerank_score"] = float(s) if s else 0
        hits.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
    return hits


async def _enrich_search_results(env, hits):
    """Enrich hits with document metadata from DB"""
    doc_ids = list(set(h.get("id", "") for h in hits if h.get("id")))
    if not doc_ids:
        return hits

    async with get_sor_context(env, 'rag') as sor:
        recs = await sor.sqlExe(
            "SELECT id, file_name, file_type, file_size, status, kb_id, created_at "
            "FROM rag_documents WHERE id IN (" + ",".join(repr(d) for d in doc_ids) + ")", {})
        doc_map = {r.id: dict(r) for r in recs}

    for h in hits:
        did = h.get("id", "")
        if did in doc_map:
            h["document"] = doc_map[did]
    return hits


def _fmt_bytes(n):
    if n < 1024: return str(n) + 'B'
    if n < 1048576: return str(round(n/1024, 1)) + 'KB'
    return str(round(n/1048576, 1)) + 'MB'


async def doc_upload_handler(request, params_kw, *args, **kwargs):
    """文件上传 → 保存 → DB记录 → 触发RAG入库"""
    env = request._run_ns
    try:
        userorgid = await env.get_userorgid()
        kb_id = params_kw.get("kb_id", "")
        file_name = params_kw.get("file_name", "upload.bin")
        if not kb_id:
            return json.dumps({"error": "kb_id required"})

        # Read raw file data from request body
        file_data = await request.read()

        if not file_data:
            return json.dumps({"error": "no file data"})

        file_size = len(file_data)

        # ---- ORG STORAGE QUOTA CHECK (per-org limit, not global) ----
        quota_limit = 104857600
        used = 0
        async with get_sor_context(env, 'rag') as sor:
            rec = await sor.sqlExe("SELECT COALESCE(SUM(file_size),0) AS used FROM rag_documents WHERE org_id=${org_id}$", {"org_id": userorgid})
            if rec: used = int(rec[0].used)
            lim = await sor.sqlExe("SELECT limit_bytes FROM rag_org_storage_limits WHERE org_id=${org_id}$", {"org_id": userorgid})
            if lim: quota_limit = int(lim[0].limit_bytes)
        if used + file_size > quota_limit:
            return json.dumps({"error": "storage_quota_exceeded",
                "message": "存储配额超限：机构已用 " + _fmt_bytes(used) + "，限额 " + _fmt_bytes(quota_limit) + "，本文件 " + _fmt_bytes(file_size)}, ensure_ascii=False)

        # Save file via FileStorage (layered dir layout, returns web path e.g. /191/193/197/97/xxx.txt)
        doc_id = uuid.uuid4().hex
        web_path = await env.save_file(file_data, file_name)

        file_type = _detect_file_type(file_name, "application/octet-stream")

        # Create document record
        async with get_sor_context(env, 'rag') as sor:
            await sor.sqlExe(
                "INSERT INTO rag_documents (id, kb_id, file_name, file_type, file_size, file_path, mime_type, status, org_id, created_at, updated_at) "
                "VALUES (${id}$, ${kb_id}$, ${file_name}$, ${file_type}$, ${file_size}$, ${file_path}$, ${mime_type}$, 'pending', ${org_id}$, NOW(), NOW())",
                {"id": doc_id, "kb_id": kb_id, "file_name": file_name, "file_type": file_type,
                 "file_size": file_size, "file_path": web_path,
                 "mime_type": "application/octet-stream", "org_id": userorgid})

            # Update KB stats
            await sor.sqlExe(
                "UPDATE rag_knowledge_bases SET doc_count=doc_count+1, total_size=total_size+${size}$ WHERE id=${kb_id}$",
                {"size": file_size, "kb_id": kb_id})

        # Trigger async ingest for text-based files via uapi
        ingest_result = None
        if file_type == "text":
            try:
                text = file_data.decode("utf-8", errors="replace")
                ingest_result = await _rag_ingest_async(env, text, kb_id, doc_id)
                # Update status to done
                async with get_sor_context(env, 'rag') as sor:
                    chunks_n = ingest_result.get("chunks", 0) if ingest_result else 0
                    await sor.sqlExe(
                        "UPDATE rag_documents SET status='done', chunk_count=${chunks}$ WHERE id=${id}$",
                        {"chunks": chunks_n, "id": doc_id})
                    if chunks_n:
                        await sor.sqlExe(
                            "UPDATE rag_knowledge_bases SET chunk_count=chunk_count+${n}$ WHERE id=${kb_id}$",
                            {"n": chunks_n, "kb_id": kb_id})
            except Exception as e:
                exception(f"uapi ingest failed: {e}")
                async with get_sor_context(env, 'rag') as sor:
                    await sor.sqlExe(
                        "UPDATE rag_documents SET status='error' WHERE id=${id}$", {"id": doc_id})

        return json.dumps({
            "status": "SUCCEEDED",
            "doc_id": doc_id,
            "file_name": file_name,
            "file_size": file_size,
            "file_type": file_type,
            "ingest": ingest_result
        }, ensure_ascii=False, default=str)

    except Exception as e:
        exception(f"doc_upload: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def doc_delete_handler(request, params_kw, *args, **kwargs):
    """文件删除 → 清理VDB → 清理图 → 清理DB → 删文件"""
    env = request._run_ns
    try:
        doc_id = params_kw.get("doc_id", "")
        if not doc_id:
            return json.dumps({"error": "doc_id required"})

        async with get_sor_context(env, 'rag') as sor:
            # Get document info
            recs = await sor.R("rag_documents", {"id": doc_id})
            if not recs:
                return json.dumps({"error": "document not found"})
            doc = recs[0]

            # Get chunks to clean VDB
            chunks = await sor.R("rag_document_chunks", {"doc_id": doc_id})

            # Delete from VDB
            if chunks:
                vector_ids = [c.vector_id for c in chunks if c.vector_id]
                if vector_ids:
                    try:
                        await _call_uapi("rag-vdb", "delete",
                                         {"colname": doc.kb_id, "ids": vector_ids})
                    except Exception as e:
                        exception(f"vdb delete failed: {e}")

            # Delete entities from graph
            try:
                await _call_uapi("rag-graph", "delete", {"graph": doc.kb_id})
            except Exception as e:
                exception(f"graph delete failed: {e}")

            # Delete DB records
            await sor.sqlExe("DELETE FROM rag_document_chunks WHERE doc_id=${id}$", {"id": doc_id})
            await sor.sqlExe("DELETE FROM rag_entities WHERE kb_id=${kb_id}$", {"kb_id": doc.kb_id})
            await sor.sqlExe("DELETE FROM rag_entity_relations WHERE kb_id=${kb_id}$", {"kb_id": doc.kb_id})
            await sor.sqlExe("DELETE FROM rag_documents WHERE id=${id}$", {"id": doc_id})

            # Update KB stats
            await sor.sqlExe(
                "UPDATE rag_knowledge_bases SET doc_count=GREATEST(doc_count-1,0), total_size=GREATEST(total_size-${size}$,0), chunk_count=GREATEST(chunk_count-${n}$,0) WHERE id=${kb_id}$",
                {"size": doc.file_size, "n": len(chunks), "kb_id": doc.kb_id})

        # Delete file from disk
        file_path = doc.file_path
        if file_path and file_path.startswith("/idfile/"):
            real_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files",
                                     os.path.basename(file_path))
            if os.path.exists(real_path):
                os.remove(real_path)

        return json.dumps({"status": "SUCCEEDED", "doc_id": doc_id, "chunks_deleted": len(chunks)})

    except Exception as e:
        exception(f"doc_delete: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


def _detect_file_type(name, mime):
    """Detect file type from name and MIME"""
    ext = os.path.splitext(name)[1].lower()
    if ext in ('.txt', '.md', '.json', '.csv', '.xml', '.html', '.py', '.js', '.css', '.yaml', '.yml'):
        return "text"
    if ext in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg'):
        return "image"
    if ext in ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac'):
        return "audio"
    if ext in ('.mp4', '.avi', '.mov', '.mkv', '.webm'):
        return "video"
    if ext == '.pdf':
        return "text"
    return "other"


async def _get_engine_cfg(env, engine_type):
    """读 rag_engine_configs 的在线引擎配置（embedding/rerank）。
    返回 dict(model_id, api_base, api_key 明文) 或 None（未配置→能力屏蔽）。"""
    try:
        from appPublic.rc4 import unpassword
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT model_name, endpoint_url, api_key FROM rag_engine_configs "
                "WHERE engine_type=${t}$ AND status='active' ORDER BY is_default DESC, priority DESC LIMIT 1",
                {"t": engine_type})
        if not recs:
            return None
        r = recs[0]
        model_id = (getattr(r, "model_name", "") or "").strip()
        api_base = (getattr(r, "endpoint_url", "") or "").strip()
        enc = (getattr(r, "api_key", "") or "").strip()
        if not (model_id and api_base and enc):
            return None
        try:
            from appPublic.jsonConfig import getConfig
            key = getConfig().password_key or 'QRIVSRHrthhwyjy176556332'
            api_key = unpassword(enc, key)
        except Exception:
            api_key = enc
        return {"model_id": model_id, "api_base": api_base.rstrip("/"), "api_key": api_key}
    except Exception as e:
        exception(f"engine cfg read failed ({engine_type}): {e}")
        return None


async def _online_embed(env, texts):
    """阿里在线 embedding（OpenAI 兼容 /embeddings）。未配置或失败返回 []。"""
    cfg = await _get_engine_cfg(env, "embedding")
    if not cfg:
        return []
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(cfg["api_base"] + "/embeddings",
                              json={"model": cfg["model_id"], "input": texts},
                              headers={"Authorization": "Bearer " + cfg["api_key"],
                                       "Content-Type": "application/json"}) as resp:
                data = await resp.json()
        items = data.get("data", []) if isinstance(data, dict) else []
        return [it.get("embedding") for it in items if it.get("embedding")]
    except Exception as e:
        exception(f"online embed failed: {e}")
        return []


async def _online_rerank(env, query, documents):
    """阿里在线 rerank（dashscope compatible-api /reranks）。未配置或失败返回 None。"""
    cfg = await _get_engine_cfg(env, "rerank")
    if not cfg:
        return None
    import aiohttp
    base = cfg["api_base"]
    if "compatible-mode" in base:
        base = base.replace("compatible-mode", "compatible-api")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(base + "/reranks",
                              json={"model": cfg["model_id"], "query": query,
                                    "documents": documents, "top_n": len(documents)},
                              headers={"Authorization": "Bearer " + cfg["api_key"],
                                       "Content-Type": "application/json"}) as resp:
                data = await resp.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        # dashscope 返回 [{index, relevance_score}]，转成与 hits 对齐的 scores 列表
        scores = [0.0] * len(documents)
        for it in results:
            idx = it.get("index")
            if isinstance(idx, int) and 0 <= idx < len(scores):
                scores[idx] = it.get("relevance_score", 0)
        return {"scores": scores}
    except Exception as e:
        exception(f"online rerank failed: {e}")
        return None


async def _call_uapi(upappid, apiname, data, timeout=10):
    """Call service via uapi config (VDB 等)"""
    import aiohttp
    env = ServerEnv()
    async with get_sor_context(env, 'rag') as sor:
        recs = await sor.sqlExe(
            "SELECT a.path, a.httpmethod, a.data as tmpl, b.baseurl "
            "FROM uapi a JOIN upapp b ON a.upappid=b.id "
            "WHERE a.upappid=${upappid}$ AND a.name=${apiname}$",
            {"upappid": upappid, "apiname": apiname})
        if not recs:
            raise Exception(f"uapi not found: {upappid}/{apiname}")
        cfg = recs[0]
    body = await _render_tmpl(cfg.tmpl, data)
    url = f"{cfg.baseurl}{cfg.path}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, data=body, headers={"Content-Type": "application/json"}) as resp:
            return await resp.json()


async def _rag_ingest_async(env, text, kb_id, doc_id):
    """RAG ingestion pipeline via uapi: chunk → embed → VDB → NER → graph"""
    chunks = _split_text(text, chunk_size=512, overlap=64)
    if not chunks:
        return {"chunks": 0}
    chunk_count = len(chunks)

    # 1. Embedding（在线模型；未配置则屏蔽 → 空向量，文档仍入库可浏览）
    embeddings = await _online_embed(env, chunks)

    # 2. VDB upsert
    vector_ids = []
    if embeddings:
        if len(embeddings) != len(chunks):
            embeddings = embeddings[:len(chunks)]
        try:
            vdb_data = {
                "collection": kb_id,
                "data": [{"id": f"{doc_id}_{i}", "vector": emb, "text": chunks[i]}
                         for i, emb in enumerate(embeddings)]
            }
            vdb_resp = await _call_uapi("rag-vdb", "upsert", vdb_data)
            vector_ids = [f"{doc_id}_{i}" for i in range(len(embeddings))]
        except Exception as e:
            exception(f"vdb upsert failed: {e}")

    # 3. NER entity extraction
    entities_found = []
    try:
        full_text = " ".join(chunks[:20])  # first 20 chunks for NER
        ner_resp = await _call_uapi("rag-ner", "entities", {"text": full_text})
        entities_found = ner_resp.get("entities", []) if isinstance(ner_resp, dict) else []
    except Exception as e:
        exception(f"ner failed: {e}")

    # 4. Save to graph
    if entities_found:
        try:
            graph_data = {
                "graph": kb_id,
                "data": {"entities": entities_found, "source_doc": doc_id}
            }
            await _call_uapi("rag-graph", "save", graph_data)
        except Exception as e:
            exception(f"graph save failed: {e}")

    # 5. Record chunks in DB
    async with get_sor_context(env, 'rag') as sor:
        for i, (chunk_text, vid) in enumerate(zip(chunks, vector_ids)):
            await sor.sqlExe(
                "INSERT INTO rag_document_chunks (id, doc_id, kb_id, chunk_index, content, vector_id, metadata, created_at) "
                "VALUES (${id}$, ${doc_id}$, ${kb_id}$, ${idx}$, ${content}$, ${vid}$, NOW())",
                {"id": f"{doc_id}_c{i}", "doc_id": doc_id, "kb_id": kb_id,
                 "idx": i, "content": chunk_text[:2000], "vid": vid})

    return {"chunks": chunk_count, "vectors": len(vector_ids),
            "entities": len(entities_found)}


def _split_text(text, chunk_size=512, overlap=64):
    """Simple text chunker: paragraph-based with size limits"""
    paragraphs = text.split('\n')
    chunks = []
    current = ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(current) + len(p) < chunk_size:
            current = (current + " " + p).strip()
        else:
            if current:
                chunks.append(current)
            current = p
    if current:
        chunks.append(current)
    # If still no chunks (single giant paragraph), force-split by size
    if not chunks and text.strip():
        for i in range(0, len(text), chunk_size - overlap):
            chunks.append(text[i:i + chunk_size])
    return chunks


async def _render_tmpl(tmpl, data):
    """Simple Jinja2-style template rendering for uapi data templates"""
    import re
    result = tmpl
    for key, val in data.items():
        result = result.replace("{{" + key + "}}", str(val))
        result = result.replace("{{json.dumps(" + key + ")}}", json.dumps(val, ensure_ascii=False))
    return result


async def _call_vdb_async(path, data, timeout=10):
    """Call VDB service via uapi (mapping path to apiname)"""
    apiname_map = {"/v1/upsert": "upsert", "/v1/search": "search", "/v1/delete": "delete"}
    apiname = apiname_map.get(path, "search")
    return await _call_uapi("rag-vdb", apiname, data, timeout)


async def _call_graph_async(path, data, timeout=10):
    """Call Graph service via uapi"""
    apiname_map = {"/api/graph/save": "save", "/api/graph/query": "query", "/api/graph/delete": "delete"}
    apiname = apiname_map.get(path, "save")
    return await _call_uapi("rag-graph", apiname, data, timeout)


# Keep sync wrappers for backward compat (note: these block in async context, prefer async versions)
def _call_vdb(path, data, timeout=10):
    """Synchronous VDB call — deprecated, use _call_vdb_async in async context"""
    import urllib.request
    url = f"http://localhost:8886{path}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _call_graph(path, data, timeout=10):
    """Synchronous Graph call — deprecated, use _call_graph_async in async context"""
    import urllib.request
    url = f"http://localhost:9092{path}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


async def dir_create_handler(request, params_kw, *args, **kwargs):
    """创建目录"""
    env = request._run_ns
    try:
        kb_id = params_kw.get("kb_id", "")
        parent_id = params_kw.get("parent_id", "")
        dir_name = params_kw.get("dir_name", "")
        if not kb_id or not dir_name:
            return json.dumps({"error": "kb_id and dir_name required"})
        async with get_sor_context(env, 'rag') as sor:
            dir_id = uuid.uuid4().hex
            await sor.sqlExe(
                "INSERT INTO rag_document_chunks (id, doc_id, kb_id, chunk_index, chunk_type, content, description, created_at) "
                "VALUES (${id}$, '', ${kb_id}$, 0, 'directory', ${name}$, ${parent}$, NOW())",
                {"id": dir_id, "kb_id": kb_id, "name": dir_name, "parent": parent_id})
            return json.dumps({"status": "SUCCEEDED", "dir_id": dir_id})
    except Exception as e:
        exception(f"dir_create: {e}")
        return json.dumps({"error": str(e)})


async def dir_delete_handler(request, params_kw, *args, **kwargs):
    """删除目录/文件"""
    env = request._run_ns
    try:
        item_id = params_kw.get("item_id", "")
        if not item_id:
            return json.dumps({"error": "item_id required"})
        async with get_sor_context(env, 'rag') as sor:
            await sor.sqlExe("DELETE FROM rag_document_chunks WHERE id=${id}$ OR doc_id=${id}$", {"id": item_id})
            return json.dumps({"status": "SUCCEEDED"})
    except Exception as e:
        exception(f"dir_delete: {e}")
        return json.dumps({"error": str(e)})


async def dir_list_handler(request, params_kw, *args, **kwargs):
    """列出知识库的目录树"""
    env = request._run_ns
    try:
        kb_id = params_kw.get("kb_id", "")
        if not kb_id:
            return json.dumps({"error": "kb_id required"})
        async with get_sor_context(env, 'rag') as sor:
            # Get directories (chunk_type='directory')
            dirs = await sor.sqlExe(
                "SELECT id, content as label, description as parent_id FROM rag_document_chunks WHERE kb_id=${kb_id}$ AND chunk_type='directory'",
                {"kb_id": kb_id})
            # Get files (documents table)
            docs = await sor.sqlExe(
                "SELECT id, file_name as label, '' as parent_id FROM rag_documents WHERE kb_id=${kb_id}$",
                {"kb_id": kb_id})
            items = []
            for d in dirs:
                items.append({"id": d.id, "label": d.label, "parent_id": d.parent_id or "", "type": "dir"})
            for d in docs:
                items.append({"id": d.id, "label": d.label, "parent_id": "", "type": "file"})
            return json.dumps({"status": "SUCCEEDED", "items": items})
    except Exception as e:
        exception(f"dir_list: {e}")
        return json.dumps({"error": str(e)})


async def tag_create_handler(request, params_kw, *args, **kwargs):
    """创建标签"""
    env = request._run_ns
    try:
        kb_id = params_kw.get("kb_id", "")
        name = params_kw.get("name", "").strip()
        color = params_kw.get("color", "#3b82f6")
        if not kb_id or not name:
            return json.dumps({"error": "kb_id and name required"})
        userorgid = await env.get_userorgid()
        async with get_sor_context(env, 'rag') as sor:
            existing = await sor.sqlExe(
                "SELECT id, color FROM rag_tags WHERE kb_id=${kb_id}$ AND name=${name}$ AND org_id=${org_id}$",
                {"kb_id": kb_id, "name": name, "org_id": userorgid})
            if existing:
                return json.dumps({"status": "SUCCEEDED", "tag_id": existing[0].id, "name": name,
                                   "color": existing[0].color, "duplicate": True}, ensure_ascii=False)
            tag_id = uuid.uuid4().hex
            await sor.sqlExe(
                "INSERT INTO rag_tags (id, kb_id, name, color, org_id, created_at) "
                "VALUES (${id}$, ${kb_id}$, ${name}$, ${color}$, ${org_id}$, NOW())",
                {"id": tag_id, "kb_id": kb_id, "name": name, "color": color, "org_id": userorgid})
            return json.dumps({"status": "SUCCEEDED", "tag_id": tag_id, "name": name, "color": color})
    except Exception as e:
        exception(f"tag_create: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_list_handler(request, params_kw, *args, **kwargs):
    """列出知识库的所有标签"""
    env = request._run_ns
    try:
        kb_id = params_kw.get("kb_id", "")
        if not kb_id:
            return json.dumps({"error": "kb_id required"})
        userorgid = await env.get_userorgid()
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.R("rag_tags", {"kb_id": kb_id, "org_id": userorgid})
            tags = [{"id": r.id, "name": r.name, "color": r.color, "created_at": str(r.created_at)} for r in recs]
            return json.dumps({"status": "SUCCEEDED", "tags": tags}, ensure_ascii=False, default=str)
    except Exception as e:
        exception(f"tag_list: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_delete_handler(request, params_kw, *args, **kwargs):
    """删除标签（级联删除关联）"""
    env = request._run_ns
    try:
        tag_id = params_kw.get("tag_id", "")
        if not tag_id:
            return json.dumps({"error": "tag_id required"})
        async with get_sor_context(env, 'rag') as sor:
            await sor.sqlExe("DELETE FROM rag_media_tags WHERE tag_id=${id}$", {"id": tag_id})
            await sor.sqlExe("DELETE FROM rag_tags WHERE id=${id}$", {"id": tag_id})
            return json.dumps({"status": "SUCCEEDED", "tag_id": tag_id})
    except Exception as e:
        exception(f"tag_delete: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_assign_handler(request, params_kw, *args, **kwargs):
    """给媒体/人脸/声纹打标签"""
    env = request._run_ns
    try:
        kb_id = params_kw.get("kb_id", "")
        media_type = params_kw.get("media_type", "")  # document / face / voice
        media_id = params_kw.get("media_id", "")
        tag_id = params_kw.get("tag_id", "")
        if not all([kb_id, media_type, media_id, tag_id]):
            return json.dumps({"error": "kb_id, media_type, media_id, tag_id required"})
        if media_type not in ("document", "face", "voice"):
            return json.dumps({"error": "media_type must be document/face/voice"})
        async with get_sor_context(env, 'rag') as sor:
            mt_id = uuid.uuid4().hex
            await sor.sqlExe(
                "INSERT INTO rag_media_tags (id, kb_id, media_type, media_id, tag_id, created_at) "
                "VALUES (${id}$, ${kb_id}$, ${type}$, ${mid}$, ${tid}$, NOW())",
                {"id": mt_id, "kb_id": kb_id, "type": media_type, "mid": media_id, "tid": tag_id})
            return json.dumps({"status": "SUCCEEDED", "media_tag_id": mt_id})
    except Exception as e:
        exception(f"tag_assign: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_unassign_handler(request, params_kw, *args, **kwargs):
    """取消标签关联"""
    env = request._run_ns
    try:
        media_type = params_kw.get("media_type", "")
        media_id = params_kw.get("media_id", "")
        tag_id = params_kw.get("tag_id", "")
        if not all([media_type, media_id, tag_id]):
            return json.dumps({"error": "media_type, media_id, tag_id required"})
        async with get_sor_context(env, 'rag') as sor:
            await sor.sqlExe(
                "DELETE FROM rag_media_tags WHERE media_type=${type}$ AND media_id=${mid}$ AND tag_id=${tid}$",
                {"type": media_type, "mid": media_id, "tid": tag_id})
            return json.dumps({"status": "SUCCEEDED"})
    except Exception as e:
        exception(f"tag_unassign: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_media_tags_handler(request, params_kw, *args, **kwargs):
    """查询某媒体的所有标签"""
    env = request._run_ns
    try:
        media_type = params_kw.get("media_type", "")
        media_id = params_kw.get("media_id", "")
        if not all([media_type, media_id]):
            return json.dumps({"error": "media_type and media_id required"})
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT t.id, t.name, t.color FROM rag_media_tags mt "
                "JOIN rag_tags t ON mt.tag_id=t.id "
                "WHERE mt.media_type=${type}$ AND mt.media_id=${mid}$",
                {"type": media_type, "mid": media_id})
            tags = [{"id": r.id, "name": r.name, "color": r.color} for r in recs]
            return json.dumps({"status": "SUCCEEDED", "tags": tags})
    except Exception as e:
        exception(f"tag_media_tags: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_search_handler(request, params_kw, *args, **kwargs):
    """组合标签检索：按 tag_ids 过滤，再语义检索"""
    env = request._run_ns
    try:
        query = params_kw.get("query", "")
        kb_id = params_kw.get("kb_id", "")
        tag_ids_str = params_kw.get("tag_ids", "")  # comma-separated tag IDs
        top_k = int(params_kw.get("top_k", 5))
        match_mode = params_kw.get("match_mode", "any")  # any / all
        if not kb_id:
            return json.dumps({"error": "kb_id required"})
        async with get_sor_context(env, 'rag') as sor:
            if tag_ids_str:
                tag_ids = [t.strip() for t in tag_ids_str.split(",") if t.strip()]
                media_ids_by_tag = []
                for tid in tag_ids:
                    recs = await sor.sqlExe(
                        "SELECT media_type, media_id FROM rag_media_tags WHERE kb_id=${kb_id}$ AND tag_id=${tid}$",
                        {"kb_id": kb_id, "tid": tid})
                    mids = {(r.media_type, r.media_id) for r in recs}
                    media_ids_by_tag.append(mids)
                if match_mode == "all":
                    matched = media_ids_by_tag[0]
                    for s in media_ids_by_tag[1:]:
                        matched = matched & s
                else:
                    matched = set()
                    for s in media_ids_by_tag:
                        matched |= s
                if not matched:
                    return json.dumps({"status": "SUCCEEDED", "results": [], "message": "no media match tags"})
                doc_ids = [mid for mt, mid in matched if mt == "document"]
                face_ids = [mid for mt, mid in matched if mt == "face"]
                voice_ids = [mid for mt, mid in matched if mt == "voice"]
                results = []
                if doc_ids:
                    docs = await sor.sqlExe(
                        "SELECT id, file_name, file_type, file_size, status, created_at FROM rag_documents WHERE id IN (${ids}$)",
                        {"ids": doc_ids})
                    for d in docs:
                        results.append({"type": "document", "id": d.id, "name": d.file_name, "file_type": d.file_type, "size": d.file_size, "status": d.status, "created_at": str(d.created_at)})
                if face_ids:
                    faces = await sor.sqlExe(
                        "SELECT id, name, description, face_embedding_id, created_at FROM rag_entities WHERE id IN (${ids}$) AND entity_type='person'",
                        {"ids": face_ids})
                    for f in faces:
                        results.append({"type": "face", "id": f.id, "name": f.name, "description": f.description, "created_at": str(f.created_at)})
                if voice_ids:
                    voices = await sor.sqlExe(
                        "SELECT id, name, description, voice_embedding_id, created_at FROM rag_entities WHERE id IN (${ids}$) AND entity_type='voice'",
                        {"ids": voice_ids})
                    for v in voices:
                        results.append({"type": "voice", "id": v.id, "name": v.name, "description": v.description, "created_at": str(v.created_at)})
                if query:
                    import urllib.request
                    tag_filtered_docs = [r for r in results if r["type"] == "document"]
                    vdb_docs = []
                    for doc in tag_filtered_docs:
                        chunks = await sor.sqlExe(
                            "SELECT content FROM rag_document_chunks WHERE doc_id=${id}$ LIMIT 3",
                            {"id": doc["id"]})
                        for c in chunks:
                            vdb_docs.append({"doc_id": doc["id"], "content": c.content})
                    results = {"documents": [{"id": r["id"], "name": r["name"], "file_type": r["file_type"]} for r in tag_filtered_docs],
                               "faces": [r for r in results if r["type"] == "face"],
                               "voices": [r for r in results if r["type"] == "voice"],
                               "chunks": vdb_docs,
                               "query": query,
                               "tag_ids": tag_ids,
                               "match_mode": match_mode}
                return json.dumps({"status": "SUCCEEDED", "results": results, "tag_ids": tag_ids, "match_mode": match_mode}, ensure_ascii=False, default=str)
            else:
                return json.dumps({"status": "SUCCEEDED", "results": [], "message": "no tag_ids provided"})
    except Exception as e:
        exception(f"tag_search: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def tag_sync_handler(request, params_kw, *args, **kwargs):
    """全量同步媒体标签关联：删除不在选中列表的，插入新增的"""
    env = request._run_ns
    try:
        kb_id = params_kw.get("kb_id", "")
        media_type = params_kw.get("media_type", "")
        media_id = params_kw.get("media_id", "")
        tag_ids_str = params_kw.get("tag_ids", "")
        if not all([kb_id, media_type, media_id]):
            return json.dumps({"error": "kb_id, media_type, media_id required"})
        if media_type not in ("document", "face", "voice"):
            return json.dumps({"error": "media_type must be document/face/voice"})
        wanted_ids = [t.strip() for t in tag_ids_str.split(",") if t.strip()]
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT id, tag_id FROM rag_media_tags WHERE media_type=${type}$ AND media_id=${mid}$",
                {"type": media_type, "mid": media_id})
            current = {r.tag_id: r.id for r in recs}
            removed = 0
            for tid, mt_id in current.items():
                if tid not in wanted_ids:
                    await sor.sqlExe("DELETE FROM rag_media_tags WHERE id=${id}$", {"id": mt_id})
                    removed += 1
            added = 0
            for tid in wanted_ids:
                if tid not in current:
                    mt_id = uuid.uuid4().hex
                    await sor.sqlExe(
                        "INSERT INTO rag_media_tags (id, kb_id, media_type, media_id, tag_id, created_at) "
                        "VALUES (${id}$, ${kb_id}$, ${type}$, ${mid}$, ${tid}$, NOW())",
                        {"id": mt_id, "kb_id": kb_id, "type": media_type, "mid": media_id, "tid": tid})
                    added += 1
            return json.dumps({"status": "SUCCEEDED", "added": added, "removed": removed})
    except Exception as e:
        exception(f"tag_sync: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def engine_options_handler(request, params_kw, *args, **kwargs):
    """产线平台 llm 表中可用的 embedding/rerank 模型选项（供配置界面下拉）"""
    env = request._run_ns
    try:
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT id, name, model_id, capabilities FROM llm WHERE status='active' ORDER BY name", {})
        rows = [{"v": r.model_id, "t": f"{r.name}（{r.model_id}）"} for r in recs]
        return json.dumps({"status": "SUCCEEDED", "rows": rows}, ensure_ascii=False)
    except Exception as e:
        exception(f"engine_options: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def engine_cfg_get_handler(request, params_kw, *args, **kwargs):
    """读当前 embedding/rerank 引擎配置（api_key 不回显明文）"""
    env = request._run_ns
    try:
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT engine_type, model_name, endpoint_url, api_key, status FROM rag_engine_configs "
                "WHERE engine_type IN ('embedding','rerank','mm_embedding','mm_rerank') ORDER BY engine_type", {})
        rows = []
        for r in recs:
            enc = getattr(r, "api_key", "") or ""
            rows.append({"engine_type": r.engine_type, "model_id": r.model_name or "",
                         "api_base": r.endpoint_url or "", "status": r.status or "active",
                         "has_key": bool(enc)})
        return json.dumps({"status": "SUCCEEDED", "rows": rows}, ensure_ascii=False)
    except Exception as e:
        exception(f"engine_cfg_get: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def engine_cfg_save_handler(request, params_kw, *args, **kwargs):
    """保存引擎配置。api_key 留空=沿用旧 key；model_id 清空=屏蔽该能力。"""
    env = request._run_ns
    try:
        engine_type = (params_kw.get("engine_type") or "").strip()
        if engine_type not in ("embedding", "rerank", "mm_embedding", "mm_rerank"):
            return json.dumps({"error": "engine_type must be embedding/rerank/mm_embedding/mm_rerank"})
        model_id = (params_kw.get("model_id") or "").strip()
        api_base = (params_kw.get("api_base") or "").strip()
        api_key_plain = (params_kw.get("api_key") or "").strip()
        status = (params_kw.get("status") or "active").strip()
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT id, api_key FROM rag_engine_configs WHERE engine_type=${t}$ LIMIT 1",
                {"t": engine_type})
            if recs:
                rid = recs[0].id
                if api_key_plain:
                    from appPublic.rc4 import password
                    from appPublic.jsonConfig import getConfig
                    key = getConfig().password_key or 'QRIVSRHrthhwyjy176556332'
                    enc = password(api_key_plain, key=key)
                else:
                    enc = recs[0].api_key or ""
                await sor.sqlExe(
                    "UPDATE rag_engine_configs SET model_name=${m}$, endpoint_url=${b}$, api_key=${k}$, "
                    "status=${s}$, is_default=1, updated_at=NOW() WHERE id=${id}$",
                    {"m": model_id, "b": api_base, "k": enc, "s": status, "id": rid})
            else:
                from appPublic.rc4 import password
                from appPublic.jsonConfig import getConfig
                key = getConfig().password_key or 'QRIVSRHrthhwyjy176556332'
                enc = password(api_key_plain, key=key) if api_key_plain else ""
                rid = uuid.uuid4().hex
                await sor.sqlExe(
                    "INSERT INTO rag_engine_configs (id, engine_type, engine_name, endpoint_url, api_key, "
                    "model_name, is_default, priority, status, created_at, updated_at) "
                    "VALUES (${id}$, ${t}$, ${n}$, ${b}$, ${k}$, ${m}$, 1, 10, ${s}$, NOW(), NOW())",
                    {"id": rid, "t": engine_type, "n": engine_type, "b": api_base,
                     "k": enc, "m": model_id, "s": status})
        shielded = not model_id or status != "active"
        return json.dumps({"status": "SUCCEEDED", "shielded": shielded}, ensure_ascii=False)
    except Exception as e:
        exception(f"engine_cfg_save: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def engine_cfg_test_handler(request, params_kw, *args, **kwargs):
    """连通性测试：用提交的 model/base/key（或已存配置）发一次真实调用"""
    env = request._run_ns
    try:
        engine_type = (params_kw.get("engine_type") or "").strip()
        model_id = (params_kw.get("model_id") or "").strip()
        api_base = (params_kw.get("api_base") or "").strip()
        api_key_plain = (params_kw.get("api_key") or "").strip()
        if not (model_id and api_base):
            cfg = await _get_engine_cfg(env, engine_type)
            if not cfg:
                return json.dumps({"error": "未配置，无法测试"}, ensure_ascii=False)
            model_id, api_base = cfg["model_id"], cfg["api_base"]
            api_key_plain = api_key_plain or cfg["api_key"]
        if not api_key_plain:
            return json.dumps({"error": "缺少 api_key"}, ensure_ascii=False)
        import aiohttp
        headers = {"Authorization": "Bearer " + api_key_plain, "Content-Type": "application/json"}
        base = api_base.rstrip("/")
        # 在线多模态：DashScope 原生 API
        if engine_type == "mm_embedding":
            if "/api/v1" not in base:
                base = base + "/api/v1"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(base + "/services/embeddings/multimodal-embedding/multimodal-embedding",
                                  json={"model": model_id, "input": {"contents": [{"text": "连通性测试"}]}},
                                  headers=headers) as resp:
                    data = await resp.json()
            embs = (data.get("output") or {}).get("embeddings") or []
            dim = len(embs[0]["embedding"]) if embs and embs[0].get("embedding") else 0
            if not dim:
                return json.dumps({"error": str(data)[:200]}, ensure_ascii=False)
            return json.dumps({"status": "SUCCEEDED", "message": f"mm_embedding OK，维度 {dim}"}, ensure_ascii=False)
        if engine_type == "mm_rerank":
            if "/api/v1" not in base:
                base = base + "/api/v1"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(base + "/services/rerank/text-rerank/text-rerank",
                                  json={"model": model_id,
                                        "input": {"query": "测试", "documents": ["甲", "乙"]},
                                        "parameters": {"top_n": 2}},
                                  headers=headers) as resp:
                    data = await resp.json()
            if not (data.get("output") or {}).get("results"):
                return json.dumps({"error": str(data)[:200]}, ensure_ascii=False)
            return json.dumps({"status": "SUCCEEDED", "message": "mm_rerank OK"}, ensure_ascii=False)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            if engine_type == "embedding":
                async with s.post(base + "/embeddings", json={"model": model_id, "input": ["连通性测试"]},
                                  headers=headers) as resp:
                    data = await resp.json()
                dim = len(data.get("data", [{}])[0].get("embedding", [])) if data.get("data") else 0
                if not dim:
                    return json.dumps({"error": str(data)[:200]}, ensure_ascii=False)
                return json.dumps({"status": "SUCCEEDED", "message": f"embedding OK，维度 {dim}"}, ensure_ascii=False)
            else:
                if "compatible-mode" in base:
                    base = base.replace("compatible-mode", "compatible-api")
                async with s.post(base + "/reranks",
                                  json={"model": model_id, "query": "测试",
                                        "documents": ["甲", "乙"], "top_n": 2},
                                  headers=headers) as resp:
                    data = await resp.json()
                if not data.get("results"):
                    return json.dumps({"error": str(data)[:200]}, ensure_ascii=False)
                return json.dumps({"status": "SUCCEEDED", "message": "rerank OK"}, ensure_ascii=False)
    except Exception as e:
        exception(f"engine_cfg_test: {e}, {format_exc()}")
        return json.dumps({"error": str(e)[:200]})


def init_rag_module():
    env = ServerEnv()
    rf = RegisterFunction()
    rf.register("status", status_handler)
    rf.register("kb_list", kb_list_handler)
    rf.register("engines", engines_handler)
    rf.register("search", search_handler)
    rf.register("doc_upload", doc_upload_handler)
    rf.register("doc_delete", doc_delete_handler)
    rf.register("dir_create", dir_create_handler)
    rf.register("dir_delete", dir_delete_handler)
    rf.register("dir_list", dir_list_handler)
    rf.register("tag_create", tag_create_handler)
    rf.register("tag_list", tag_list_handler)
    rf.register("tag_delete", tag_delete_handler)
    rf.register("tag_assign", tag_assign_handler)
    rf.register("tag_unassign", tag_unassign_handler)
    rf.register("tag_media_tags", tag_media_tags_handler)
    rf.register("tag_search", tag_search_handler)
    rf.register("tag_sync", tag_sync_handler)
    rf.register("engine_options", engine_options_handler)
    rf.register("engine_cfg_get", engine_cfg_get_handler)
    rf.register("engine_cfg_save", engine_cfg_save_handler)
    rf.register("engine_cfg_test", engine_cfg_test_handler)
