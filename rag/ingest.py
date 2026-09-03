# -*- coding:utf-8 -*-
"""RAG 文档入库管线（共享模块）— 从 upload_file.dspy 抽出，UI dspy 与对外 API dspy 共用。

ingest_one(doc_id, kb_id, file_name, ext_l, real_path):
    后台 asyncio 任务（background_reco 派发）。只接原始类型参数，
    不触碰 env/request（响应返回后已失效），自建 DBPools 连接。
    解析文本/图片/音频/视频 → 向量化 → VDB upsert → chunks 落库 → 文档置 done。

2026-09-03 从 wwwroot/knowledge_bases_list/upload_file.dspy 抽取（逻辑零改动），
避免 UI 上传与 /rag/api 对外上传两份入库实现分叉。
"""
import json, os, base64, subprocess
from appPublic.log import exception
from ahserver.serverenv import ServerEnv
from sqlor.dbpools import DBPools
from appPublic.streamhttpclient import StreamHttpClient

_env = ServerEnv()


def _dbname():
    return _env.get_module_dbname('rag')


async def _fail(db, _dbn, doc_id, reason):
    """入库失败落库：状态置 failed + error_msg 记录真实原因（禁静默 pending）。"""
    import traceback
    tb = traceback.format_exc()
    msg = reason + (' | ' + tb[-400:] if 'Traceback' in tb else '')
    exception(f"ingest_one[{doc_id}] failed: {msg}")
    try:
        async with db.sqlorContext(_dbn) as sor:
            await sor.sqlExe(
                "UPDATE rag_documents SET status='failed', error_msg=${e}$, updated_at=NOW() WHERE id=${id}$",
                {"id": doc_id, "e": msg[:600]})
            await sor.sqlExe("COMMIT", {})
    except Exception:
        pass


