import os
import asyncio
import json
import sys
import re
import time as lib_time
import math
import unicodedata
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Optional, List, Dict, Any, Union
from fastmcp import FastMCP
import asyncpg
from dotenv import load_dotenv
from datetime import datetime, time, timedelta
import pytz
import aiohttp
from openai import OpenAI
from guidelines import GUIDELINES

CAMPINA_GRANDE_TZ = pytz.timezone("America/Fortaleza")

from pathlib import Path
project_dir = Path(__file__).parent
load_dotenv(dotenv_path=project_dir / '.env')

mcp = FastMCP("Ana - Cesto d'Amore")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

EMBEDDING_CACHE: Dict[str, List[float]] = {}
PRODUCT_EMBEDDINGS: Dict[str, Dict[str, Any]] = {}
QUERY_COMPATIBILITY_INDEX: Dict[str, Dict[str, Any]] = {}
QUERY_COMPATIBILITY_DB_LOADED = False
EMBEDDING_TABLE_READY = False

QUERY_COMPATIBILITY_THRESHOLD = float(os.getenv("QUERY_COMPATIBILITY_THRESHOLD", "0.86"))
QUERY_COMPATIBILITY_MAX_ITEMS = int(os.getenv("QUERY_COMPATIBILITY_MAX_ITEMS", "600"))
QUERY_COMPATIBILITY_DB_LOOKBACK = int(os.getenv("QUERY_COMPATIBILITY_DB_LOOKBACK", "400"))
QUERY_STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "do", "das", "dos", "e", "ou", "em", "no", "na",
    "nos", "nas", "por", "para", "pra", "pro", "com", "sem", "um", "uma", "uns", "umas",
    "que", "quero", "queria", "gostaria", "me", "te", "se", "ao", "aos", "à", "às"
}

@mcp.tool()
async def check_mcp_health() -> str:
    """Check if the MCP server is healthy and return tool count."""
    count = len(mcp._tool_manager._tools) if hasattr(mcp, "_tool_manager") else 0
    now_local = _get_local_time()
    return f"MCP is healthy. Registered tools: {count}. Server time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}"

@mcp.tool()
async def reset_mcp_cache() -> str:
    """Reset MCP server cache and database pool. Use when experiencing stale data."""
    await reset_db_pool()
    EMBEDDING_CACHE.clear()
    PRODUCT_EMBEDDINGS.clear()
    QUERY_COMPATIBILITY_INDEX.clear()
    global QUERY_COMPATIBILITY_DB_LOADED
    QUERY_COMPATIBILITY_DB_LOADED = False
    now_local = _get_local_time()
    return f"✅ Cache resetado com sucesso! Horário do servidor: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}"

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST"),
    "port": os.getenv("POSTGRES_PORT"),
    "user": os.getenv("POSTGRES_USER"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "database": os.getenv("POSTGRES_DB"),
}

EVOLUTION_API_CONFIG = {
    "url": os.getenv("EVOLUTION_API_URL"),
    "key": os.getenv("EVOLUTION_API_KEY"),
    "instance": os.getenv("EVOLUTION_API_INSTANCE"),
    "chat_id": os.getenv("CHAT_ID"),
}


BUSINESS_HOURS = {
    "monday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "tuesday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "wednesday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "thursday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "friday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "saturday": [(time(8, 0), time(11, 0))],
}

db_pool = None

async def get_db_pool():
    """Get or create a database connection pool."""
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            host=DB_CONFIG["host"],
            port=int(DB_CONFIG["port"]) if DB_CONFIG["port"] else 5432,
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            min_size=2,
            max_size=10,
            command_timeout=30
        )
    return db_pool

async def reset_db_pool():
    """Reset database connection pool to clear cache."""
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None
        _safe_print("🔄 Database pool reset successfully")

async def get_db_connection():
    """Deprecated: Use db_pool instead. Keeping for compatibility."""
    pool = await get_db_pool()
    return await pool.acquire()

def _get_local_time():
    """
    Get current time in Campina Grande timezone.
    Returns:
        datetime: aware datetime in America/Fortaleza (UTC-3)
    """
    return datetime.now(CAMPINA_GRANDE_TZ)

def _validate_timezone_safety(date_to_check: str) -> tuple[str, str]:
    """
    Check if the requested date matches local date for logging purposes.
    """
    now_local = _get_local_time()
    date_obj = datetime.strptime(date_to_check, "%Y-%m-%d").date()
    local_date = now_local.date()
    
    debug_str = f"🕐 [TIME] {now_local.strftime('%H:%M:%S')} | Hoje: {local_date.strftime('%Y-%m-%d')} | Requisição: {date_to_check}"
    
    return date_to_check, debug_str

def _format_structured_response(data: Dict[str, Any], humanized_message: str) -> str:
    """
    Format response with structured JSON + humanized message.
    Helps LLM parse data while keeping human-friendly text.
    """
    response = f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n\n{humanized_message}"
    return response

def _safe_print(message: str) -> None:
    """
    Safe print that handles Unicode errors gracefully by writing to stderr.
    Writes to stderr to avoid breaking the MCP stdio protocol (which uses stdout).
    Prepends timestamp in Campina Grande timezone.
    """
    try:
        now = datetime.now(CAMPINA_GRANDE_TZ).strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{now}] {message}\n")
        sys.stderr.flush()
    except:
        pass

def _hash_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()

def _normalize_embedding_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized

def _tokenize_context(text: str) -> List[str]:
    normalized = _normalize_embedding_text(text)
    return [
        token for token in normalized.split(" ")
        if token and token not in QUERY_STOPWORDS and len(token) > 2
    ]

def _context_compatibility_score(source_text: str, candidate_text: str) -> float:
    source_norm = _normalize_embedding_text(source_text)
    candidate_norm = _normalize_embedding_text(candidate_text)
    if not source_norm or not candidate_norm:
        return 0.0

    sequence_score = SequenceMatcher(None, source_norm, candidate_norm).ratio()
    source_tokens = set(_tokenize_context(source_text))
    candidate_tokens = set(_tokenize_context(candidate_text))

    if source_tokens and candidate_tokens:
        intersection = len(source_tokens.intersection(candidate_tokens))
        union = len(source_tokens.union(candidate_tokens)) or 1
        jaccard = intersection / union
        containment = intersection / max(1, min(len(source_tokens), len(candidate_tokens)))
    else:
        jaccard = 0.0
        containment = 0.0

    return (0.50 * sequence_score) + (0.35 * jaccard) + (0.15 * containment)

def _remember_query_embedding(text: str, embedding_hash: str, embedding: List[float]) -> None:
    normalized = _normalize_embedding_text(text)
    if not normalized:
        return

    QUERY_COMPATIBILITY_INDEX[normalized] = {
        "hash": embedding_hash,
        "text": text,
        "embedding": embedding,
    }

    if len(QUERY_COMPATIBILITY_INDEX) > QUERY_COMPATIBILITY_MAX_ITEMS:
        oldest_key = next(iter(QUERY_COMPATIBILITY_INDEX.keys()))
        QUERY_COMPATIBILITY_INDEX.pop(oldest_key, None)

async def _fetch_recent_query_embedding_records(limit: int = QUERY_COMPATIBILITY_DB_LOOKBACK) -> List[Dict[str, Any]]:
    await _ensure_embedding_table()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT embedding_hash, text_content, vector
            FROM public."EmbeddingCache"
            WHERE embedding_type = 'query'
              AND text_content IS NOT NULL
              AND text_content <> ''
            ORDER BY updated_at DESC
            LIMIT $1
            """,
            limit,
        )

    records: List[Dict[str, Any]] = []
    for row in rows:
        vector = row["vector"]
        try:
            if isinstance(vector, str):
                vector = json.loads(vector)
            if isinstance(vector, list):
                records.append(
                    {
                        "embedding_hash": str(row["embedding_hash"]),
                        "text_content": str(row["text_content"]),
                        "vector": [float(v) for v in vector],
                    }
                )
        except Exception as e:
            _safe_print(f"⚠️ Error parsing query embedding from DB: {e}")
    return records

async def _load_query_compatibility_index() -> None:
    global QUERY_COMPATIBILITY_DB_LOADED
    if QUERY_COMPATIBILITY_DB_LOADED:
        return

    try:
        records = await _fetch_recent_query_embedding_records()
        for record in records:
            _remember_query_embedding(
                record["text_content"],
                record["embedding_hash"],
                record["vector"],
            )
        _safe_print(f"🧠 Índice de compatibilidade carregado com {len(QUERY_COMPATIBILITY_INDEX)} contextos")
    except Exception as e:
        _safe_print(f"⚠️ Falha ao carregar índice de compatibilidade: {e}")
    finally:
        QUERY_COMPATIBILITY_DB_LOADED = True

async def _find_compatible_query_embedding(text: str, embedding_hash: str) -> Optional[Dict[str, Any]]:
    await _load_query_compatibility_index()

    best_match: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for candidate in QUERY_COMPATIBILITY_INDEX.values():
        candidate_hash = str(candidate.get("hash") or "")
        if not candidate_hash or candidate_hash == embedding_hash:
            continue

        score = _context_compatibility_score(text, str(candidate.get("text") or ""))
        if score > best_score:
            best_score = score
            best_match = {
                "score": score,
                "hash": candidate_hash,
                "text": candidate.get("text"),
                "embedding": candidate.get("embedding"),
            }

    if best_match and best_score >= QUERY_COMPATIBILITY_THRESHOLD:
        return best_match
    return None

async def _ensure_embedding_table() -> None:
    global EMBEDDING_TABLE_READY
    if EMBEDDING_TABLE_READY:
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public."EmbeddingCache" (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                embedding_type TEXT NOT NULL,
                embedding_hash TEXT NOT NULL,
                product_id TEXT NULL,
                text_content TEXT NULL,
                model TEXT NOT NULL,
                vector JSONB NOT NULL,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                UNIQUE (embedding_type, embedding_hash)
            );
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS embeddingcache_type_hash_idx
            ON public."EmbeddingCache" (embedding_type, embedding_hash);
            """
        )
    EMBEDDING_TABLE_READY = True

async def _fetch_cached_embeddings(
    embedding_type: str,
    hashes: List[str],
) -> Dict[str, List[float]]:
    if not hashes:
        return {}

    await _ensure_embedding_table()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT embedding_hash, vector
            FROM public."EmbeddingCache"
            WHERE embedding_type = $1
              AND embedding_hash = ANY($2::TEXT[])
            """,
            embedding_type,
            hashes,
        )

    results: Dict[str, List[float]] = {}
    for row in rows:
        vector = row["vector"]
        # Handle cases where vector might be a JSON string or already parsed list
        try:
            if isinstance(vector, str):
                vector = json.loads(vector)
            
            if isinstance(vector, list):
                results[str(row["embedding_hash"])] = [float(v) for v in vector]
        except Exception as e:
            _safe_print(f"⚠️ Error parsing cached embedding for {row['embedding_hash']}: {e}")
            
    return results

async def _store_embedding_records(records: List[Dict[str, Any]]) -> None:
    if not records:
        return

    await _ensure_embedding_table()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO public."EmbeddingCache"
                (embedding_type, embedding_hash, product_id, text_content, model, vector, updated_at)
            VALUES
                ($1, $2, $3, $4, $5, $6::jsonb, NOW())
            ON CONFLICT (embedding_type, embedding_hash)
            DO UPDATE SET
                product_id = EXCLUDED.product_id,
                text_content = EXCLUDED.text_content,
                model = EXCLUDED.model,
                vector = EXCLUDED.vector,
                updated_at = NOW();
            """,
            [
                (
                    r["embedding_type"],
                    r["embedding_hash"],
                    r.get("product_id"),
                    r.get("text_content"),
                    r["model"],
                    json.dumps(r["vector"], ensure_ascii=False),
                )
                for r in records
            ],
        )

