# -*- coding:utf-8 -*-
"""在线多模态引擎：qwen3-vl-embedding / qwen3-vl-rerank（阿里 DashScope 原生 API）。
与 CLIP（GPU 本地）并存：kb.embedding_engine=qwen3-vl-embedding 走在线，
clip-vith14 仍走 GPU /mme 端点（有 GPU 的环境保留本地部署）。
配置来自 rag_engine_configs：engine_type=mm_embedding / mm_rerank，空=能力屏蔽。
"""
import json
from traceback import format_exc
from ahserver.serverenv import ServerEnv
from appPublic.log import exception
from sqlor.dbpools import get_sor_context

NATIVE_EMB_PATH = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
NATIVE_RR_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
DEFAULT_MM_DIM = 2560
TEXT_DIM = 1024


async def get_mm_cfg(engine_type="mm_embedding"):
    """读引擎配置，返回 {model_id, api_base, api_key 明文, dim}；未配置/空 → None（能力屏蔽）。"""
    from appPublic.rc4 import unpassword
    env = ServerEnv()
    try:
        async with get_sor_context(env, 'rag') as sor:
            recs = await sor.sqlExe(
                "SELECT model_name, endpoint_url, api_key, config_json FROM rag_engine_configs "
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
        dim = DEFAULT_MM_DIM
        try:
            cj = json.loads(getattr(r, "config_json", "") or "{}")
            dim = int(cj.get("dim", DEFAULT_MM_DIM))
        except Exception:
            pass
        return {"model_id": model_id, "api_base": api_base.rstrip("/"), "api_key": api_key, "dim": dim}
    except Exception as e:
        exception(f"mm cfg read failed ({engine_type}): {e}")
        return None


async def _post_json(url, payload, headers, timeout=60):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.post(url, json=payload, headers=headers) as resp:
            return await resp.json()


async def vl_embed_contents(contents, engine_type="mm_embedding"):
    """单条多模态向量化。contents 例：[{"text": "..."}] 或 [{"image": b64dataurl}] 或图文混合。"""
    cfg = await get_mm_cfg(engine_type)
    if not cfg:
        return None
    url = cfg["api_base"] + NATIVE_EMB_PATH
    headers = {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"}
    try:
        data = await _post_json(url, {"model": cfg["model_id"], "input": {"contents": contents}}, headers)
        embs = (data.get("output") or {}).get("embeddings") or []
        return embs[0]["embedding"] if embs and embs[0].get("embedding") else None
    except Exception as e:
        exception(f"vl embed failed: {e}, {format_exc()}")
        return None


async def vl_embed_texts(texts, engine_type="mm_embedding"):
    """批量纯文本向量化（走 vl 原生 API，contents=[{"text":t}]）。"""
    cfg = await get_mm_cfg(engine_type)
    if not cfg:
        return []
    url = cfg["api_base"] + NATIVE_EMB_PATH
    headers = {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"}
    out = []
    for t in texts:
        try:
            data = await _post_json(url, {"model": cfg["model_id"], "input": {"contents": [{"text": t}]}}, headers)
            embs = (data.get("output") or {}).get("embeddings") or []
            out.append(embs[0]["embedding"] if embs and embs[0].get("embedding") else None)
        except Exception as e:
            exception(f"vl embed text failed: {e}")
            out.append(None)
    return out


async def vl_embed_image_bytes(image_bytes, engine_type="mm_embedding"):
    """图片 bytes → base64 data url → 在线向量化。"""
    import base64
    b64 = base64.b64encode(image_bytes).decode()
    return await vl_embed_contents([{"image": "data:image/jpeg;base64," + b64}], engine_type)


async def vl_rerank(query, documents):
    """在线多模态重排。query: 文本 str；documents: list[str] 或 [{\"image\": ...}]。
    未配置/失败返回 None（调用方回退召回分排序）。"""
    cfg = await get_mm_cfg("mm_rerank")
    if not cfg:
        return None
    base = cfg["api_base"]
    if "compatible" in base:
        base = base.replace("/compatible-mode", "").replace("/compatible-api", "")
    if "/api/v1" not in base:
        base = base + "/api/v1"
    url = base + NATIVE_RR_PATH.replace("/api/v1", "")
    headers = {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"}
    payload = {"model": cfg["model_id"],
               "input": {"query": query, "documents": documents},
               "parameters": {"top_n": len(documents), "return_documents": False}}
    try:
        data = await _post_json(url, payload, headers, timeout=30)
        results = (data.get("output") or {}).get("results") or []
        if not results:
            return None
        scores = [0.0] * len(documents)
        for it in results:
            idx = it.get("index")
            if isinstance(idx, int) and 0 <= idx < len(scores):
                scores[idx] = it.get("relevance_score", 0)
        return {"scores": scores}
    except Exception as e:
        exception(f"vl rerank failed: {e}")
        return None


async def vdb_baseurl():
    """VDB 服务地址：读 upapp.rag-vdb（生产已切内网 9186），不再硬编码域名。"""
    env = ServerEnv()
    async with get_sor_context(env, 'rag') as sor:
        recs = await sor.sqlExe("SELECT baseurl FROM upapp WHERE id='rag-vdb'", {})
    if not recs:
        raise Exception("upapp rag-vdb not configured")
    return (recs[0].baseurl or "").rstrip("/")


async def vdb_call(apiname, payload, timeout=20):
    """直调 VDB HTTP 接口（createcollection/upsert），URL 来自 upapp。"""
    import aiohttp
    base = await vdb_baseurl()
    url = f"{base}/v1/{apiname}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.post(url, json=payload, headers={"Content-Type": "application/json"}) as resp:
            return await resp.json()
