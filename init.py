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
            recs = await sor.R("knowledge_bases", {})
            cards = []
            for r in recs:
                doc_recs = await sor.R("documents", {"kb_id": r.id})
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
                engine = r.embedding_engine or "CLIP"
                card = {"widgettype":"VBox","options":{"cwidth":16,"cheight":12,"bgcolor":"#f0f7ff","padding":"16px","css":"card clickable","border":"1px solid #d0e4f7"},"subwidgets":[
                    {"widgettype":"Text","options":{"text":"📚 " + str(r.name),"cfontsize":16,"fontWeight":"bold"}},
                    {"widgettype":"Text","options":{"text":str(engine)+" · 1024维","cfontsize":12,"color":"#888"}},
                    {"widgettype":"HBox","options":{"spacing":"12px"},"subwidgets":[
                        {"widgettype":"Text","options":{"text":"📄 "+str(doc_count)+"文档","cfontsize":12,"color":"#666"}},
                        {"widgettype":"Text","options":{"text":"💾 "+size_str,"cfontsize":12,"color":"#666"}}]}],
                    "binds":[{"wid":"self","event":"click","actiontype":"urlwidget","target":"app.rag_main_content","mode":"replace","options":{"url":"/knowledge_bases_list/detail.ui","params":{"kb_id":str(r.id),"kb_name":str(r.name)}}}]}
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
            sql = "SELECT * FROM engine_configs WHERE status='active' AND (org_id IS NULL OR org_id=${org_id}$) ORDER BY engine_type, priority DESC"
            recs = await sor.sqlExe(sql, {"org_id": userorgid})
            rows = [dict(r) for r in recs]
            return json.dumps({"status": "SUCCEEDED", "data": {"rows": rows, "total": len(rows)}}, ensure_ascii=False, default=str)
    except Exception as e:
        exception(f"engines: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


async def search_handler(request, params_kw, *args, **kwargs):
    env = request._run_ns
    try:
        query = params_kw.get("query", "")
        kb_id = params_kw.get("kb_id", "")
        top_k = int(params_kw.get("top_k", 5))
        if not query:
            return json.dumps({"error": "query required"})
        try:
            from pipeline import search as pipeline_search
            result = pipeline_search(query, pipeline_name="kg-rag-standard",
                                     collection=kb_id or "knowledge",
                                     graph_name=kb_id or "knowledge",
                                     top_k=top_k, llm_func=None)
            return json.dumps(result, ensure_ascii=False)
        except ImportError:
            return json.dumps({"status": "FALLBACK", "message": "pipeline not available"})
    except Exception as e:
        exception(f"search: {e}, {format_exc()}")
        return json.dumps({"error": str(e)})


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

        # Save file
        doc_id = uuid.uuid4().hex[:16]
        ext = os.path.splitext(file_name)[1] or ".bin"
        saved_name = f"{doc_id}{ext}"
        files_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")
        os.makedirs(files_dir, exist_ok=True)
        file_path = os.path.join(files_dir, saved_name)
        with open(file_path, "wb") as f:
            f.write(file_data)

        file_size = len(file_data)
        file_type = _detect_file_type(file_name, "application/octet-stream")

        # Create document record
        async with get_sor_context(env, 'rag') as sor:
            await sor.sqlExe(
                "INSERT INTO documents (id, kb_id, file_name, file_type, file_size, file_path, mime_type, status, org_id, created_at, updated_at) "
                "VALUES (${id}$, ${kb_id}$, ${file_name}$, ${file_type}$, ${file_size}$, ${file_path}$, ${mime_type}$, 'pending', ${org_id}$, NOW(), NOW())",
                {"id": doc_id, "kb_id": kb_id, "file_name": file_name, "file_type": file_type,
                 "file_size": file_size, "file_path": "/idfile/files/" + saved_name,
                 "mime_type": "application/octet-stream", "org_id": userorgid})

            # Update KB stats
            await sor.sqlExe(
                "UPDATE knowledge_bases SET doc_count=doc_count+1, total_size=total_size+${size}$ WHERE id=${kb_id}$",
                {"size": file_size, "kb_id": kb_id})

        # Trigger async ingest for text-based files
        ingest_result = None
        if file_type == "text":
            try:
                text = file_data.decode("utf-8", errors="replace")
                from pipeline import ingest as pipeline_ingest
                ingest_result = pipeline_ingest(
                    text, pipeline_name="kg-rag-standard",
                    collection=kb_id, graph_name=kb_id, llm_func=None)
                # Update status to done
                async with get_sor_context(env, 'rag') as sor:
                    await sor.sqlExe(
                        "UPDATE documents SET status='done', chunk_count=${chunks}$ WHERE id=${id}$",
                        {"chunks": ingest_result.get("chunks", 0) if ingest_result else 0, "id": doc_id})
                    if ingest_result:
                        await sor.sqlExe(
                            "UPDATE knowledge_bases SET chunk_count=chunk_count+${n}$ WHERE id=${kb_id}$",
                            {"n": ingest_result.get("chunks", 0), "kb_id": kb_id})
            except Exception as e:
                exception(f"async ingest failed: {e}")

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
            recs = await sor.R("documents", {"id": doc_id})
            if not recs:
                return json.dumps({"error": "document not found"})
            doc = recs[0]

            # Get chunks to clean VDB
            chunks = await sor.R("document_chunks", {"doc_id": doc_id})

            # Delete from VDB
            if chunks:
                vector_ids = [c.vector_id for c in chunks if c.vector_id]
                if vector_ids:
                    try:
                        _call_vdb("/v1/delete", {"colname": doc.kb_id, "ids": vector_ids})
                    except Exception as e:
                        exception(f"vdb delete failed: {e}")

            # Delete from graph
            entities = await sor.R("entities", {"kb_id": doc.kb_id})
            if entities:
                try:
                    _call_graph("/api/graph/save", {"graph": doc.kb_id})
                except Exception as e:
                    exception(f"graph cleanup failed: {e}")

            # Delete DB records
            await sor.sqlExe("DELETE FROM document_chunks WHERE doc_id=${id}$", {"id": doc_id})
            await sor.sqlExe("DELETE FROM entities WHERE kb_id=${kb_id}$", {"kb_id": doc.kb_id})
            await sor.sqlExe("DELETE FROM entity_relations WHERE kb_id=${kb_id}$", {"kb_id": doc.kb_id})
            await sor.sqlExe("DELETE FROM documents WHERE id=${id}$", {"id": doc_id})

            # Update KB stats
            await sor.sqlExe(
                "UPDATE knowledge_bases SET doc_count=GREATEST(doc_count-1,0), total_size=GREATEST(total_size-${size}$,0), chunk_count=GREATEST(chunk_count-${n}$,0) WHERE id=${kb_id}$",
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


def _call_vdb(path, data, timeout=10):
    """Call VDB service"""
    import urllib.request
    url = f"http://localhost:8886{path}"
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _call_graph(path, data, timeout=10):
    """Call Graph service"""
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
            dir_id = uuid.uuid4().hex[:16]
            await sor.sqlExe(
                "INSERT INTO document_chunks (id, doc_id, kb_id, chunk_index, chunk_type, content, description, created_at) "
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
            await sor.sqlExe("DELETE FROM document_chunks WHERE id=${id}$ OR doc_id=${id}$", {"id": item_id})
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
                "SELECT id, content as label, description as parent_id FROM document_chunks WHERE kb_id=${kb_id}$ AND chunk_type='directory'",
                {"kb_id": kb_id})
            # Get files (documents table)
            docs = await sor.sqlExe(
                "SELECT id, file_name as label, '' as parent_id FROM documents WHERE kb_id=${kb_id}$",
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
            tag_id = uuid.uuid4().hex[:16]
            await sor.sqlExe(
                "INSERT INTO tags (id, kb_id, name, color, org_id, created_at) "
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
            recs = await sor.R("tags", {"kb_id": kb_id, "org_id": userorgid})
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
            await sor.sqlExe("DELETE FROM media_tags WHERE tag_id=${id}$", {"id": tag_id})
            await sor.sqlExe("DELETE FROM tags WHERE id=${id}$", {"id": tag_id})
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
            mt_id = uuid.uuid4().hex[:16]
            await sor.sqlExe(
                "INSERT INTO media_tags (id, kb_id, media_type, media_id, tag_id, created_at) "
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
                "DELETE FROM media_tags WHERE media_type=${type}$ AND media_id=${mid}$ AND tag_id=${tid}$",
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
                "SELECT t.id, t.name, t.color FROM media_tags mt "
                "JOIN tags t ON mt.tag_id=t.id "
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
                        "SELECT media_type, media_id FROM media_tags WHERE kb_id=${kb_id}$ AND tag_id=${tid}$",
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
                        "SELECT id, file_name, file_type, file_size, status, created_at FROM documents WHERE id IN (${ids}$)",
                        {"ids": doc_ids})
                    for d in docs:
                        results.append({"type": "document", "id": d.id, "name": d.file_name, "file_type": d.file_type, "size": d.file_size, "status": d.status, "created_at": str(d.created_at)})
                if face_ids:
                    faces = await sor.sqlExe(
                        "SELECT id, name, description, face_embedding_id, created_at FROM entities WHERE id IN (${ids}$) AND entity_type='person'",
                        {"ids": face_ids})
                    for f in faces:
                        results.append({"type": "face", "id": f.id, "name": f.name, "description": f.description, "created_at": str(f.created_at)})
                if voice_ids:
                    voices = await sor.sqlExe(
                        "SELECT id, name, description, voice_embedding_id, created_at FROM entities WHERE id IN (${ids}$) AND entity_type='voice'",
                        {"ids": voice_ids})
                    for v in voices:
                        results.append({"type": "voice", "id": v.id, "name": v.name, "description": v.description, "created_at": str(v.created_at)})
                if query:
                    import urllib.request
                    tag_filtered_docs = [r for r in results if r["type"] == "document"]
                    vdb_docs = []
                    for doc in tag_filtered_docs:
                        chunks = await sor.sqlExe(
                            "SELECT content FROM document_chunks WHERE doc_id=${id}$ LIMIT 3",
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


def init_ragserver():
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