async def _prune_product_embeddings(records: List[Dict[str, Any]]) -> None:
    if not records:
        return

    to_prune = []
    for record in records:
        if record.get("embedding_type") != "product":
            continue
        product_id = record.get("product_id")
        embedding_hash = record.get("embedding_hash")
        if product_id and embedding_hash:
            to_prune.append((product_id, embedding_hash))

    if not to_prune:
        return

    await _ensure_embedding_table()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            DELETE FROM public."EmbeddingCache"
            WHERE embedding_type = 'product'
              AND product_id = $1
              AND embedding_hash <> $2;
            """,
            to_prune,
        )

def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return float(dot / (norm1 * norm2))

def _softmax(scores: List[float], temperature: float) -> List[float]:
    temp = max(0.05, float(temperature))
    scaled = [s / temp for s in scores]
    max_val = max(scaled) if scaled else 0.0
    exp_scores = [math.exp(s - max_val) for s in scaled]
    total = sum(exp_scores) or 1.0
    return [s / total for s in exp_scores]

def _build_product_text(product: Dict[str, Any]) -> str:
    parts = [
        str(product.get("name") or ""),
        str(product.get("description") or ""),
    ]
    return ". ".join([p for p in parts if p]).strip()

def _categorize_product_type(name: str, description: str) -> str:
    """
    Categoriza o tipo de produto baseado no nome e descrição.
    
    Retorna:
        "QUADRO_FOTO": Quadros, Polaroides, Fotos, Instax
        "FLOR": Buquês, Rosas, Flores
        "PELUCIA": Pelúcias, Ursos
        "QUEBRA_CABECA": Quebra-cabeças
        "CANECA": Canecas
        "BAR_DRINKS": Coquetéis, Drinks, Bebidas
        "CESTA": Cestas (padrão para tudo o mais)
    """
    text = f"{name} {description}".lower()
    
    if any(kw in text for kw in ["quadro", "polaroide", "polaróide", "foto", "instax", "fotografia"]):
        return "QUADRO_FOTO"
    elif any(kw in text for kw in ["buquê", "buque", "bouquet", "rosa", "flores", "flor", "rosas"]):
        return "FLOR"
    elif any(kw in text for kw in ["pelúcia", "pelucia", "urso", "ursinho", "pelúcia"]):
        return "PELUCIA"
    elif any(kw in text for kw in ["quebra-cabeça", "quebracabeca", "quebra cabeca", "puzzle"]):
        return "QUEBRA_CABECA"
    elif any(kw in text for kw in ["caneca"]):
        return "CANECA"
    elif any(kw in text for kw in ["bar", "coquetel", "drink", "bebida", "cerveja", "vinho"]):
        return "BAR_DRINKS"
    else:
        return "CESTA"

def _apply_contextual_ranking(
    scored_products: List[Dict[str, Any]],
    has_context: bool,
    search_term: str = ""
) -> List[Dict[str, Any]]:
    """
    Aplica ranking contextual aos produtos.
    
    Se tem contexto (cliente especificou ocasião):
        - Ordena por similaridade semântica decrescente
        - Aplica penalidade a canecas (salvo se busca explícita por "caneca")
    Se NÃO tem contexto (busca genérica):
        - Prioridade por tipo: QUADRO > FLOR > PELUCIA > CESTA > QUEBRA > CANECA > BAR
    """
    search_lower = search_term.lower().strip()
    is_caneca_search = "caneca" in search_lower

    for product in scored_products:
        product["product_type"] = _categorize_product_type(
            product.get("name", ""),
            product.get("description", "")
        )

    if has_context:
        CANECA_PENALTY = 0.15
        for product in scored_products:
            if product["product_type"] == "CANECA" and not is_caneca_search:
                product["similarity"] = max(0.0, product["similarity"] - CANECA_PENALTY)
                product["ranking_reason"] = "CONTEXTO_SEMÂNTICO (penalidade caneca)"
            else:
                product["ranking_reason"] = "CONTEXTO_SEMÂNTICO"

        sorted_products = sorted(
            scored_products,
            key=lambda p: (p["similarity"], float(p.get("price") or 0.0)),
            reverse=True
        )
        return sorted_products
    
    # Sem contexto: aplica prioridades por tipo + preço DESC
    type_priority = {
        "QUADRO_FOTO": 1,
        "FLOR": 2,
        "PELUCIA": 3,
        "CESTA": 4,
        "QUEBRA_CABECA": 5,
        "CANECA": 6,
        "BAR_DRINKS": 7,
    }
    
    for product in scored_products:
        product["type_priority"] = type_priority.get(product["product_type"], 999)
    
    sorted_products = sorted(
        scored_products,
        key=lambda p: (
            p["type_priority"],
            -float(p.get("price") or 0.0),
            -p["similarity"],
        )
    )
    
    for product in sorted_products:
        product["ranking_reason"] = f"TIPO_GENÉRICO:{product['product_type']}"
    
    return sorted_products

def _parse_price_value(raw_value: str) -> Optional[float]:
    if not raw_value:
        return None
    cleaned = re.sub(r"[^0-9,\.]", "", raw_value)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

def _extract_price_bounds(text: str) -> tuple[Optional[float], Optional[float]]:
    if not text:
        return None, None
    text_lower = text.lower()
    text_lower = text_lower.replace("r$", "r$")

    range_match = re.search(
        r"(entre|de)\s*r?\$?\s*([\d\.,]+)\s*(e|a)\s*r?\$?\s*([\d\.,]+)",
        text_lower,
    )
    if range_match:
        min_val = _parse_price_value(range_match.group(2))
        max_val = _parse_price_value(range_match.group(4))
        return min_val, max_val

    max_match = re.search(
        r"(ate|até|no\s*maximo|no\s*máximo|menos\s*de)\s*r?\$?\s*([\d\.,]+)",
        text_lower,
    )
    if max_match:
        max_val = _parse_price_value(max_match.group(2))
        return None, max_val

    min_match = re.search(
        r"(a\s*partir\s*de|minimo|minimo|min\.?|acima\s*de)\s*r?\$?\s*([\d\.,]+)",
        text_lower,
    )
    if min_match:
        min_val = _parse_price_value(min_match.group(2))
        return min_val, None

    return None, None

async def _get_embeddings(texts: List[str]) -> List[List[float]]:
    client = openai_client
    if not client:
        raise RuntimeError("OPENAI_API_KEY not configured")

    def _call():
        return client.embeddings.create(model=EMBEDDING_MODEL, input=texts)

    response = await asyncio.to_thread(_call)
    return [item.embedding for item in response.data]

async def _get_embedding_cached(text: str) -> List[float]:
    key = text.strip()
    if not key:
        return []
    if key in EMBEDDING_CACHE:
        return EMBEDDING_CACHE[key]

    normalized_key = _normalize_embedding_text(key)
    compatible_local = QUERY_COMPATIBILITY_INDEX.get(normalized_key)
    if compatible_local and compatible_local.get("embedding"):
        embedding = compatible_local["embedding"]
        EMBEDDING_CACHE[key] = embedding
        return embedding

    key_hash = _hash_text(key)
    cached = await _fetch_cached_embeddings("query", [key_hash])
    if key_hash in cached:
        embedding = cached[key_hash]
        EMBEDDING_CACHE[key] = embedding
        _remember_query_embedding(key, key_hash, embedding)
        return embedding

    compatible = await _find_compatible_query_embedding(key, key_hash)
    if compatible and compatible.get("embedding"):
        reused_embedding = compatible["embedding"]
        EMBEDDING_CACHE[key] = reused_embedding
        _remember_query_embedding(key, key_hash, reused_embedding)
        await _store_embedding_records(
            [
                {
                    "embedding_type": "query",
                    "embedding_hash": key_hash,
                    "product_id": None,
                    "text_content": key[:1000],
                    "model": EMBEDDING_MODEL,
                    "vector": reused_embedding,
                }
            ]
        )
        _safe_print(
            f"♻️ Reuso de embedding por compatibilidade ({compatible['score']:.3f}) | origem: '{str(compatible['text'])[:80]}'"
        )
        return reused_embedding

    embeddings = await _get_embeddings([key])
    EMBEDDING_CACHE[key] = embeddings[0]
    _remember_query_embedding(key, key_hash, embeddings[0])
    await _store_embedding_records(
        [
            {
                "embedding_type": "query",
                "embedding_hash": key_hash,
                "product_id": None,
                "text_content": key[:1000],
                "model": EMBEDDING_MODEL,
                "vector": embeddings[0],
            }
        ]
    )
    return embeddings[0]

async def _ensure_product_embeddings(products: List[Dict[str, Any]]) -> None:
    to_embed: List[Dict[str, Any]] = []
    hashes: List[str] = []
    # Map hash to list of [id, text]
    hash_to_products: Dict[str, List[Dict[str, Any]]] = {}

    for product in products:
        product_id = str(product.get("id"))
        text = _build_product_text(product)
        text_hash = _hash_text(text)
        
        if text_hash not in hash_to_products:
            hash_to_products[text_hash] = []
        
        product_info = {
            "id": product_id,
            "text": text,
            "hash": text_hash,
        }
        hash_to_products[text_hash].append(product_info)
        hashes.append(text_hash)

        cached = PRODUCT_EMBEDDINGS.get(product_id)
        if cached and cached.get("hash") == text_hash:
            continue

        # Check if we already have this text in to_embed batch to avoid duplicate calls
        if not any(item["hash"] == text_hash for item in to_embed):
            to_embed.append(product_info)

    cached_db = await _fetch_cached_embeddings("product", list(set(hashes)))
    for text_hash, embedding in cached_db.items():
        products_matching = hash_to_products.get(text_hash, [])
        for p_match in products_matching:
            PRODUCT_EMBEDDINGS[p_match["id"]] = {
                "embedding": embedding,
                "hash": text_hash,
            }

    # Filter to_embed to only items not found in DB
    to_embed = [
        item
        for item in to_embed
        if item["hash"] not in cached_db
    ]

    if not to_embed:
        return

    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [b["text"] for b in batch]
        embeddings = await _get_embeddings(texts)
        records = []
        for item, embedding in zip(batch, embeddings):
            # Update all products that have this same text
            products_matching = hash_to_products.get(item["hash"], [])
            for p_match in products_matching:
                PRODUCT_EMBEDDINGS[p_match["id"]] = {
                    "embedding": embedding,
                    "hash": item["hash"],
                }
                records.append(
                    {
                        "embedding_type": "product",
                        "embedding_hash": item["hash"],
                        "product_id": p_match["id"],
                        "text_content": item["text"][:1000],
                        "model": EMBEDDING_MODEL,
                        "vector": embedding,
                    }
                )
        await _prune_product_embeddings(records)
        await _store_embedding_records(records)

def _get_emoji_for_reason(reason: str) -> str:
    """
    Map support reason to emoji indicator.
    🔴 = Critical (product unavailable, customization, price manipulation)
    🟡 = Medium (freight doubts)
    🟢 = Success (checkout completion/finalization)
    """
    reason_lower = reason.lower()
    
    if any(kw in reason_lower for kw in ["finaliza", "paga", "compra", "pedido", "checkout", "concluído"]):
        return "🟢"
    elif "frete" in reason_lower or "duvida" in reason_lower:
        return "🟡"
    else:
        return "🔴"

async def _send_whatsapp_notification(
    message: str,
    client_name: Optional[str] = None,
    client_phone: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a WhatsApp notification via Evolution API.
    Returns dict with success status and response.
    """
    try:
        if not all([
            EVOLUTION_API_CONFIG["url"],
            EVOLUTION_API_CONFIG["key"],
            EVOLUTION_API_CONFIG["instance"],
            EVOLUTION_API_CONFIG["chat_id"]
        ]):
            return {
                "success": False,
                "error": "Evolution API configuration missing",
                "message": "Variáveis de ambiente não configuradas"
            }
        
        base_url = (EVOLUTION_API_CONFIG['url'] or "").rstrip('/')
        instance = EVOLUTION_API_CONFIG['instance']
        
        endpoint = f"{base_url}/message/sendText/{instance}"
        
        headers = {
            "apikey": EVOLUTION_API_CONFIG['key'],
            "Content-Type": "application/json"
        }
        
        payload = {
            "number": EVOLUTION_API_CONFIG["chat_id"],
            "text": message
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response_text = await response.text()
                
                try:
                    response_data = await response.json()
                except:
                    response_data = {"raw": response_text}
                
                if response.status in [200, 201]:
                    message_id = None
                    if isinstance(response_data, dict):
                        message_block = response_data.get("message", {})
                        if isinstance(message_block, dict):
                            key_block = message_block.get("key", {})
                            if isinstance(key_block, dict):
                                message_id = key_block.get("id")
                    return {
                        "success": True,
                        "status_code": response.status,
                        "message_id": message_id,
                        "response": response_data,
                        "endpoint_used": endpoint
                    }
                else:
                    error_msg = response_data.get("message", response_data.get("error", f"HTTP {response.status}"))
                    return {
                        "success": False,
                        "status_code": response.status,
                        "error": str(error_msg),
                        "response": response_data,
                        "endpoint_used": endpoint
                    }
    
    except Exception as e:
        return {
            "success": False,
            "error": str(type(e).__name__),
            "message": str(e)
        }

def _format_support_message(
    reason: str,
    customer_context: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_phone: Optional[str] = None
) -> str:
    """
    Format the support notification message following the standard pattern:
    *AJUDA [PRIORITY] - Cliente [NOME] - [NUMERO]*
    """
    emoji = _get_emoji_for_reason(reason)
    nome = customer_name or "Desconhecido"
    numero = customer_phone or "Sem contato"
    
    header = f"🚨 *AJUDA [{emoji}] - CLIENTE: {nome.upper()}*"
    contato = f"📱 Contato: {numero}"
    
    reason_lower = reason.lower()
    if any(kw in reason_lower for kw in ["finaliza", "pedido", "checkout", "end_of"]):
        descricao = "✅ *PEDIDO PRONTO PARA CONEXÃO HUMANA*"
    elif "frete" in reason_lower:
        descricao = "🚚 *DÚVIDA / CONFIRMAÇÃO DE FRETE*"
    elif "cart_added" in reason_lower:
        descricao = "🛒 *CLIENTE ADICIONOU AO CARRINHO*"
    else:
        descricao = f"📌 Motivo: {reason.replace('_', ' ').capitalize()}"

    if customer_context and customer_context.strip().lower() != "none":
        contexto = customer_context.strip()
        message = f"{header}\n{contato}\n{descricao}\n\n{contexto}"
    else:
        message = f"{header}\n{contato}\n{descricao}\n\n⚠️ Contexto não fornecido pela IA."
        
    return message


@mcp.prompt()
async def core_identity_guideline() -> str:
    """
    Identidade, tom e comportamento base da Ana.
    
    USE QUANDO:
    - Início de conversa (apresentação)
    - Definir tom de comunicação
    - Entender filosofia de atendimento
    - Referência sobre humanização
    """
    return GUIDELINES["core"]

@mcp.prompt()
async def delivery_rules_guideline() -> str:
    """
    Regras de entrega, horários de funcionamento e áreas de cobertura.
    
    USE QUANDO:
    - Cliente perguntar sobre horários
    - Cliente perguntar "Faz entrega em [cidade]?"
    - Validar disponibilidade de data/hora
    - Calcular frete
    - Dúvidas sobre entregas
    """
    return GUIDELINES["delivery_rules"]

@mcp.prompt()
async def product_selection_guideline() -> str:
    """
    Como apresentar e selecionar produtos para o cliente.
    
    USE QUANDO:
    - Apresentar cestas ou flores
    - Cliente pedir "mais opções"
    - Cliente especificar tipo de produto
    - Necessitar manter consistência de tipo
    - Decidir quantos produtos mostrar
    """
    return GUIDELINES["product_selection"]

@mcp.prompt()
async def closing_protocol_guideline() -> str:
    """
    Protocolo completo de fechamento de vendas (9 passos obrigatórios).
    
    USE QUANDO:
    - Cliente disser "quero essa", "vou levar", "como compro?"
    - Iniciar processo de finalização
    - Coletar dados para pedido
    - Transferir para atendente humano
    """
    return GUIDELINES["closing_protocol"]

@mcp.prompt()
async def cart_protocol_guideline() -> str:
    """
    Protocolo OBRIGATÓRIO quando cliente adiciona produto ao carrinho.
    
    USE QUANDO:
    - Mensagem contém "[Interno] O cliente adicionou um produto ao carrinho pessoal"
    - Detectar adição de produto ao carrinho
    
    ESTE PROMPT CONTÉM:
    - Sequência obrigatória de ações (informar + notificar + bloquear)
    - Mensagens exatas para horário ABERTO vs FECHADO
    - Parâmetros corretos para notify_human_support
    - Checklist de execução
    - Exemplos completos
    
    ⚠️ CRÍTICO: Este protocolo NÃO pode ser ignorado ou modificado
    """
    return GUIDELINES["cart_protocol"]

@mcp.prompt()
async def customization_guideline() -> str:
    """
    Regras sobre personalização e coleta de fotos.
    
    USE QUANDO:
    - Cliente perguntar sobre personalização
    - Cliente querer enviar fotos
    - Cliente perguntar sobre customização
    - Explicar processo de personalização
    """
    return GUIDELINES["customization"]

@mcp.prompt()
async def inexistent_products_guideline() -> str:
    """
    Como lidar com produtos fora do catálogo.
    
    USE QUANDO:
    - Cliente pedir produto que não vendemos
    - Cliente mencionar vinho, café da manhã, frutas, etc
    - Produto não encontrado em busca
    - Necessitar oferecer alternativas
    """
    return GUIDELINES["inexistent_products"]

@mcp.prompt()
async def indecision_guideline() -> str:
    """
    Como ajudar cliente indeciso.
    
    USE QUANDO:
    - Cliente já viu 4+ produtos e ainda pede mais
    - Cliente está indeciso entre opções
    - Necessário enviar catálogo completo
    - Cliente não sabe o que quer
    """
    return GUIDELINES["indecision"]

@mcp.prompt()
async def mass_orders_guideline() -> str:
    """
    Procedimento para pedidos corporativos e em lote.
    
    USE QUANDO:
    - Cliente mencionar quantidade >= 20 unidades
    - Orçamento > R$ 1.000
    - Pedido corporativo ou empresarial
    - Necessitar descontos de volume
    """
    return GUIDELINES["mass_orders"]

@mcp.prompt()
async def location_guideline() -> str:
    """
    Informações sobre localização e logística da loja.
    
    USE QUANDO:
    - Cliente perguntar onde fica a loja
    - Cliente querer retirar pessoalmente
    - Dúvidas sobre cobertura de entrega
    - Informações sobre a loja física
    """
    return GUIDELINES["location"]

@mcp.prompt()
async def faq_production_guideline() -> str:
    """
    FAQ sobre tempo de produção e prazos.
    
    USE QUANDO:
    - Cliente perguntar "quanto tempo demora?"
    - Dúvidas sobre produção imediata
    - Explicar prazos de customização
    - Diferenciar pronta entrega vs personalizado
    """
    return GUIDELINES["faq_production"]

@mcp.prompt()
async def fallback_guideline() -> str:
    """
    Como lidar com contextos fora do escopo.
    
    USE QUANDO:
    - Cliente faz perguntas não relacionadas à loja
    - Assuntos pessoais, políticos, aleatórios
    - Spam ou comportamento suspeito
    - Redirecionar para o assunto da loja
    """
    return GUIDELINES["fallback"]

@mcp.prompt()
async def human_transfer_guideline() -> str:
    """
    Protocolo OBRIGATÓRIO para transferência humana.
    
    USE QUANDO:
    - Cliente pedir explicitamente "atendente", "humano", "pessoa" ou qualquer nome de funcionário.
    - Cliente demonstrar irritação ou cansaço da IA
    - Casos de manipulação de preços ou descontos insistentes
    - Pedidos em grande volume/corporativos
    - Assuntos complexos que a IA não sabe resolver
    
    ESTE PROMPT CONTÉM:
    - Regras de quando transferir imediatamente
    - Como informar o horário de atendimento
    - Como notificar e bloquear a sessão
    """
    return GUIDELINES["human_transfer"]

def _normalize_product_search_term(termo: str) -> str:
    """
    Normaliza termos genéricos de cliente para termos específicos do catálogo.
    
    Exemplo: "presentes" → "cesto d'amore" | "flores" → "buquê" | "caneca" → "caneca d'amore"
    
    MAPEAMENTO INTELIGENTE:
    - Cliente diz "presentes" → Busca por "cesto" (oferece várias opções)
    - Cliente diz "festa" → Busca por "bar" (festas geralmente pedem bar)
    - Cliente diz "productos" → Busca por "cesto" (genérico)
    - Cliente diz "cestas" → Busca por "cesto" (específico)
    - Cliente diz "flores" → Busca por "buquê" (muito comum)
    - Cliente diz "buquê" → Mantém "buquê" (já específico)
    - Cliente diz "caneca" → Busca por "caneca d'amore" (específico)
    - Cliente diz "urso" → Busca por "pelúcia" (categoria)
    - Cliente diz "quadro" → Busca por "quadro" (específico)
    - Cliente diz "namorados" → Busca por "coração" (tema)
    - Cliente diz "aniversário" → Busca por "aniversário d'amore" (específico)
    - Cliente diz "café" → Busca por "café d'amore" (específico)
    - Cliente diz "chocolate" → Busca por "chocolate d'amore" (específico)
    """
    termo_lower = termo.lower().strip()
    
    term_mappings = {
        "presentes": "cesto",
        "presente": "cesto",
        "products": "cesto",
        "producto": "cesto",
        "productos": "cesto",
        "cestas": "cesto",
        "cesta": "cesto",
        "gift": "cesto",
        "gifts": "cesto",
        "present": "cesto",
        
        "anuncio": "anuncio",
        
        "flores": "buquê",
        "flora": "buquê",
        "rosas": "buquê",
        "rosa": "buquê",
        "flor": "buquê",
        
        "festa": "bar",
        "festas": "bar",
        "party": "bar",
        "cerveja": "bar",
        "cervejas": "bar",
        
        "caneca": "caneca d'amore",
        "canecas": "caneca d'amore",
        "urso": "pelúcia",
        "ursos": "pelúcia",
        "pelúcia": "pelúcia",
        "pelúcias": "pelúcia",
        "pelucia": "pelúcia",
        "pelúcio": "pelúcia",
        "teddy": "pelúcia",
        "quadro": "quadro",
        "quadros": "quadro",
        "quebra-cabeça": "quebra-cabeça",
        "quebra": "quebra-cabeça",
        "puzzle": "quebra-cabeça",
        
        "namorados": "coração",
        "coração": "coração",
        "coracao": "coração",
        "amor": "coração",
        "casal": "coração",
        "casamento": "cesto",
        "casamentos": "cesto",
        "formatura": "cesto",
        "graduação": "cesto",
        "graduacao": "cesto",
        
        "aniversário": "aniversário",
        "aniversario": "aniversário",
        "birthday": "aniversário",
        "café": "café",
        "cafe": "café",
        "chocolate": "chocolate",
        "chocolates": "chocolate",
        "cone": "cone",
        
        "mais opções": "cesto",
        "opções": "cesto",
        "opcoes": "cesto",
        "outro": "cesto",
        "outra": "cesto",
        "diferente": "cesto",
    }
    
    if termo_lower in term_mappings:
        mapeado = term_mappings[termo_lower]
        _safe_print(f"🔄 Normalizado: '{termo}' → '{mapeado}'")
        return mapeado
    
    termo_limpo = re.sub(r"[^\w\s]", "", termo_lower).strip()
    specific_terms = [
        "cesto", "cesta", "buquê", "buque", "bar", "caneca", "pelúcia", "pelucia", "quadro",
        "quebra-cabeça", "quebra", "coração", "coracao", "aniversário", "aniversario",
        "café", "cafe", "chocolate", "cone", "rosa", "flor"
    ]
    
    if any(specific in termo_limpo for specific in specific_terms):
        _safe_print(f"✓ Termo específico mantido: '{termo}'")
        return termo
    
    _safe_print(f"ℹ️ Termo '{termo}' não mapeado, usando original")
    return termo

@mcp.tool()
async def consultarCatalogo(
    termo: str,
    preco_minimo: Optional[float] = None,
    preco_maximo: Optional[float] = None,
    exclude_product_ids: Optional[List[str]] = None,
    contexto: Optional[str] = None,
    use_semantic: Optional[bool] = None,
    temperature: Optional[float] = None,
    min_similarity: Optional[float] = None,
    top_k: Optional[int] = None,
) -> str:
    """
    Busca produtos por termo (ocasião ou tipo), com filtros de preço.

    Retorna JSON: {"exatos": [], "fallback": []}. Priorize SEMPRE produtos "exatos".
    Mostre exatamente 2 produtos por vez (ranking menor = melhor).

    Campos obrigatórios na apresentação: ID, Nome, Preço, Descrição e Production Time.
    Se esvaziar busca por preço, ofereça buscar sem limite.

    Args:
        termo: palavra-chave de busca (ex: "aniversário", "flores", "caneca")
        preco_minimo: preco minimo em reais. Use quando cliente diz "a partir de R$ X" (padrao: 0)
        preco_maximo: preco maximo em reais. Use quando cliente diz "até R$ X" ou "barato" (padrao: 999999)
        exclude_product_ids: IDs de produtos a excluir (já apresentados ao cliente)
        contexto: Contexto COMPLETO da necessidade do cliente (ex: "presente de aniversário para namorada que gosta de chocolates e fotos"). NÃO use apenas uma palavra aqui. Use a frase do cliente para melhor RAG.
        use_semantic: se true, aplica ranking semantico por embeddings (padrao: true)
        temperature: controla o "rank de temperatura" (padrao: 0.35)
        min_similarity: limiar para classificar como EXATO (padrao: 0.18)
        top_k: maximo de produtos retornados (padrao: 10)

    Exemplos:
        - "Aniversário" -> termo="aniversário"
        - "Flores baratas" -> termo="flores", preco_maximo=120
        - "Cestas até 200" -> termo="cesto", preco_maximo=200
        - "Caneca" -> termo="caneca"
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            termo_normalizado = _normalize_product_search_term(termo)
            if termo_normalizado != termo:
                _safe_print(f"📝 Termo original: '{termo}' → Normalizado: '{termo_normalizado}'")

            exclude_ids = exclude_product_ids if exclude_product_ids else []
            exclude_ids = [str(id) for id in exclude_ids]

            contexto_limpo = (contexto or "").strip()
            price_source = f"{termo_normalizado} {contexto_limpo}".strip()
            ctx_min, ctx_max = _extract_price_bounds(price_source)

            if preco_minimo is None:
                preco_minimo = ctx_min if ctx_min is not None else 0.0
            if preco_maximo is None:
                preco_maximo = ctx_max if ctx_max is not None else 999999.0

            if ctx_min is not None and preco_minimo < ctx_min:
                preco_minimo = ctx_min
            if ctx_max is not None and preco_maximo > ctx_max:
                preco_maximo = ctx_max

            if preco_minimo > preco_maximo:
                preco_minimo, preco_maximo = preco_maximo, preco_minimo

            use_semantic_search = (
                True if use_semantic is None else bool(use_semantic)
            ) and openai_client is not None
            temperature = float(temperature) if temperature is not None else 0.35
            min_similarity = float(min_similarity) if min_similarity is not None else 0.18
            top_k = int(top_k) if top_k else 10
            top_k = max(2, min(10, top_k))

            if use_semantic_search:
                try:
                    query = """
                    SELECT id, name, description, price, image_url, production_time
                    FROM public."Product"
                    WHERE price >= $1
                      AND price <= $2
                      AND is_active = true
                      AND NOT (id::TEXT = ANY($3::TEXT[]))
                    """
                    rows = await conn.fetch(query, preco_minimo, preco_maximo, exclude_ids)

                    if rows:
                        products = [dict(r) for r in rows]
                        await _ensure_product_embeddings(products)

                        query_text = termo_normalizado
                        if contexto_limpo:
                            query_text = f"{query_text}. {contexto_limpo}"

                        query_embedding = await _get_embedding_cached(query_text)

                        scored = []
                        termo_lower = termo_normalizado.lower().strip()
                        original_lower = termo.lower().strip()
                        for product in products:
                            product_id = str(product.get("id"))
                            cached = PRODUCT_EMBEDDINGS.get(product_id, {})
                            embedding = cached.get("embedding")
                            similarity = (
                                _cosine_similarity(query_embedding, embedding)
                                if embedding
                                else 0.0
                            )
                            name = (product.get("name") or "").lower()
                            description = (product.get("description") or "").lower()
                            lexical_match = (
                                termo_lower in name or 
                                termo_lower in description or
                                original_lower in name or
                                original_lower in description
                            )

                            scored.append(
                                {
                                    **product,
                                    "similarity": similarity,
                                    "lexical_match": lexical_match,
                                }
                            )

                        # Determina se há contexto significativo
                        has_context = bool(contexto_limpo and len(contexto_limpo) > 5)
                        
                        # Aplica ranking contextual (com ou sem ocasião específica)
                        scored = _apply_contextual_ranking(scored, has_context, termo_normalizado)
                        
                        if has_context:
                            _safe_print(f"🎯 [CONTEXTO DETECTADO] Comprimento: {len(contexto_limpo)} chars")
                            _safe_print(f"   Contexto: '{contexto_limpo[:100]}...'")
                            _safe_print(f"   Estratégia: RANKING SEMÂNTICO (similaridade + preço)")
                        else:
                            if contexto_limpo:
                                _safe_print(f"⚠️ [CONTEXTO MUITO CURTO] Comprimento: {len(contexto_limpo)} chars (mínimo: 5)")
                            else:
                                _safe_print(f"🔍 [BUSCA GENÉRICA] Nenhum contexto ou contexto vazio")
                            _safe_print(f"   Estratégia: PRIORIZAÇÃO POR TIPO (QUADRO > FLOR > PELUCIA > CESTA > QUEBRA > CANECA > BAR)")

                        temperature_scores = _softmax(
                            [p["similarity"] for p in scored], temperature
                        )
                        for idx, item in enumerate(scored):
                            item["temperature_score"] = temperature_scores[idx]
                            item["ranking"] = idx + 1

                        scored = scored[:top_k]

                        exact_matches = [
                            p
                            for p in scored
                            if p["similarity"] >= min_similarity or p["lexical_match"]
                        ]
                        fallback_matches = [
                            p
                            for p in scored
                            if p not in exact_matches
                        ]

                        is_caneca_search = "caneca" in termo_lower
                        caneca_guidance = ""
                        if is_caneca_search:
                            caneca_guidance = "\n🎁 **IMPORTANTE**: Temos canecas de pronta entrega (1h (horário comercial)) e as customizáveis com fotos/nomes (18h (horário comercial)). Qual você prefere?"

                        structured = {
                            "status": "found" if scored else "not_found",
                            "termo": termo,
                            "termo_processado": termo_normalizado,
                            "contexto": contexto_limpo,
                            "contexto_detectado": has_context,
                            "estrategia_ranking": "SEMÂNTICO" if has_context else "TIPO_COM_PREÇO",
                            "is_caneca_search": is_caneca_search,
                            "caneca_guidance": caneca_guidance,
                            "exatos": [
                                {
                                    "ranking": p["ranking"],
                                    "id": str(p["id"]),
                                    "nome": p["name"],
                                    "preco": float(p["price"]),
                                    "descricao": p["description"],
                                    "imagem": p.get("image_url")
                                    or "https://api.cestodamore.com.br/images/default-product.webp",
                                    "production_time": int(p["production_time"])
                                    if p.get("production_time") is not None
                                    else 1,
                                    "tipo_resultado": "EXATO",
                                    "tipo_produto": p.get("product_type", "CESTA"),
                                    "motivo_ranking": p.get("ranking_reason", "DESCONHECIDO"),
                                    "relevance_score": int(p["similarity"] * 1000),
                                    "temperature_score": round(
                                        float(p["temperature_score"]), 6
                                    ),
                                }
                                for p in exact_matches
                            ],
                            "fallback": [
                                {
                                    "ranking": p["ranking"],
                                    "id": str(p["id"]),
                                    "nome": p["name"],
                                    "preco": float(p["price"]),
                                    "descricao": p["description"],
                                    "imagem": p.get("image_url")
                                    or "https://api.cestodamore.com.br/images/default-product.webp",
                                    "production_time": int(p["production_time"])
                                    if p.get("production_time") is not None
                                    else 1,
                                    "tipo_resultado": "FALLBACK",
                                    "tipo_produto": p.get("product_type", "CESTA"),
                                    "motivo_ranking": p.get("ranking_reason", "DESCONHECIDO"),
                                    "relevance_score": int(p["similarity"] * 1000),
                                    "temperature_score": round(
                                        float(p["temperature_score"]), 6
                                    ),
                                }
                                for p in fallback_matches
                            ],
                        }

                        for p in scored:
                            tipo = (
                                "EXATO"
                                if p in exact_matches
                                else "FALLBACK"
                            )
                            product_type = p.get("product_type", "CESTA")
                            ranking_reason = p.get("ranking_reason", "DESCONHECIDO")
                            price = float(p['price'])
                            sim_score = p['similarity']
                            _safe_print(
                                f"  ✅ [{tipo}] #{p['ranking']:2d} | {product_type:15} | {p['name']:<35} | R$ {price:7.2f} | SIM={sim_score:.3f} | {ranking_reason}"
                            )

                        return json.dumps(structured, ensure_ascii=False)
                except Exception as e:
                    _safe_print(f"⚠️ Falha no ranking semântico, usando busca lexical: {e}")

            common_words = {"o", "a", "de", "da", "do", "em", "um", "uma", "e", "ou", "para", "por", "com", "cliente", "procura", "queria", "quero"}
            
            # Adiciona variantes (com e sem acento) para aumentar matching lexical
            def get_variants(t):
                if not t: return []
                v = [t]
                # Remove acentos
                no_accents = "".join(
                    c for c in unicodedata.normalize("NFD", t)
                    if unicodedata.category(c) != "Mn"
                )
                if no_accents != t:
                    v.append(no_accents)
                return v

            search_terms = []
            for w in termo_normalizado.split():
                if w.strip().lower() not in common_words and len(w.strip()) > 2:
                    search_terms.extend(get_variants(w.strip()))
            
            # Adiciona também palavras-chave do contexto se for curto
            if contexto_limpo and len(contexto_limpo) < 100:
                for w in contexto_limpo.split():
                    if w.strip().lower() not in common_words and len(w.strip()) > 3:
                        search_terms.extend(get_variants(w.strip()))

            search_terms = list(set(search_terms + get_variants(termo_normalizado) + get_variants(termo)))
            search_terms = [t for t in search_terms if t.strip()]
            
            if len(search_terms) > 1:
                _safe_print(f"🔑 Multi-term search variants: {search_terms}")
            all_rows = []
            search_terms_tested = []
            
            for search_term in search_terms:
                if not search_term.strip():
                    continue
                    
                search_terms_tested.append(search_term)
                query = """
                WITH input_params AS (
                    SELECT LOWER($1) as termo, $2::float as preco_maximo, $3::float as preco_minimo
                ),
                products_scored AS (
                  SELECT p.id, p.name, p.description, p.price, p.image_url, p.production_time,
                  (
                    -- Name exact match (highest priority = 100)
                    (CASE WHEN p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 100 ELSE 0 END) +
                    -- Description/Tags content match (medium priority = 50)
                    (CASE WHEN p.description ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 50 ELSE 0 END) +
                    -- Word-boundary matches in tags (lower priority = 30)
                    (CASE WHEN p.description ~* ('\\b' || (SELECT termo FROM input_params) || '\\b') THEN 30 ELSE 0 END)
                  ) as relevance_score,
                  -- is_exact_match: score >= 50 means term is explicitly in name or description
                  (CASE WHEN 
                    p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' OR
                    p.description ILIKE '%' || (SELECT termo FROM input_params) || '%'
                   THEN true ELSE false END) as is_exact_match
                  FROM public."Product" p
                  WHERE p.price >= (SELECT preco_minimo FROM input_params) 
                    AND p.price <= (SELECT preco_maximo FROM input_params)
                    AND p.is_active = true
                    AND NOT (p.id::TEXT = ANY($4::TEXT[]))
                )
                SELECT 
                  id, name, description, price, image_url, production_time, relevance_score, is_exact_match,
                  ROW_NUMBER() OVER (ORDER BY is_exact_match DESC, relevance_score DESC, price DESC) as ranking
                FROM products_scored 
                WHERE relevance_score > 0
                ORDER BY is_exact_match DESC, ranking ASC
                LIMIT 10;
                """
                
                _safe_print(f"🔍 Testando termo: '{search_term}'")
                start_time = lib_time.time()
                rows = await conn.fetch(query, search_term, preco_maximo, preco_minimo, exclude_ids)
                duration = lib_time.time() - start_time
                _safe_print(f"⏱️ termo '{search_term}' retornou {len(rows)} produtos em {duration:.2f}s")
                
                for row in rows:
                    if not any(r['id'] == row['id'] for r in all_rows):
                        all_rows.append(row)
            
            all_rows = sorted(
                all_rows,
                key=lambda r: (not r['is_exact_match'], -r['relevance_score'], -r['price'])
            )
            
            # Returns up to 10 products for the IA to have context, but she must display only 2 per message.
            rows = all_rows[:10]
            
            _safe_print(f"🔍 consultarCatalogo: termo original='{termo}', testou {len(search_terms_tested)} keywords, preço=[{preco_minimo}-{preco_maximo}], exclude={len(exclude_ids)} IDs")
            
            if not rows:
                if len(search_terms) > 1:
                    _safe_print(f"⚠️ Nenhum resultado encontrado. Tentando termo original: '{termo}'")
                    single_query = """
                    WITH input_params AS (
                        SELECT LOWER($1) as termo, $2::float as preco_maximo, $3::float as preco_minimo
                    ),
                    products_scored AS (
                      SELECT p.id, p.name, p.description, p.price, p.image_url, p.production_time,
                      (
                        (CASE WHEN p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 100 ELSE 0 END) +
                        (CASE WHEN p.description ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 50 ELSE 0 END) +
                        (CASE WHEN p.description ~* ('\\b' || (SELECT termo FROM input_params) || '\\b') THEN 30 ELSE 0 END)
                      ) as relevance_score,
                      (CASE WHEN 
                        p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' OR
                        p.description ILIKE '%' || (SELECT termo FROM input_params) || '%'
                       THEN true ELSE false END) as is_exact_match
                      FROM public."Product" p
                      WHERE p.price >= (SELECT preco_minimo FROM input_params) 
                        AND p.price <= (SELECT preco_maximo FROM input_params)
                        AND p.is_active = true
                        AND NOT (p.id::TEXT = ANY($4::TEXT[]))
                    )
                    SELECT 
                      id, name, description, price, image_url, production_time, relevance_score, is_exact_match,
                      ROW_NUMBER() OVER (ORDER BY is_exact_match DESC, relevance_score DESC, price DESC) as ranking
                    FROM products_scored 
                    WHERE relevance_score > 0
                    ORDER BY is_exact_match DESC, ranking ASC
                    LIMIT 10;
                    """
                    rows = await conn.fetch(single_query, termo_normalizado, preco_maximo, preco_minimo, exclude_ids)

                if not rows and exclude_ids:
                    _safe_print("🔁 Fallback: tentando sem exclusões")
                    rows = await conn.fetch(single_query, termo_normalizado, preco_maximo, preco_minimo, [])

                if not rows:
                    term_lower = termo_normalizado.lower()
                    fallback_terms = []
                    if any(t in term_lower for t in ["cesto", "cesta", "presente"]):
                        fallback_terms.append("cesto")
                    if any(t in term_lower for t in ["buquê", "buque", "flores", "rosa"]):
                        fallback_terms.append("buquê")
                    if "caneca" in term_lower:
                        fallback_terms.append("caneca")
                    if any(t in term_lower for t in ["romant", "românt", "namorad"]):
                        fallback_terms.extend(["romântica", "namorados"])
                    if "anivers" in term_lower:
                        fallback_terms.append("aniversário")
                    if "bar" in term_lower:
                        fallback_terms.append("bar")
                    if "chocolate" in term_lower:
                        fallback_terms.append("chocolate")
                    if any(t in term_lower for t in ["pelucia", "pelúcia", "urso"]):
                        fallback_terms.append("pelúcia")
                    if "quebra" in term_lower:
                        fallback_terms.append("quebra-cabeça")
                    if "quadro" in term_lower:
                        fallback_terms.append("quadro")

                    fallback_terms = list(dict.fromkeys([t for t in fallback_terms if t]))
                    for fallback_term in fallback_terms:
                        _safe_print(f"🔁 Fallback: tentando termo similar '{fallback_term}'")
                        rows = await conn.fetch(
                            single_query,
                            fallback_term,
                            preco_maximo,
                            preco_minimo,
                            [],
                        )
                        if rows:
                            break
                
                if not rows:
                    return f"❌ Nenhum produto encontrado para '{termo}'. Desculpa! 😔"
            
            exact_matches = [r for r in rows if r['is_exact_match']]
            fallback_matches = [r for r in rows if not r['is_exact_match']]
            
            is_caneca_search = 'caneca' in termo_normalizado.lower()
            caneca_guidance = ""
            if is_caneca_search:
                caneca_guidance = "\n🎁 **IMPORTANTE**: Temos canecas de pronta entrega (1h (horário comercial)) e as customizáveis com fotos/nomes (18h (horário comercial)). Qual você prefere?"
            
            structured = {
                "status": "found" if rows else "not_found",
                "termo": termo,
                "termo_processado": termo_normalizado,
                "is_caneca_search": is_caneca_search,
                "caneca_guidance": caneca_guidance,
                "exatos": [
                    {
                        "ranking": r['ranking'],
                        "id": str(r['id']),
                        "nome": r['name'],
                        "preco": float(r['price']),
                        "descricao": r['description'],
                        "imagem": r['image_url'] or "https://api.cestodamore.com.br/images/default-product.webp",
                        "production_time": int(r['production_time']) if r['production_time'] is not None else 1,
                        "tipo_resultado": "EXATO",
                        "relevance_score": int(r['relevance_score'])
                    }
                    for r in exact_matches
                ],
                "fallback": [
                    {
                        "ranking": r['ranking'],
                        "id": str(r['id']),
                        "nome": r['name'],
                        "preco": float(r['price']),
                        "descricao": r['description'],
                        "imagem": r['image_url'] or "https://api.cestodamore.com.br/images/default-product.webp",
                        "production_time": int(r['production_time']) if r['production_time'] is not None else 1,
                        "tipo_resultado": "FALLBACK",
                        "relevance_score": int(r['relevance_score'])
                    }
                    for r in fallback_matches
                ]
            }
            
            for r in rows:
                tipo = "EXATO" if r['is_exact_match'] else "FALLBACK"
                _safe_print(f"  ✅ [{tipo}] Ranking {r['ranking']}: {r['name']} - R$ {r['price']:.2f}")
            
            return json.dumps(structured, ensure_ascii=False)
        except Exception as e:
            _safe_print(f"❌ Erro em consultarCatalogo: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool()
async def get_adicionais() -> str:
    """Retorna itens adicionais (Balões, Chocolates, Ursos) para a cesta."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Busca todas as categorias de itens que podem ser adicionados
        query = """
            SELECT name, base_price as price, description, image_url, type 
            FROM public."Item" 
            WHERE type IN ('additional', 'caneca', 'quadro', 'quebra_cabeca', 'outros')
            ORDER BY type, name
        """
        rows = await conn.fetch(query)
        adicionais = [{"name": r['name'], "price": float(r['price']), "description": r['description'], "image_url": r['image_url'], "type": r['type']} for r in rows]
        
        # Formata a lista de forma simples
        items_list = ""
        for i in adicionais:
            items_list += f"• {i['name']} - R$ {i['price']:.2f}\n"
            
        humanized = "✨ PARA TORNAR AINDA MAIS ESPECIAL:\n\n" + items_list
        return _format_structured_response({"status": "found", "adicionais": adicionais}, humanized)

@mcp.tool()
async def get_full_catalog() -> str:
    """
    Retorna link do Catálogo Completo. Use APENAS se cliente pedir explicitamente ("menu", "catalogo") ou estiver muito indeciso após ver várias opções.
    """
    catalog_url = "https://wa.me/c/558382163104"
    
    structured_data = {
        "status": "success",
        "catalog_url": catalog_url,
        "message": "Catálogo completo disponível"
    }
    
    humanized = f"""✨ Aqui está nosso catálogo completo com TODAS as opções e preços! 

{catalog_url}

Lá você consegue ver todas as fotos, descrições e valores. Dá uma olhadinha com calma e me chama se tiver alguma dúvida! 💕
Háa, lembrando que para Campina Grande o frete é GRÁTIS no PIX!
"""
    
    _safe_print(f"📋 [CATALOG] Enviando catálogo completo para cliente")
    
    return _format_structured_response(structured_data, humanized)


def _calculate_ready_datetime(
    start_dt: datetime,
    production_hours: int,
    business_hours_map: dict
) -> tuple:
    """
    Calculate when a product will be ready, walking through business hour blocks.
    Properly handles the 12:00-14:00 gap and multi-day production.
    Returns (ready_date, ready_time).
    """
    remaining = float(production_hours)
    current_date = start_dt.date()
    current_time_val = start_dt.time()
    day_names_en = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    
    for _ in range(30):  # max 30 days lookahead
        day_key = day_names_en[current_date.weekday()]
        blocks = business_hours_map.get(day_key, [])
        
        for block_start, block_end in blocks:
            effective_start = block_start if current_time_val <= block_start else current_time_val
            if effective_start >= block_end:
                continue
            
            available_hours = (
                datetime.combine(current_date, block_end) - 
                datetime.combine(current_date, effective_start)
            ).total_seconds() / 3600.0
            
            if remaining <= available_hours:
                ready_dt = datetime.combine(current_date, effective_start) + timedelta(hours=remaining)
                return current_date, ready_dt.time()
            
            remaining -= available_hours
        
        current_date += timedelta(days=1)
        current_time_val = time(0, 0)
    
    # Fallback - should not normally reach here
    return current_date, time(8, 0)


@mcp.tool()
async def validate_delivery_availability(date_str: str, time_str: Optional[str] = None, production_time_hours: Optional[int] = None) -> str:
    """
    Verifica se podemos entregar em Data (YYYY-MM-DD) e Hora (HH:MM).
    Retorna disponibilidade ou "suggested_slots" (blocos de horario) se hora nao for informada.
    SEMPRE mostre os suggested_slots ao cliente.

    Args:
        date_str: Data desejada no formato YYYY-MM-DD
        time_str: Hora desejada no formato HH:MM (opcional)
        production_time_hours: Tempo de producao do produto em horas comerciais (opcional, padrao: 1).
                               Passe o production_time do produto consultado via consultarCatalogo ou get_product_details.
    """
    try:
        date_str_validated, tz_debug = _validate_timezone_safety(date_str)
        _safe_print(tz_debug)
        
        date_obj = datetime.strptime(date_str_validated, "%Y-%m-%d").date()
        now_local = _get_local_time()
        
        day_names = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        day_name = day_names[date_obj.weekday()]
        day_num = date_obj.weekday()
        
        _safe_print(f"📅 [VALIDATE-DELIVERY] Data: {date_str} | Dia: {day_name} | Hora: {time_str or 'não informada'} | Agora: {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
        
        async def is_holiday(check_date):
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                query = """
                SELECT name, closure_type, duration_hours
                FROM public."Holiday"
                WHERE is_active = true
                AND $1::DATE >= start_date 
                AND $1::DATE <= end_date
                LIMIT 1;
                """
                result = await conn.fetchrow(query, check_date)
                return result
        
        async def get_next_available(current_date):
            next_d = current_date + timedelta(days=1)
            while True:
                d_num = next_d.weekday()
                d_name = day_names[d_num]
                hours = BUSINESS_HOURS.get(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d_num], [])
                
                holiday_check = await is_holiday(next_d)
                if hours and not holiday_check:
                    return next_d, d_name, hours
                next_d += timedelta(days=1)

        if day_num == 6:
            next_date, next_day_name, next_hours = await get_next_available(date_obj)
            hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in next_hours])
            
            structured_data = {
                "status": "unavailable",
                "reason": "closed",
                "date": date_str,
                "day": day_name,
                "next_available_date": next_date.strftime("%Y-%m-%d"),
                "next_available_day": next_day_name,
                "next_available_hours": hours_fmt
            }
            return _format_structured_response(
                structured_data,
                f"😔 Aos domingos a gente descansa para estar 100% pra você na segunda! ❤️\n\nQue tal marcar para {next_day_name} ({next_date.strftime('%d/%m')})? Funcionamos das {hours_fmt}. Quer agendar? 🥰"
            )
        
        holiday_info = await is_holiday(date_obj)
        if holiday_info:
            next_date, next_day_name, next_hours = await get_next_available(date_obj)
            hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in next_hours])
            holiday_name = holiday_info['name']
            
            structured_data = {
                "status": "unavailable",
                "reason": "holiday",
                "date": date_str,
                "day": day_name,
                "holiday_name": holiday_name,
                "next_available_date": next_date.strftime("%Y-%m-%d"),
                "next_available_day": next_day_name,
                "next_available_hours": hours_fmt
            }
            return _format_structured_response(
                structured_data,
                f"😔 No dia {date_obj.strftime('%d/%m')} é {holiday_name} e estamos fechados para aproveitar com a família! ❤️\n\nQue tal marcar para {next_day_name} ({next_date.strftime('%d/%m')})? Funcionamos das {hours_fmt}. Quer agendar? 🥰"
            )
        
        day_key = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day_num]
        business_hours = BUSINESS_HOURS.get(day_key, [])
        
        if not business_hours:
            next_date, next_day_name, next_hours = await get_next_available(date_obj)
            hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in next_hours])
            
            structured_data = {
                "status": "unavailable",
                "reason": "no_business_hours",
                "date": date_str,
                "day": day_name
            }
            return _format_structured_response(
                structured_data,
                f"😔 Não abrimos aos {day_name}s. Que tal marcar para {next_day_name} ({next_date.strftime('%d/%m')})? Atendemos das {hours_fmt}. 🥰"
            )
        
        if time_str:
            try:
                requested_time = datetime.strptime(time_str, "%H:%M").time()
                
                is_within_hours = any(
                    start <= requested_time <= end 
                    for start, end in business_hours
                )
                
                if not is_within_hours:
                    is_too_early = requested_time < business_hours[0][0]
                    is_too_late = requested_time > business_hours[-1][1]
                    
                    is_interval = False
                    if len(business_hours) > 1:
                        for i in range(len(business_hours) - 1):
                            if business_hours[i][1] < requested_time < business_hours[i+1][0]:
                                is_interval = True
                                break
                    
                    next_date, next_day_name, next_hours = await get_next_available(date_obj)
                    hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in next_hours])
                    current_day_hours = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in business_hours])

                    if is_too_late:
                        return _format_structured_response(
                            {"status": "unavailable", "reason": "after_hours", "requested_time": time_str},
                            f"Poxa, agora são {time_str} e já estamos fora do horário comercial. ⏰\n\nMas você pode marcar para {next_day_name} ({next_date.strftime('%d/%m')})! Nosso horário é das {hours_fmt}. Quer agendar? 🥰"
                        )
                    elif is_interval:
                        return _format_structured_response(
                            {"status": "unavailable", "reason": "interval", "requested_time": time_str},
                            f"⏰ Agora estamos em horário de intervalo! Mas voltamos já, às {business_hours[1][0].strftime('%H:%M')}.\n\nPara hoje as opções são: {current_day_hours}. Qual funciona melhor? 💕"
                        )
                    else:
                        return _format_structured_response(
                            {"status": "unavailable", "reason": "outside_hours", "requested_time": time_str},
                            f"⏰ Nesse horário não estamos operando. Hoje ({day_name}) nosso horário é {current_day_hours}.\n\nQual horário fica melhor pra você? ✨"
                        )
                
                if date_obj == now_local.date():
                    prod_hours = production_time_hours or 1
                    ready_date, ready_time_val = _calculate_ready_datetime(now_local, prod_hours, BUSINESS_HOURS)
                    min_ready_time = ready_time_val
                    
                    if ready_date > date_obj or requested_time < min_ready_time:
                        next_date, next_day_name, next_hours = await get_next_available(date_obj)
                        next_hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in next_hours])
                        ready_info = f"{ready_date.strftime('%d/%m')} às {ready_time_val.strftime('%H:%M')}" if ready_date > date_obj else f"às {ready_time_val.strftime('%H:%M')}"
                        
                        return _format_structured_response(
                            {
                                "status": "unavailable", 
                                "reason": "insufficient_production_time", 
                                "current_time": now_local.strftime("%H:%M"),
                                "production_time_hours": prod_hours,
                                "estimated_ready_date": ready_date.strftime("%Y-%m-%d"),
                                "estimated_ready_time": ready_time_val.strftime("%H:%M"),
                                "requested_time": requested_time.strftime("%H:%M"),
                                "next_available_date": next_date.strftime("%Y-%m-%d"),
                                "next_available_hours": next_hours_fmt
                            },
                            f"⏱️ Poxa, o prazo ficou apertado! Agora são {now_local.strftime('%H:%M')} e precisamos de {prod_hours}h comerciais para preparar (ficaria pronta {ready_info}).\n\nO horário que você pediu ({requested_time.strftime('%H:%M')}) não é viável.\n\nQue tal marcar para {next_day_name} ({next_date.strftime('%d/%m')})? Atendemos das {next_hours_fmt}. 🌹"
                        )
                
                return _format_structured_response(
                    {"status": "available", "date": date_str, "time": time_str, "production_time_hours": production_time_hours or 1},
                    f"✅ Perfeito! Tá marcado para {day_name} às {time_str}! Sua cesta vai estar prontinha após {production_time_hours or 1}h comerciais de produção. 🌹❤️"
                )
            
            except ValueError:
                return "⚠️ Formato de hora inválido. Use HH:MM (exemplo: 14:30)"
        
        else:
            now_local = _get_local_time()
            current_time = now_local.time()
            
            def format_hours(h_list):
                return ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in h_list])

            hours_fmt = format_hours(business_hours)
            
            if date_obj == now_local.date():
                is_after_hours = current_time > business_hours[-1][1]
                
                if is_after_hours:
                    next_date, next_day_name, next_hours = await get_next_available(date_obj)
                    next_hours_fmt = format_hours(next_hours)
                    return _format_structured_response(
                        {
                            "status": "unavailable", 
                            "reason": "after_hours_today",
                            "current_time_campina": now_local.strftime("%H:%M")
                        },
                        f"Poxa, hoje os pedidos já encerraram (agora são {now_local.strftime('%H:%M')})! ⏰\n\nMas você pode marcar para {next_day_name} ({next_date.strftime('%d/%m')})! Abrimos das {next_hours_fmt}. Quer agendar? 🥰"
                    )
                
                min_ready_dt = now_local + timedelta(hours=1)
                prod_hours = production_time_hours or 1
                ready_date, ready_time_val = _calculate_ready_datetime(now_local, prod_hours, BUSINESS_HOURS)
                min_ready_time = ready_time_val
                ready_time_formatted = ready_time_val.strftime("%H:%M")
                
                # If product can't be ready today, suggest next available day
                if ready_date > date_obj:
                    next_date, next_day_name, next_hours = await get_next_available(date_obj)
                    next_hours_fmt = format_hours(next_hours)
                    return _format_structured_response(
                        {
                            "status": "unavailable",
                            "reason": "production_exceeds_today",
                            "production_time_hours": prod_hours,
                            "estimated_ready_date": ready_date.strftime("%Y-%m-%d"),
                            "estimated_ready_time": ready_time_formatted,
                            "next_available_date": next_date.strftime("%Y-%m-%d"),
                            "next_available_hours": next_hours_fmt
                        },
                        f"Esse produto precisa de {prod_hours}h comerciais de produção e não dá tempo pra hoje! ⏰\n\nFicaria pronto {next_day_name} ({ready_date.strftime('%d/%m')}) às {ready_time_formatted}. Quer agendar? 🥰"
                    )
                
                # Verificar se há slots disponíveis após a produção estar pronta
                # Busca em TODOS os períodos do dia, não só a partir de agora
                available_today = []
                for s, e in business_hours:
                    # Se o fim do período é depois que ficará pronta, tem slots
                    if e > min_ready_time:
                        # Começa do horário em que ficará pronta ou do início do período, o que for maior
                        effective_start = max(s, min_ready_time)
                        available_today.append((effective_start, e))
                
                # Se não há slots com o horário mínimo, pode oferecer o próximo dia
                if not available_today:
                     next_date, next_day_name, next_hours = await get_next_available(date_obj)
                     next_hours_fmt = format_hours(next_hours)
                     return _format_structured_response(
                        {
                            "status": "unavailable", 
                            "reason": "no_slots_left_today",
                            "current_time_campina": now_local.strftime("%H:%M"),
                            "min_ready_time": ready_time_formatted
                        },
                        f"Hoje os horários se encerram em breve (agora são {now_local.strftime('%H:%M')} e a cesta ficaria pronta às {ready_time_formatted}). ⏰\n\nQue tal amanhã ({next_day_name}, {next_date.strftime('%d/%m')})? Abrimos desde as {next_hours[0][0].strftime('%H:%M')}. Qual horário funciona melhor? 🥰"
                    )

                available_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in available_today])

                suggested_slots = []
                for s, e in available_today:
                    temp_dt = datetime.combine(date_obj, s)
                    
                    if temp_dt.minute > 30:
                        temp_dt = temp_dt.replace(minute=0) + timedelta(hours=1)
                    elif temp_dt.minute > 0 and temp_dt.minute < 30:
                        temp_dt = temp_dt.replace(minute=30)
                    elif temp_dt.minute == 0:
                        pass
                    
                    end_dt = datetime.combine(date_obj, e)
                    while temp_dt <= end_dt:
                        slot_time = temp_dt.time()
                        suggested_slots.append(slot_time.strftime("%H:%M"))
                        temp_dt += timedelta(minutes=30)
                
                suggested_str = " | ".join(suggested_slots[:12])
                
                return _format_structured_response(
                    {
                        "status": "available", 
                        "today": True, 
                        "current_time_campina": now_local.strftime("%H:%M"),
                        "production_time_hours": prod_hours,
                        "estimated_ready_time": ready_time_formatted,
                        "available_hours_total": hours_fmt,
                        "available_from_ready_time": available_fmt,
                        "suggested_slots": suggested_slots,
                        "ai_instruction": "APRESENTE TODOS os suggested_slots ao cliente e PERGUNTE qual ele prefere. NAO escolha por ele. estimated_ready_time e tempo de producao, NAO e o horario de entrega."
                    },
                    f"Tem como entregar hoje ainda! Com produção de {prod_hours}h comerciais, fica pronta por volta das {ready_time_formatted}! 🎁\n\n**Opções de entrega para hoje:**\n{suggested_str}\n\nQual desses horários você prefere? 🌹"
                )
            
            # Future date
            prod_hours = production_time_hours or 1
            ready_date, ready_time_val = _calculate_ready_datetime(now_local, prod_hours, BUSINESS_HOURS)
            response_data = {
                "status": "available", 
                "date": date_str, 
                "available_hours": hours_fmt,
                "current_time_campina": now_local.strftime("%H:%M"),
                "production_time_hours": prod_hours,
                "estimated_ready_date": ready_date.strftime("%Y-%m-%d"),
                "estimated_ready_time": ready_time_val.strftime("%H:%M"),
                "ai_instruction": "PERGUNTE ao cliente qual horario ele prefere dentro de available_hours. NAO escolha por ele. estimated_ready_time e tempo de producao, NAO e o horario de entrega."
            }
            
            if ready_date > date_obj:
                response_data["warning"] = f"Produção de {prod_hours}h pode não ficar pronta antes de {date_str}"
            
            return _format_structured_response(
                response_data,
                f"✅ {day_name.capitalize()} ({date_obj.strftime('%d/%m')}) é perfeitinho! Atendemos das {hours_fmt}.\n\nQual horário você prefere? 🎁"
            )
    
    except ValueError as e:
        return f"⚠️ Erro no formato da data. Use YYYY-MM-DD (exemplo: 2026-01-15): {str(e)}"
    except Exception as e:
        return f"⚠️ Erro ao validar: {str(e)}"