async def ingest_one(doc_id, kb_id, file_name, ext_l, real_path):
    db = DBPools()
    # 库名必须经宿主的 get_module_dbname 映射（ragserver→'rag'，pipeline-app→'pipeline'）
    # 后台任务无 request/env，用注入的全局函数解析；禁硬编码库名
    # 钩子缺失（如脱离服务进程的裸任务）必须落错，不能静默吞掉
    try:
        _dbn = _dbname()
    except Exception as e:
        exception(f"ingest_one[{doc_id}]: get_module_dbname 钩子不可用: {e}")
        try:
            async with db.sqlorContext('pipeline') as sor:
                await sor.sqlExe(
                    "UPDATE rag_documents SET status='failed', error_msg=${e}$, updated_at=NOW() WHERE id=${id}$",
                    {"id": doc_id, "e": ('dbname 解析失败: ' + str(e))[:600]})
                await sor.sqlExe("COMMIT", {})
        except Exception:
            pass
        return

    # 读知识库向量引擎：
    #   bge-m3             → 在线文本(阿里, 1024维)
    #   qwen3-vl-embedding → 在线多模态(阿里, 2560维)
    #   clip-vith14        → GPU 本地 CLIP(1024维, 有GPU环境保留)
    emb_engine = 'clip-vith14'
    try:
        async with db.sqlorContext(_dbn) as sor:
            krecs = await sor.sqlExe("SELECT embedding_engine FROM rag_knowledge_bases WHERE id=${kb_id}$", {"kb_id": kb_id})
            if krecs:
                emb_engine = (getattr(krecs[0], 'embedding_engine', '') or 'clip-vith14').strip()
    except: pass
    is_text = emb_engine == 'bge-m3'
    is_vl = emb_engine == 'qwen3-vl-embedding'
    vdb_dim = 2560 if is_vl else 1024

    # VDB 服务地址：读 upapp.rag-vdb（生产已切内网），不硬编码
    try:
        async with db.sqlorContext(_dbn) as sor:
            _u = await sor.sqlExe("SELECT baseurl FROM upapp WHERE id='rag-vdb'", {})
        VDB_BASE = (_u[0].baseurl or '').rstrip('/') if _u else 'https://vectordb.opencomputing.net:10443'
    except:
        VDB_BASE = 'https://vectordb.opencomputing.net:10443'

    if is_text:
        emb_url = 'https://embedding.opencomputing.net:10443/txte/api/embed'
        emb_model = 'bge-m3'
    else:
        emb_url = 'https://embedding.opencomputing.net:10443/mme/api/embed'
        emb_model = 'CLIP-ViT-H-14'

    # 在线引擎：配置读取 + key 解密（mm_embedding / mm_rerank）
    _vl_cfg = None
    if is_vl or not is_text:
        try:
            from rag.vl_online import get_mm_cfg
            _vl_cfg = await get_mm_cfg('mm_embedding')
        except:
            _vl_cfg = None

    async def _online_text_embed(texts):
        """文本向量化：vl 引擎走在线原生API；bge-m3 走在线兼容API（经 engine_configs）"""
        if is_vl and _vl_cfg:
            from rag.vl_online import vl_embed_texts
            return await vl_embed_texts(texts)
        try:
            from rag.init import _online_embed
            from ahserver.serverenv import ServerEnv
            return await _online_embed(ServerEnv(), texts)
        except:
            return []

    async def _media_embed_image(img_b64):
        """图片向量化：vl 在线(base64) 或 GPU CLIP"""
        if is_vl and _vl_cfg:
            try:
                from rag.vl_online import vl_embed_contents
                v = await vl_embed_contents([{"image": "data:image/jpeg;base64," + img_b64}])
                return v
            except:
                return None
        try:
            client = StreamHttpClient()
            resp = await client.request('POST', emb_url,
                json={"images": [img_b64], "model": emb_model})
            emb_data = json.loads(resp)
            es = emb_data.get("image_embeddings", emb_data.get("embeddings", []))
            return es[0] if es else None
        except:
            return None

    async def ensure_vdb_collection(client, kb_id):
        payload = {"colname": kb_id, "fields": [{"name": "id", "type": "str", "is_primary": True, "max_length": 64}, {"name": "vector", "type": "fvector", "dim": vdb_dim}, {"name": "text", "type": "str", "max_length": 65535}], "description": "RAG kb", "metric": "COSINE"}
        await client.request('POST', VDB_BASE + '/v1/createcollection', json=payload)

    text = ''
    chunks_n = 0
    face_count = 0
    voice_speakers = 0
    meta_parts = {}
    try:
        with open(real_path, 'rb') as f:
            file_data = f.read()

        text_exts = {'.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm', '.py', '.js', '.css', '.yaml', '.yml', '.log', '.rst'}
        image_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
        audio_exts = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac'}
        video_exts = {'.mp4', '.avi', '.mov', '.mkv', '.webm'}

        # --- OFFICE DOCS: text extraction (PDF/DOCX/PPTX/XLSX) ---
        if ext_l == '.pdf' and not text:
            import io; from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(file_data))
            text = '\n'.join(p.extract_text() or '' for p in reader.pages)
        elif ext_l == '.docx' and not text:
            import io; from docx import Document
            doc = Document(io.BytesIO(file_data))
            text = '\n'.join(p.text for p in doc.paragraphs)
        elif ext_l == '.pptx' and not text:
            import io; from pptx import Presentation
            prs = Presentation(io.BytesIO(file_data))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, 'text') and shape.text:
                        parts.append(shape.text)
            text = '\n'.join(parts)
        elif ext_l == '.xlsx' and not text:
            import io; from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(file_data), data_only=True)
            parts = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    parts.append('\t'.join(str(c or '') for c in row))
            text = '\n'.join(parts)

        # --- TEXT EXTRACTION ---
        if ext_l in text_exts:
            text = file_data.decode('utf-8', errors='replace')

        # --- 文本知识库不支持媒体文件 ---
        if is_text and (ext_l in image_exts or ext_l in audio_exts or ext_l in video_exts):
            try:
                async with db.sqlorContext(_dbn) as sor:
                    await sor.sqlExe(
                        "UPDATE rag_documents SET status='failed', metadata=${meta}$, updated_at=NOW() WHERE id=${id}$",
                        {"id": doc_id, "meta": json.dumps({"error": "文本知识库不支持媒体文件，请上传文本类文件(txt/md/pdf/docx等)或改用多媒体知识库"}, ensure_ascii=False)})
            except: pass
            return

        # --- IMAGE: face detection + 图片向量化入库 ---
        if ext_l in image_exts:
            img_b64 = base64.b64encode(file_data).decode()
            try:
                client = StreamHttpClient()
                resp = await client.request('POST', 'https://media.opencomputing.net:10443/face/api/detect', json={"images": [img_b64]})
                fd = json.loads(resp)
                results = fd.get("results", [])
                if results and isinstance(results[0], dict):
                    faces = results[0].get("faces", results[0].get("detections", []))
                    face_count = len(faces)
                    if faces and isinstance(faces[0], dict):
                        meta_parts['face_bboxes'] = [f.get("bbox", {}) for f in faces[:10]]
                meta_parts['face'] = face_count
            except: pass
            # 图片本身向量化（vl在线 或 GPU CLIP），存入 VDB 供以文搜图
            try:
                img_vec = await _media_embed_image(img_b64)
                if img_vec:
                    client2 = StreamHttpClient()
                    await ensure_vdb_collection(client2, kb_id)
                    vdb_data = {"colname": kb_id, "data": [
                        {"id": doc_id + "_c0", "vector": img_vec, "text": file_name}]}
                    await client2.request('POST', VDB_BASE + '/v1/upsert', json=vdb_data)
                    async with db.sqlorContext(_dbn) as sor:
                        await sor.sqlExe(
                            "INSERT INTO rag_document_chunks (id, doc_id, kb_id, chunk_index, content, vector_id, created_at) "
                            "VALUES (${id}$, ${doc_id}$, ${kb_id}$, 0, ${content}$, ${vid}$, NOW())",
                            {"id": doc_id + "_c0", "doc_id": doc_id, "kb_id": kb_id,
                             "content": file_name, "vid": doc_id + "_c0"})
                    chunks_n = 1
                    meta_parts['image'] = 'embedded'
            except: pass

        # --- AUDIO: voiceprint ---
        if ext_l in audio_exts:
            try:
                client = StreamHttpClient()
                resp = await client.request('POST', 'https://media.opencomputing.net:10443/voiceprint/extract/submit',
                    files={'file': (file_name, file_data)})
                vd = json.loads(resp)
                voice_speakers = vd.get('speakers', 1) if vd.get('status') == 'SUCCEEDED' else (1 if vd.get('embedding') else 0)
                meta_parts['voiceprint'] = voice_speakers
            except: pass

        # --- VIDEO: frame extraction + voiceprint ---
        if ext_l in video_exts:
            meta_parts['video'] = 'pending'
            video_ok = False
            try:
                tmp_img = '/tmp/' + doc_id + '_frame.jpg'
                subprocess.run(['ffmpeg', '-y', '-i', real_path, '-vframes', '1', '-q:v', '2', tmp_img],
                               capture_output=True, timeout=30)
                if os.path.exists(tmp_img):
                    with open(tmp_img, 'rb') as fi:
                        frame_data = fi.read()
                    img_b64 = base64.b64encode(frame_data).decode()
                    frame_bboxes = []
                    try:
                        client = StreamHttpClient()
                        resp = await client.request('POST', 'https://media.opencomputing.net:10443/face/api/detect', json={"images": [img_b64]})
                        fd = json.loads(resp)
                        results = fd.get("results", [])
                        if results and isinstance(results[0], dict):
                            faces = results[0].get("faces", results[0].get("detections", []))
                            face_count = len(faces)
                            frame_bboxes = [f.get("bbox", {}) for f in faces[:10]] if faces else []
                    except: pass
                    # --- 视频帧向量化（vl在线 或 GPU CLIP） ---
                    try:
                        frame_vec = await _media_embed_image(img_b64)
                        img_embeddings = [frame_vec] if frame_vec else []
                    except:
                        img_embeddings = []
                    if img_embeddings:
                        try:
                            client3 = StreamHttpClient()
                            await ensure_vdb_collection(client3, kb_id)
                            vdb_data = {"colname": kb_id, "data": [
                                {"id": doc_id + "_c0", "vector": img_embeddings[0], "text": file_name}
                            ]}
                            await client3.request('POST', VDB_BASE + '/v1/upsert', json=vdb_data)
                            chunk_meta = {"start_time": 0}
                            if frame_bboxes:
                                chunk_meta["bboxes"] = frame_bboxes
                            async with db.sqlorContext(_dbn) as sor:
                                await sor.sqlExe(
                                    "INSERT INTO rag_document_chunks (id, doc_id, kb_id, chunk_index, content, vector_id, metadata, created_at) "
                                    "VALUES (${id}$, ${doc_id}$, ${kb_id}$, 0, ${content}$, ${vid}$, ${meta}$, NOW())",
                                    {"id": doc_id + "_c0", "doc_id": doc_id, "kb_id": kb_id,
                                     "content": file_name, "vid": doc_id + "_c0",
                                     "meta": json.dumps(chunk_meta, ensure_ascii=False)})
                        except:
                            pass
                    os.remove(tmp_img)
                    video_ok = True
            except: pass

            # --- Voiceprint: extract audio from video ---
            if video_ok:
                try:
                    tmp_wav = '/tmp/' + doc_id + '_audio.wav'
                    subprocess.run(['ffmpeg', '-y', '-i', real_path, '-vn', '-acodec', 'pcm_s16le',
                                    '-ar', '16000', '-ac', '1', tmp_wav],
                                   capture_output=True, timeout=60)
                    if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 1000:
                        with open(tmp_wav, 'rb') as fa:
                            audio_data = fa.read()
                        try:
                            client4 = StreamHttpClient()
                            resp4 = await client4.request('POST',
                                'https://media.opencomputing.net:10443/voiceprint/extract/submit',
                                files={'file': (file_name.rsplit('.', 1)[0] + '.wav', audio_data)})
                            vd = json.loads(resp4)
                            voice_speakers = vd.get('speakers', 1) if vd.get('status') == 'SUCCEEDED' else (1 if vd.get('embedding') else 0)
                            meta_parts['voiceprint'] = voice_speakers
                        except: pass
                    if os.path.exists(tmp_wav):
                        os.remove(tmp_wav)
                except: pass

            if video_ok:
                meta_parts['video'] = 'done'

        # --- RAG INGEST for text ---
        if text and len(text.strip()) > 10:
            paragraphs = text.split('\n')
            chunks = []
            cur = ''
            for p in paragraphs:
                p = p.strip()
                if not p:
                    if cur: chunks.append(cur); cur = ''
                    continue
                if len(cur) + len(p) < 500:
                    cur = (cur + '\n' + p).strip()
                else:
                    if cur: chunks.append(cur)
                    cur = p
            if cur: chunks.append(cur)

            if chunks:
                # 在线文本向量化（vl引擎→qwen3-vl原生；bge-m3→在线兼容；GPU CLIP 时代的老路径已移除）
                embeddings = await _online_text_embed(chunks)
                embeddings = [e for e in embeddings if e] or []
                if not embeddings:
                    await _fail(db, _dbn, doc_id,
                        'embedding 返回空（在线引擎不可达/未配置，检查 rag_engine_configs 与 VDB_BASE=' + VDB_BASE + '）')
                    return

                client2 = StreamHttpClient()
                await ensure_vdb_collection(client2, kb_id)
                vdb_data = {"colname": kb_id, "data": [
                    {"id": doc_id + "_c" + str(i), "vector": emb, "text": chunks[i]}
                    for i, emb in enumerate(embeddings)]}
                resp3 = await client2.request('POST', VDB_BASE + '/v1/upsert', json=vdb_data)
                vdb_ret = json.loads(resp3)
                if vdb_ret.get('status') != 'SUCCEEDED':
                    await _fail(db, _dbn, doc_id,
                        'VDB upsert 失败 @ ' + VDB_BASE + ': ' + str(vdb_ret)[:300])
                    return
                vector_ids = [doc_id + "_c" + str(i) for i in range(len(embeddings))]

                async with db.sqlorContext(_dbn) as sor:
                    for i, chunk_text in enumerate(chunks):
                        vid = vector_ids[i] if i < len(vector_ids) else ''
                        await sor.sqlExe(
                            "INSERT INTO rag_document_chunks (id, doc_id, kb_id, chunk_index, content, vector_id, created_at) "
                            "VALUES (${id}$, ${doc_id}$, ${kb_id}$, ${idx}$, ${content}$, ${vid}$, NOW())",
                            {"id": doc_id + "_c" + str(i), "doc_id": doc_id, "kb_id": kb_id,
                             "idx": i, "content": chunk_text[:2000], "vid": vid})
                    await sor.sqlExe("COMMIT", {})
                chunks_n = len(chunks)
    except Exception as e:
        await _fail(db, _dbn, doc_id, 'ingest 异常: ' + repr(e)[:300])
        return

    # --- finalize: mark document done + update KB chunk counts ---
    meta_json = json.dumps(meta_parts, ensure_ascii=False)
    try:
        async with db.sqlorContext(_dbn) as sor:
            await sor.sqlExe(
                "UPDATE rag_documents SET status='done', chunk_count=${chunks}$, metadata=${meta}$, updated_at=NOW() WHERE id=${id}$",
                {"id": doc_id, "chunks": chunks_n, "meta": meta_json})
            if chunks_n:
                await sor.sqlExe(
                    "UPDATE rag_knowledge_bases SET chunk_count=chunk_count+${n}$ WHERE id=${kb_id}$",
                    {"n": chunks_n, "kb_id": kb_id})
    except: pass