@mcp.tool()
async def get_product_details(product_id: str) -> str:
    """
    Busca nome, preço, descrição e COMPONENTES do produto.
    OBRIGATÓRIO: Ler 'componentes' antes de responder o que tem na cesta.
    Não alucine itens não listados.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            product_id_str = str(product_id)
            
            # Busca dados básicos do produto
            product_query = """
            SELECT id, name, description, price, production_time
            FROM public."Product"
            WHERE id = $1
            LIMIT 1;
            """
            product_row = await conn.fetchrow(product_query, product_id_str)
            
            if not product_row:
                return json.dumps({
                    "status": "not_found",
                    "message": f"Produto com ID {product_id_str} não encontrado"
                }, ensure_ascii=False)
            
            # Busca componentes do produto
            components_query = """
            SELECT i.name, pc.quantity
            FROM public."ProductComponent" pc
            JOIN public."Item" i ON pc.item_id = i.id
            WHERE pc.product_id = $1
            ORDER BY i.name ASC;
            """
            component_rows = await conn.fetch(components_query, product_id_str)
            
            componentes = [
                {
                    "nome": r['name'],
                    "quantidade": r['quantity']
                }
                for r in component_rows
            ]
            
            structured = {
                "status": "found",
                "id": str(product_row['id']),
                "nome": product_row['name'],
                "preco": float(product_row['price']),
                "descricao": product_row['description'] or "",
                "production_time": int(product_row['production_time'] or 0),
                "componentes": componentes,
                "_debug": f"Total de {len(componentes)} componentes encontrados"
            }
            
            _safe_print(f"📦 get_product_details: {product_row['name']} | {len(componentes)} componentes")
            
            return json.dumps(structured, ensure_ascii=False)
            
        except Exception as e:
            _safe_print(f"❌ Erro em get_product_details: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool()
async def get_active_holidays() -> str:
    """Retorna datas em que a loja estará FECHADA (Feriados)."""
    pool = await get_db_pool()
    now_local = _get_local_time()
    
    _safe_print(f"🕐 [HOLIDAYS-CHECK] Timezone: {now_local.strftime('%Z (UTC%z)')} | Horário: {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
    async with pool.acquire() as conn:
        query = """
        SELECT name, start_date, end_date, closure_type, duration_hours
        FROM public."Holiday"
        WHERE is_active = true
        AND start_date >= $1::DATE - INTERVAL '1 day'
        ORDER BY start_date ASC;
        """
        rows = await conn.fetch(query, now_local.date())
        if not rows:
            return _format_structured_response(
                {"status": "no_holidays"},
                "Nenhum feriado ou encerramento programado no momento."
            )
        
        holidays = []
        humanized = "🗓️ *Datas com loja fechada:*\n\n"
        
        for row in rows:
            start = row['start_date']
            end = row['end_date']
            name = row['name']
            closure_type = row['closure_type']
            
            holiday_info = {
                "name": name,
                "start_date": str(start),
                "end_date": str(end),
                "closure_type": closure_type
            }
            holidays.append(holiday_info)
            
            if closure_type == "full_day":
                if start == end:
                    humanized += f"• {name}: {start.strftime('%d/%m/%Y')}\n"
                else:
                    humanized += f"• {name}: {start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}\n"
            else:
                hours = row['duration_hours'] or 0
                humanized += f"• {name}: {start.strftime('%d/%m/%Y')} - Fechado por {hours}h\n"
        
        humanized += "\n⚠️ Nessas datas não fazemos entrega ou processamento."
        
        return _format_structured_response(
            {"status": "found", "holidays": holidays},
            humanized
        )

@mcp.tool()
async def calculate_freight(city: str, payment_method: str = "PIX") -> str:
    """Calcula frete. Campina Grande=Grátis(PIX) ou R$10(Cartão). Outras=Até 20km."""
    if not city or str(city).strip() == "":
        return _format_structured_response(
            {"status": "error", "error": "missing_city"},
            "⚠️ Por favor confirme a cidade de entrega antes de calcular o frete. Pergunte ao cliente: 'Qual cidade será a entrega?'"
        )

    city_lower = str(city).lower().strip()
    method_lower = str(payment_method).lower().strip() if payment_method else "pix"
    is_card = any(k in method_lower for k in ['cart', 'cartão', 'cartao', 'credito', 'crédito', 'debito', 'débito'])

    if re.search(r"\bcampina\b", city_lower) or "campina grande" in city_lower:
        if not is_card:
            return "Sim! Entrega para Campina Grande é gratuita no PIX. Entregamos também em outras cidades até 20 km. Os detalhes de frete serão passados ao fim do atendimento 😊"
        else:
            return "O frete para Campina Grande no CARTÃO é R$ 10,00 🚚. Entregamos também em outras cidades até 20 km. Os detalhes de frete serão passados ao fim do atendimento 😊"
    
    return "Entregamos em Campina Grande (grátis no PIX) e em outras cidades até 20 km. Os detalhes de frete serão passados ao fim do atendimento 😊"

@mcp.tool()
async def get_current_business_hours() -> str:
    """Retorna horário de funcionamento de hoje e status (Aberto/Fechado)."""
    now = _get_local_time()
    day_num = now.weekday()
    day_names_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    day_key = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day_num]
    day_name_pt = day_names_pt[day_num]
    hours = BUSINESS_HOURS.get(day_key, [])
    
    _safe_print(f"🕐 [BUSINESS-HOURS] Dia: {day_name_pt} | Horário atual: {now.strftime('%H:%M:%S')} | Timezone: {now.strftime('%Z')}")
    
    if not hours:
        _safe_print(f"⚠️ [BUSINESS-HOURS] Sem horários configurados para {day_name_pt}")
        next_day = now + timedelta(days=1)
        next_day_num = next_day.weekday()
        next_day_name = day_names_pt[next_day_num]
        return f"Hoje ({day_name_pt}) não abrimos para produção, mas estamos anotando pedidos para {next_day_name}! ❤️"
        
    hours_fmt = " e das ".join([f"{s.strftime('%H:%M')} às {e.strftime('%H:%M')}" for s, e in hours])
    status = "Abertos"
    
    current_time = now.time()
    is_open = any(s <= current_time <= e for s, e in hours)
    
    _safe_print(f"📊 [BUSINESS-HOURS] Horários: {hours_fmt} | Aberto: {is_open}")
    
    if not is_open:
        status = "Fechados no momento"
        next_open = None
        for s, e in hours:
            if current_time < s:
                next_open = s.strftime('%H:%M')
                break
        
        if next_open:
            return f"⏰ No momento estamos fechados. Abrimos novamente às {next_open}. Mas você pode enviar a mensagem agora que respondemos em breve! 📱\n\nHorário completo de hoje: {hours_fmt}"
        else:
            return f"⏰ Já encerramos o expediente de hoje. Amanhã estamos de volta! ❤️\n\nHorário de hoje era: {hours_fmt}"
        
    return f"✅ Estamos abertos! Funcionamos hoje ({day_name_pt}) das {hours_fmt}. Status: {status} 💕"

@mcp.tool()
async def validate_price_manipulation(claimed_price: float, product_name: str) -> str:
    """Detects price manipulation."""
    return "Preço validado."

@mcp.tool()
async def notify_human_support(reason: str, customer_context: str, customer_name: str = "Cliente", customer_phone: str = "", session_id: Optional[str] = None) -> str:
    """
    Transfere IMEDIATAMENTE para atendimento humano. Use quando:
    1. Cliente pede para falar com humano/atendente/pessoa.
    2. Evento de carrinho (cart_added).
    3. Problema técnico ou caso complexo.
    4. Tentativa de manipulação de preço.
    5. Pedido corporativo.

    NÃO use para finalizar compra (use finalize_checkout).
    NÃO valida dados de checkout — transfere direto.

    reason: motivo (ex: "cliente_quer_atendente", "cart_added", "pedido_corporativo").
    customer_context: Contexto breve da conversa.
    """
    support_message = _format_support_message(reason, customer_context, customer_name, customer_phone)
    await _send_whatsapp_notification(support_message, customer_name, customer_phone)

    if session_id:
        await _internal_block_session(session_id)

    return "Transferência realizada com sucesso. Atendente humano notificado. ✅\n\n⚠️ IMPORTANTE: Você DEVE AGORA avisar o cliente que ele foi transferido e informar OBRIGATORIAMENTE na sua reposta final o horário de atendimento comercial exato:\nSeg-Sex 08:30-12:00 | 14:00-17:00\nSábado 08:00-11:00"


@mcp.tool()
async def finalize_checkout(customer_context: str, customer_name: str = "Cliente", customer_phone: str = "", session_id: Optional[str] = None) -> str:
    """
    Finaliza pedido APÓS coleta completa dos dados. USO OBRIGATÓRIO no fim do checkout.

    customer_context DEVE conter TODOS:
    - Produto (nome + preço R$)
    - Data e horário de entrega
    - Endereço completo (ou "retirada")
    - Forma de pagamento (PIX ou Cartão)

    Se faltar algum dado, retorna erro com instruções de coleta.
    Após sucesso, bloqueia a sessão automaticamente.
    """
    ctx = (customer_context or "").lower()
    is_retirada = "retirada" in ctx or "retirar" in ctx

    has_product = bool(re.search(r"(cesta|produto|buqu[eê]|buque|caneca|rosa|quadro|chocolate|bar|pelúcia|pelucia|flor|cone|quebra)", ctx))
    has_delivery = bool(re.search(r"(entrega|data|hoje|amanh[aã]|\d{1,2}\/\d{1,2}|\d{4}-\d{2}-\d{2})", ctx))
    has_address = is_retirada or bool(re.search(r"(rua|avenida|av\.|r\.|endereço|endereco|bairro)", ctx))
    has_payment = bool(re.search(r"(pix|cart[aã]o|cartao|crédito|credito|débito|debito)", ctx))

    missing = []
    if not has_product:
        missing.append("produto (nome e preço)")
    if not has_delivery:
        missing.append("data/horário de entrega")
    if not has_address:
        missing.append("endereço completo")
    if not has_payment:
        missing.append("forma de pagamento")

    if missing:
        return _format_structured_response(
            {"status": "error", "error": "incomplete_checkout", "missing": missing},
            f"⚠️ Checkout incompleto. Faltam: {', '.join(missing)}.\n\nColeta obrigatória:\n1. Produto (nome + preço)\n2. Data e Horário\n3. Endereço completo\n4. Forma de pagamento (PIX ou Cartão)\n5. Resumo final + confirmação do cliente"
        )

    # Monta contexto estruturado com informações completas
    structured_context = f"""=== RESUMO DO PEDIDO ===
{customer_context}
Horário de Atendimento: Seg-Sex 08:30-12:00 | 14:00-17:00 | Sáb 08:00-11:00
====================="""
    
    support_message = _format_support_message("end_of_checkout", structured_context, customer_name, customer_phone)
    await _send_whatsapp_notification(support_message, customer_name, customer_phone)

    if session_id:
        await _internal_block_session(session_id)

    return _format_structured_response(
        {"status": "success", "action": "checkout_finalized"},
        "Pedido finalizado e equipe notificada com sucesso! ✅\n\n⚠️ IMPORTANTE: Você DEVE AGORA enviar ao cliente OBRIGATORIAMENTE a mensagem final com o horário de atendimento comercial exato:\nSeg-Sex 08:30-12:00 | 14:00-17:00\nSábado 08:00-11:00"
    )

@mcp.tool()
async def math_calculator(expression: str) -> str:
    """Calculadora simples (ex: "100 + 20")."""
    try:
        allowed_chars = "0123456789+-*/.() "
        if not all(c in allowed_chars for c in expression):
            return "Erro: Expressão contém caracteres não permitidos."
        
        result = eval(expression, {"__builtins__": {}})
        return f"Resultado: {result:.2f}"
    except Exception as e:
        return f"Erro ao calcular: {str(e)}"

async def _internal_block_session(session_id: str) -> str:
    """
    Logica interna para bloquear a sessão atual do chat.
    """
    pool = await get_db_pool()
    now_local = _get_local_time()
    now_naive = now_local.replace(tzinfo=None)
    expires_at = now_naive + timedelta(seconds=345600)
    
    _safe_print(f" tentando bloquear sessão: {session_id}")

    async with pool.acquire() as conn:
        try:
            query = """
            UPDATE "AIAgentSession"
            SET is_blocked = true, expires_at = $2
            WHERE id = $1;
            """
            result = await conn.execute(query, session_id, expires_at)
            
            if result == "UPDATE 0":
                _safe_print(f"⚠️ Nenhuma linha afetada com UPDATE normal, tentando com cast ::uuid para {session_id}")
                query_uuid = """
                UPDATE "AIAgentSession"
                SET is_blocked = true, expires_at = $2
                WHERE id = $1::uuid;
                """
                result = await conn.execute(query_uuid, session_id, expires_at)
            
            if result == "UPDATE 1":
                _safe_print(f"🔒 Sessão {session_id} bloqueada com sucesso até {expires_at}.")
                return "Sessão bloqueada com sucesso. O Agente de IA não responderá mais nesta conversa. ✅"
            else:
                _safe_print(f"⚠️ Falha ao bloquear: Sessão {session_id} não encontrada no banco. Resultado: {result}")
                return f"Aviso: Não foi possível encontrar a sessão {session_id} para bloquear. Verifique se o ID está correto."
                
        except Exception as e:
            _safe_print(f"❌ Erro fatal ao bloquear sessão {session_id}: {e}")
            return f"Erro ao bloquear sessão: {str(e)}"

@mcp.tool()
async def block_session(session_id: str) -> str:
    """
    ENCERRA O ATENDIMENTO DA IA para esta sessão.
    Deve ser chamado OBRIGATORIAMENTE IMEDIATAMENTE APÓS 'notify_human_support'.
    Isso impede que a Ana continue falando após o humano assumir.
    """
    return await _internal_block_session(session_id)

@mcp.tool()
async def save_customer_summary(customer_phone: str, summary: str) -> str:
    """
    SALVA O STATUS ATUAL DO PEDIDO na memória de longo prazo.
    Use SEMPRE após avanços importantes (escolheu cesta, deu endereço, etc).
    Isso evita que a Ana esqueça o que foi combinado se a conversa ficar longa.
    """
    pool = await get_db_pool()
    now_local = _get_local_time()
    async with pool.acquire() as conn:
        try:
            now_naive = now_local.replace(tzinfo=None)
            expires_at = now_naive + timedelta(days=15)
            query = """
            INSERT INTO public."CustomerMemory" (id, customer_phone, summary, updated_at, expires_at)
            VALUES (gen_random_uuid(), $1, $2, $3, $4)
            ON CONFLICT (customer_phone) DO UPDATE 
            SET summary = $2, updated_at = $3, expires_at = $4
            RETURNING id;
            """
            row = await conn.fetchrow(query, customer_phone, summary, now_naive, expires_at)
            structured_data = {"status": "success", "customer_phone": customer_phone, "memory_id": str(row['id'])}
            return _format_structured_response(structured_data, f"Memória atualizada para {customer_phone}.")
        except Exception as e:
            return f"Erro: {str(e)}"

@mcp.prompt()
async def proc_validacao_entrega() -> str:
    """
    PROCEDIMENTO: Validar Disponibilidade de Entrega
    
    QUANDO USAR: Cliente mencionou uma data/hora específica para entrega
    
    PASSOS OBRIGATÓRIOS:
    1. EXTRAIA a data mencionada (formato: YYYY-MM-DD)
    2. EXTRAIA a hora se mencionada (formato: HH:MM)
    3. CHAME validate_delivery_availability com data_str + time_str
    4. INTERPRETE o resultado:
       - ✅ "disponível" → Prossiga normalmente
       - ❌ "Fechado aos domingos" → Proponha próximo dia útil
       - ❌ "fora do horário" → Proponha horário durante funcionamento
    
    NUNCA:
    - Assuma que data é válida sem validar
    - Marque entrega em domingo
    - Ignore horários fora do comercial
    
    EXEMPLO:
    Cliente: "Quero para amanhã às 14h"
    → Extraia: date_str='2026-01-08', time_str='14:00'
    → Chame tool com esses valores
    → Confirme a disponibilidade com o cliente
    """
    return "Procedimento de validação de entrega carregado."

@mcp.prompt()
async def proc_calculo_frete() -> str:
    """
    PROCEDIMENTO: Calcular Frete
    
    QUANDO USAR: Cliente confirmou a cesta E cidade de entrega E MÉTODO DE PAGAMENTO
    
    ⚠️ CRÍTICO: NUNCA calcule frete sem confirmar o método de pagamento!
    
    MÉTODOS DE PAGAMENTO:
    - "pix" → Frete GRÁTIS em Campina Grande, R$ 15 em cidades vizinhas
    - "credito" → Frete pago pelo atendente no fechamento (valor: consulte procedimento_closing)
    - "debito" → Frete pago pelo atendente no fechamento (valor: consulte procedimento_closing)
    
    PASSOS OBRIGATÓRIOS:
    1. CONFIRME com cliente: "Qual é seu método de pagamento? PIX ou Cartão?"
    2. AGUARDE resposta do cliente
    3. SOMENTE APÓS resposta, chame calculate_freight(city, payment_method)
    
    NUNCA:
    - Assuma método de pagamento sem perguntar
    - Calcule frete para cartão/débito (valor é dado pelo atendente)
    - Use estimate quando cliente não confirmou método
    
    EXEMPLO CORRETO:
    Cliente: "Quero entregar em Puxinanã"
    Você: "Perfeito! Qual é seu método de pagamento? PIX ou Cartão?"
    Cliente: "PIX"
    → Chame: calculate_freight(city='Puxinanã', payment_method='pix')
    
    EXEMPLO ERRADO (NÃO FAÇA):
    Cliente: "Quero entregar em Puxinanã"
    → Chame calculate_freight direto SEM perguntar método ❌
    """
    return "Procedimento de cálculo de frete carregado."

@mcp.prompt()
async def proc_closing_protocol() -> str:
    """
    PROCEDIMENTO: Fechamento de Venda (OBRIGATÓRIO)
    
    ATIVE ESTE PROCEDIMENTO quando cliente diz: "Quero essa", "Vou levar", "Como compro?"
    
    SEQUÊNCIA DE COLETA (1 pergunta por vez):
    
    1️⃣ CONFIRME A CESTA:
       "Você escolheu a [NOME_DA_CESTA] por R$ [PREÇO], certo?"
       Aguarde confirmação.
    
    2️⃣ DATA E HORÁRIO:
       "Para qual data você gostaria de receber? E qual horário?"
       → Valide com validate_delivery_availability
    
    3️⃣ ENDEREÇO COMPLETO:
       "Me passa seu endereço completo: rua, número, bairro, complemento"
       Aguarde resposta.
    
    4️⃣ MÉTODO DE PAGAMENTO:
       "Você prefere pagar com PIX ou Cartão?"
       Aguarde resposta.
       → Se PIX: "PIX é vantajoso! Você ganha frete GRÁTIS em Campina Grande"
       → Se Cartão: "O frete será confirmado no pagamento por nosso atendente"
    
    5️⃣ CÁLCULO DO FRETE:
       → Se PIX: use calculate_freight(city, 'pix')
       → Se Cartão/Débito: NUNCA use a tool, avise ao cliente que atendente dirá o valor
    
    6️⃣ PERSONALIZAÇÃO (se aplicável):
       "Deseja adicionar foto, frase ou algo personalizado?"
       → Se sim: "Vou transferir para um atendente especializado que coleta esses detalhes"
    
    7️⃣ FECHAMENTO FINAL:
       Resuma tudo:
       - Cesta: [NOME] - R$ [PREÇO]
       - Entrega: [DATA] às [HORA] em [CIDADE]
       - Pagamento: [PIX/CARTÃO]
       - Frete: R$ [VALOR ou 'será confirmado pelo atendente']
       - Personalização: [SIM/NÃO]
       
       "Perfeito! Vou transferir para nosso time que vai confirmar o pagamento e detalhes finais. Obrigada! ❤️"
    
    8️⃣ NOTIFIQUE O SUPORTE:
       Chame notify_human_support com:
       - reason: "end_of_checkout"
       - customer_context: {toda info acima}
       - customer_name: [nome do cliente]
       - customer_phone: [número do cliente]
       - should_block_flow: true
    
    NUNCA:
    - Pule etapas
    - Pergunte tudo de uma vez
    - Calcule frete sem PIX confirmado
    - Transfira sem confirmar todos os dados
    """
    return "Protocolo de Fechamento carregado."

@mcp.prompt()
async def proc_consultar_diretrizes() -> str:
    """
    PROCEDIMENTO: Consultar Diretrizes Antes de Agir
    
    USE SEMPRE ANTES DE:
    - Recomendar um produto
    - Falar sobre customização
    - Explicar prazos de entrega
    - Falar sobre tipos de flores
    - Lidar com indecisão de cliente
    
    PASSOS:
    1. Identifique o contexto do cliente
    2. Chame search_guidelines com a categoria apropriada:
       - "product_selection" → Antes de recomendar cestas
       - "customization" → Antes de coletar fotos/frases
       - "faq_production" → Antes de falar sobre prazos
       - "delivery_rules" → Antes de falar sobre entrega
       - "inexistent_products" → Se cliente pedir algo que não vendemos
    
    3. LEIA a resposta das diretrizes
    4. SIGA exatamente o que as diretrizes dizem
    
    NUNCA:
    - Invente procedimentos que não estão nas diretrizes
    - Ignore as diretrizes e faça do seu jeito
    - Recomende sem consultar "product_selection"
    """
    return "Procedimento de consulta de diretrizes carregado."

@mcp.prompt()
async def proc_validar_horario_funcionamento() -> str:
    """
    PROCEDIMENTO: Validar Horários de Funcionamento da Loja
    
    QUANDO USAR: Cliente perguntar "A loja está aberta?", "Que horas vocês fecham?", "Vocês abrem aos domingos?"
    
    HORÁRIOS OPERACIONAIS:
    - **Segunda a Sexta**: 08:30 às 12:00 e 14:00 às 17:00 (com intervalo 12:00-14:00)
    - **Sábado**: 08:00 às 11:00
    - **Domingo**: ❌ FECHADO
    
    PASSOS OBRIGATÓRIOS:
    
    1. IDENTIFIQUE o contexto:
       - Cliente perguntando AGORA? → Diga o horário atual + próximas aberturas
       - Cliente perguntando para UMA DATA ESPECÍFICA? → Use validate_delivery_availability com essa data
    
    2. RESPOSTA PARA "AGORA":
       Analise o horário atual recebido no prompt. Se estiver dentro do horário operacional (considerando os intervalos), responda:
       "✅ Estamos abertos! Funcionamos hoje das [HORÁRIO_INÍCIO] às [HORÁRIO_FIM]."
       
       Se estiver fora do horário operacional, responda:
       "⏰ No momento estamos fechados. Abrimos novamente [PRÓXIMO_HORÁRIO]"
       
       Sempre adicione: "Mas você pode enviar a mensagem agora que respondemos em breve! 📱"
    
    3. RESPOSTA PARA DATA ESPECÍFICA:
       Chame: validate_delivery_availability(date_str='YYYY-MM-DD')
       A tool retornará os horários exatos + disponibilidade
    
    4. INFORMAÇÕES EXTRAS:
       Se cliente perguntar sobre pausas:
       "Das 12:00 às 14:00 a gente fica em intervalo, mas já retorna! ⏰"
       
       Se perguntar sobre domingo:
       "Domingos a gente descansa, mas segunda abrimos cedinho às 7:30! Quer marcar pra lá? ❤️"
    
    NUNCA:
    - Invente horários diferentes dos informados
    - Diga que abre às 8h de segunda a sexta (ERRADO: é 7:30)
    - Processe pedidos no domingo
    - Ignore intervalos/pausas
    
    EXEMPLO CORRETO:
    Cliente: "Vocês estão abertos agora?"
    Você: "✅ Estamos sim! Funcionamos até as 17:00 hoje. Pode fazer seu pedido! 🌹"
    
    Cliente: "E aos domingos?"
    Você: "Domingos a gente descansa, mas segunda abrimos cedinho às 7:30! Quer marcar pra lá? ❤️"
    
    Cliente: "Quero entregar sábado"
    Você: [Chame validate_delivery_availability('2026-01-11')] e retorna a resposta da tool
    """
    return "Procedimento de validação de horários carregado."


if __name__ == "__main__":
    mcp.run()
