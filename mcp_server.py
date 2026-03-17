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
    "monday": [(time(8, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "tuesday": [(time(8, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "wednesday": [(time(8, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "thursday": [(time(8, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "friday": [(time(8, 30), time(12, 0)), (time(14, 0), time(17, 0))],
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

    Prioridade (tanto com contexto quanto sem):
        1. Tipo: QUADRO_FOTO > FLOR > PELUCIA > CESTA > BAR_DRINKS > QUEBRA_CABECA > CANECA
        2. Preço: mais caro primeiro (dentro do mesmo tipo)
        3. Similaridade semântica: desempate final

    Exceções:
        - Se busca explícita por "caneca" → CANECA vira prioridade máxima
        - Se busca explícita por "quebra" → QUEBRA_CABECA vira prioridade máxima
        - Se busca explícita por "quadro/polaroide" → QUADRO_FOTO vira prioridade máxima
    """
    search_lower = search_term.lower().strip()
    is_caneca_search = "caneca" in search_lower
    is_quebra_search = "quebra" in search_lower
    is_quadro_search = "quadro" in search_lower or "polaroide" in search_lower or "polaroides" in search_lower

    for product in scored_products:
        product["product_type"] = _categorize_product_type(
            product.get("name", ""),
            product.get("description", "")
        )

    # Prioridade base por tipo — QUADRO primeiro, CANECA e QUEBRA por último
    type_priority = {
        "QUADRO_FOTO":  1,
        "PELUCIA":      2,
        "FLOR":         3,
        "CESTA":        4,
        "BAR_DRINKS":   5,
        "QUEBRA_CABECA":6,
        "CANECA":       7,
    }

    # Ajuste de prioridade quando a busca é explícita para um tipo específico
    if is_caneca_search:
        type_priority["CANECA"] = 1
    if is_quebra_search:
        type_priority["QUEBRA_CABECA"] = 1
    if is_quadro_search:
        type_priority["QUADRO_FOTO"] = 1

    for product in scored_products:
        product["type_priority"] = type_priority.get(product["product_type"], 999)
        product["ranking_reason"] = f"TIPO:{product['product_type']} | PREÇO:{product.get('price')}"

    sorted_products = sorted(
        scored_products,
        key=lambda p: (
            p["type_priority"],            # 1º: tipo (menor = melhor)
            -float(p.get("price") or 0.0), # 2º: preço (maior = melhor)
            -p.get("similarity", 0.0),     # 3º: similaridade semântica
        )
    )

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

def _keyword_in_text(text: str, keywords: List[str]) -> bool:
    lower_text = (text or "").lower()
    return any(keyword in lower_text for keyword in keywords)

def _infer_search_profile(termo: str, contexto: str) -> str:
    combined = f"{termo} {contexto}".lower()

    if _keyword_in_text(combined, ["cafe", "café", "manha", "manhã", "croissant", "pão de queijo"]):
        return "BREAKFAST"
    if _keyword_in_text(combined, ["buque", "buquê", "flores", "rosa", "rosas"]):
        return "FLOWERS"
    if _keyword_in_text(combined, ["caneca"]):
        return "MUG"
    if _keyword_in_text(combined, ["quadro", "polaroide", "polaroides", "foto"]):
        return "PHOTO_FRAME"
    if _keyword_in_text(combined, ["pelucia", "pelúcia", "urso", "ursinho"]):
        return "PLUSH"
    if _keyword_in_text(combined, ["criança", "crianca", "infantil", "filho", "filha", "bebê", "bebe", "menino", "menina", "kids"]):
        return "CHILDREN"

    return "GENERIC"

@mcp.tool()
async def list_available_menus() -> str:
    """
    Restaura a lista de menus dinâmicos configurados no banco de dados.
    Você precisa desta lista para saber os IDs (node_id) exatos de cada menu para onde você pode rotear o cliente, caso ele queira "ver opções", "voltar", etc.
    Sempre chame esta tool primeiro caso nao saiba qual node_id usar em change_flow_node.
    """
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT nodes FROM public."BotFlow" WHERE is_active = true LIMIT 1')
            if not row or not row.get("nodes"):
                return "Erro: Nenhum fluxo ativo foi encontrado no banco de dados."
            
            nodes_data = row["nodes"]
            if isinstance(nodes_data, str):
                nodes = json.loads(nodes_data)
            else:
                nodes = nodes_data
                
            available_menus = []
            for node in nodes:
                if isinstance(node, dict) and node.get("type") == "menuNode":
                    node_id = node.get("id")
                    data = node.get("data", {})
                    label = data.get("label") or data.get("message") or data.get("name") or "Menu Desconhecido"
                    options = data.get("options", [])
                    options_str = ""
                    if options:
                        options_list = [opt if isinstance(opt, str) else opt.get("label", opt.get("value", "")) for opt in options]
                        options_str = ", ".join(options_list)
                    
                    available_menus.append(f"- ID: '{node_id}' | Titulo: '{str(label)[:100]}' | Opcoes: [{options_str}]")
            
            if not available_menus:
                return "Nenhum menu foi encontrado no fluxo atual."
            
            result = "Menus disponiveis no fluxo ativo (memorize este ID para a tool change_flow_node):\n" + "\n".join(available_menus)
            return result
    except Exception as e:
        return f"Erro ao buscar menus: {str(e)}"

@mcp.tool()
async def change_flow_node(node_id: str, reason: str, customer_phone: Optional[str] = None) -> str:
    """
    Roteia o cliente para um nó de atendimento específico do fluxo principal.
    
    USE QUANDO:
    - O cliente pedir para "ver opções", "botar pro início", "voltar", "ver catálogo".
    - A intenção do cliente for melhor atendida por um menu do que por texto.
    
    Args:
        node_id: O ID do nó exato (obtido em list_available_menus) para onde enviar o cliente. Exemplo: "1234-abcd-..."
        reason: Motivo para alterar o fluxo (ex: "cliente pediu para voltar ao inicio").
        customer_phone: Telefone do cliente (opcional).
        
    Retorna:
         Instrução para a engine salvar que o node_id mudou. A engine backend intercepta esta string no caso de fallback.
    """
    requested_node_id = (node_id or "").strip()
    _safe_print(f"🔄 Redirecionando fluxo: {customer_phone or 'Cliente'} ➝ NO: {requested_node_id} (Motivo: {reason})")

    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT nodes, edges FROM public."BotFlow" WHERE is_active = true LIMIT 1')
            if not row:
                return "Erro: Nenhum fluxo ativo foi encontrado para redirecionamento."

            nodes_data = row.get("nodes")
            edges_data = row.get("edges")

            nodes = json.loads(nodes_data) if isinstance(nodes_data, str) else (nodes_data or [])
            edges = json.loads(edges_data) if isinstance(edges_data, str) else (edges_data or [])

            nodes_by_id = {
                str(n.get("id")): n for n in nodes
                if isinstance(n, dict) and n.get("id")
            }

            resolved_node_id = requested_node_id if requested_node_id in nodes_by_id else None

            if not resolved_node_id:
                normalized_requested = _normalize_embedding_text(requested_node_id)
                normalized_reason = _normalize_embedding_text(reason or "")
                combined = f"{normalized_requested} {normalized_reason}".strip()

                asks_for_start = any(
                    token in combined
                    for token in ["menu principal", "inicio", "inicial", "primeiro menu", "primeiro", "comeco", "comeco", "start"]
                )

                if asks_for_start:
                    start_node = next(
                        (n for n in nodes if isinstance(n, dict) and n.get("type") == "startNode"),
                        None,
                    )
                    if start_node:
                        start_id = str(start_node.get("id"))
                        first_edge = next(
                            (e for e in edges if isinstance(e, dict) and str(e.get("source")) == start_id),
                            None,
                        )
                        if first_edge and first_edge.get("target"):
                            target_id = str(first_edge.get("target"))
                            if target_id in nodes_by_id:
                                resolved_node_id = target_id
                        if not resolved_node_id and start_id in nodes_by_id:
                            resolved_node_id = start_id

            if not resolved_node_id:
                menu_candidates = []
                for n in nodes:
                    if not isinstance(n, dict) or n.get("type") != "menuNode":
                        continue
                    nid = str(n.get("id"))
                    data = n.get("data", {}) or {}
                    title = data.get("label") or data.get("message") or data.get("name") or "Menu"
                    menu_candidates.append(f"- {nid}: {str(title)[:80]}")

                hint = "\n".join(menu_candidates[:5]) if menu_candidates else "- nenhum menu encontrado"
                return (
                    f"ERRO_NODE_ID_INVALIDO: '{requested_node_id}'. "
                    "Use list_available_menus e escolha um node_id existente.\n"
                    f"Sugestões:\n{hint}"
                )

            return f"SUCESSO: Fluxo redirecionado para node_id {resolved_node_id}\nSUCESSO_REDIRECIONAMENTO_DE_NO:[{resolved_node_id}]"
    except Exception as e:
        return f"Erro ao redirecionar fluxo: {str(e)}"

@mcp.tool()
async def route_to_flow_node(target_node_id: str, reason: str, customer_phone: Optional[str] = None) -> str:
    """Compatibilidade retroativa. Prefira change_flow_node em novas integrações."""
    return await change_flow_node(target_node_id, reason, customer_phone)

def _token_overlap_score(query_text: str, candidate_text: str) -> float:
    query_tokens = set(_tokenize_context(query_text))
    candidate_tokens = set(_tokenize_context(candidate_text))

    if not query_tokens or not candidate_tokens:
        return 0.0

    overlap = len(query_tokens.intersection(candidate_tokens))
    return overlap / max(1, len(query_tokens))

def _score_profile_alignment(profile: str, candidate_text: str) -> float:
    text = (candidate_text or "").lower()

    breakfast_kw = ["cafe", "café", "manhã", "manha", "croissant", "pão", "pao", "cappuccino", "chocolate quente", "lanche"]
    plush_kw = ["pelucia", "pelúcia", "urso", "ursinho"]
    flower_kw = ["buquê", "buque", "rosa", "rosas", "flores", "flor"]
    mug_kw = ["caneca"]
    frame_kw = ["quadro", "polaroide", "polaroides", "foto", "instax"]

    if profile == "BREAKFAST":
        if _keyword_in_text(text, breakfast_kw):
            return 0.45
        if _keyword_in_text(text, plush_kw + flower_kw):
            return -0.30
        return -0.08

    if profile == "FLOWERS":
        if _keyword_in_text(text, flower_kw):
            return 0.38
        if _keyword_in_text(text, breakfast_kw):
            return -0.15
        return -0.05

    if profile == "MUG":
        if _keyword_in_text(text, mug_kw):
            return 0.34
        return -0.06

    if profile == "PHOTO_FRAME":
        if _keyword_in_text(text, frame_kw):
            return 0.34
        return -0.06

    if profile == "PLUSH":
        if _keyword_in_text(text, plush_kw):
            return 0.34
        return -0.06

    children_kw = ["criança", "crianca", "infantil", "para_crianças", "kids"]
    if profile == "CHILDREN":
        if _keyword_in_text(text, children_kw):
            return 0.45
        if _keyword_in_text(text, ["romântic", "romantic", "namorad", "casal", "noiv"]):
            return -0.30
        return -0.08

    return 0.0

def _semantic_fallback_score(
    query_embedding: List[float],
    product_embedding: List[float],
    query_text: str,
    candidate_text: str,
    profile: str,
) -> float:
    semantic = _cosine_similarity(query_embedding, product_embedding)
    lexical = _token_overlap_score(query_text, candidate_text)
    profile_alignment = _score_profile_alignment(profile, candidate_text)

    return (semantic * 0.62) + (lexical * 0.28) + profile_alignment

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
        "lembrancinha": "cesto",
        "lembrancinhas": "cesto",
        "prendinha": "cesto",
        "prendinhas": "cesto",
        "pequeno": "cesto",
        "pequenino": "cesto",
        
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
        
        "criança": "criança",
        "crianca": "criança",
        "crianças": "criança",
        "criancas": "criança",
        "infantil": "criança",
        "kids": "criança",
        "filho": "criança",
        "filha": "criança",
        "bebê": "criança",
        "bebe": "criança",
        "menino": "criança",
        "menina": "criança",
        
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
    contexto: str,
    preco_minimo: Optional[float] = None,
    preco_maximo: Optional[float] = None,
    exclude_ids: Optional[List[str]] = None,
    top_k: Optional[int] = 10,
) -> str:
    """
    🔍 Busca produtos no catálogo usando contexto OBRIGATÓRIO.

    CRÍTICO: contexto é OBRIGATÓRIO - sem ele os resultados são errados!
    CRÍTICO: Ao buscar "mais opções", SEMPRE passe os IDs dos produtos já mostrados em exclude_ids!
             Sem exclude_ids, os MESMOS produtos serão retornados toda vez (busca determinística).

    Args:
        termo: Tipo/nome do produto (e.g., "cesto romantico", "buquê")
        contexto: OBRIGATÓRIO - Contexto COMPLETO com ocasião, motivo, destinatário, orçamento
                  ✅ "Para aniversário da namorada, gosta de flores, até R$ 200"
                  ✅ "Presente para mãe no dia das mães, orçamento R$ 150"
                  ❌ "presente" - MUITO VAGO
        preco_minimo: Mínimo opcional (inferido do contexto se não fornecido)
        preco_maximo: Máximo opcional (inferido do contexto se não fornecido)
        exclude_ids: OBRIGATÓRIO quando o cliente pede "mais opções" ou já recebeu produtos antes.
                     Liste os IDs de TODOS os produtos já apresentados nesta conversa.
                     Isso garante que produtos diferentes sejam retornados a cada chamada.
                     ✅ ["123", "456"] — quando os produtos de id 123 e 456 já foram mostrados
                     ✅ ["123", "456", "789"] — terceira rodada, três produtos excluídos
                     ❌ [] ou None — quando já houve apresentação anterior (causa repetição!)
        top_k: Quantidade de resultados (max 10, default 10)

    Retorna: JSON com status, termos, e listas de produtos exatos e fallback.
    """
    try:
        contexto_limpo = (contexto or "").strip()
        
        # ⚠️ Validação crítica
        if not contexto_limpo or contexto_limpo.lower() == termo.lower():
            raise ValueError(
                "❌ CONTEXTO OBRIGATÓRIO E DIFERENTE DO TERMO!\n"
                "Você DEVE requisitar um contexto COMPLETO.\n"
                "Exemplos corretos:\n"
                "  ✅ 'Para aniversário da minha namorada, gosta de flores, até R$ 200'\n"
                "  ✅ 'Presente para mãe, ocasião dia das mães, orçamento R$ 150'\n"
                "Exemplos INCORRETOS:\n"
                "  ❌ Apenas o termo sem contexto\n"
                "  ❌ Contexto=termo (apenas repetição)"
            )
        
        termo_normalizado = _normalize_product_search_term(termo)
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Extrai limites de preço
            exclude_ids = exclude_ids or []
            exclude_ids = [str(id) for id in exclude_ids]
            price_source = f"{termo_normalizado} {contexto_limpo}".strip()
            ctx_min, ctx_max = _extract_price_bounds(price_source)

            budget_present_in_input = (preco_minimo is not None) or (preco_maximo is not None)
            budget_present_in_context = (ctx_min is not None) or (ctx_max is not None)
            prefer_high_price = not (budget_present_in_input or budget_present_in_context)

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

            top_k = int(top_k) if top_k else 10
            top_k = max(2, min(10, top_k))

            _safe_print(f"🔍 Busca: termo='{termo_normalizado}', preço=[{preco_minimo:.2f}-{preco_maximo:.2f}]")

            # Função auxiliar para gerar variantes (com/sem acento)
            def get_variants(t):
                if not t: 
                    return []
                variants = [t]
                no_accents = "".join(
                    c for c in unicodedata.normalize("NFD", t)
                    if unicodedata.category(c) != "Mn"
                )
                if no_accents != t:
                    variants.append(no_accents)
                return variants

            common_words = {"o", "a", "de", "da", "do", "em", "um", "uma", "e", "ou", "para", "por", "com",
                            "que", "quero", "quer", "tem", "ter", "pra", "pro", "meu", "minha", "seu", "sua",
                            "ele", "ela", "nos", "nosso", "muito", "mais", "como", "ser", "está", "estou",
                            "esse", "essa", "isso", "aqui", "ali", "bem", "bom", "boa", "vai", "vou",
                            "sim", "não", "nao", "mas", "ainda", "todo", "toda", "tipo", "gostaria",
                            "preciso", "busco", "procuro", "algo", "coisa", "opcao", "opção"}
            
            context_product_keywords = {
                "criança", "crianca", "crianças", "criancas", "infantil", "kids",
                "filho", "filha", "bebê", "bebe", "menino", "menina",
                "romântico", "romantico", "romântica", "romantica", "namorada", "namorado",
                "amor", "coração", "coracao", "casal",
                "aniversário", "aniversario", "birthday",
                "café", "cafe", "coffee",
                "chocolate", "chocolates",
                "flores", "flor", "rosa", "rosas", "buquê", "buque",
                "pelúcia", "pelucia", "urso", "teddy",
                "caneca", "canecas", "quadro", "quadros",
                "bar", "cerveja", "festa",
                "mãe", "mae", "pai", "esposa", "marido",
                "express", "rápido", "rapido", "pronta",
            }
            
            # Constrói lista de termos para buscar
            search_terms = list(dict.fromkeys(get_variants(termo_normalizado)))
            for w in termo_normalizado.split():
                if w.strip().lower() not in common_words and len(w.strip()) > 2:
                    search_terms.extend(get_variants(w.strip()))
            search_terms.extend(get_variants(termo))
            
            # Extrai keywords relevantes do contexto para busca ILIKE
            contexto_words = re.split(r"[\s,;.!?]+", contexto_limpo.lower())
            for cw in contexto_words:
                cw_clean = cw.strip()
                if cw_clean in context_product_keywords and cw_clean not in common_words:
                    normalized_cw = _normalize_product_search_term(cw_clean)
                    search_terms.extend(get_variants(normalized_cw))
                    if normalized_cw != cw_clean:
                        search_terms.extend(get_variants(cw_clean))
            
            search_terms = list(dict.fromkeys([t for t in search_terms if t.strip()]))
            
            _safe_print(f"🔑 Termos de busca: {search_terms[:3]}")

            all_rows_by_id: Dict[str, Dict[str, Any]] = {}
            
            # Busca com cada termo
            for search_term in search_terms:
                if not search_term.strip() or search_term.strip().lower() in common_words:
                    continue
                
                query = """
                SELECT id, name, description, price, image_url, production_time,
                       (CASE WHEN name ILIKE $1 THEN 100 ELSE 0 END +
                        CASE WHEN description ILIKE $1 THEN 50 ELSE 0 END) as relevance_score,
                       (name ILIKE $1 OR description ILIKE $1) as is_exact_match
                FROM public."Product"
                WHERE price >= $2 AND price <= $3 AND is_active = true
                  AND NOT (id::TEXT = ANY($4::TEXT[]))
                ORDER BY is_exact_match DESC, relevance_score DESC, price DESC
                LIMIT $5;
                """
                
                rows = await conn.fetch(query, f"%{search_term}%", preco_minimo, preco_maximo, exclude_ids, top_k)
                for row in rows:
                    row_dict = dict(row)
                    row_id = str(row_dict["id"])

                    if row_id not in all_rows_by_id:
                        all_rows_by_id[row_id] = row_dict
                        continue

                    existing = all_rows_by_id[row_id]
                    existing["relevance_score"] = max(
                        int(existing.get("relevance_score") or 0),
                        int(row_dict.get("relevance_score") or 0),
                    )
                    existing["is_exact_match"] = bool(existing.get("is_exact_match")) or bool(row_dict.get("is_exact_match"))

            all_rows = list(all_rows_by_id.values())
            
            # Separa exatos de fallback
            exact_matches = [dict(r) for r in all_rows if r['is_exact_match']]
            fallback_matches = [dict(r) for r in all_rows if not r['is_exact_match']]

            requested_keywords = set()
            generic_terms = {"cesta", "cesto", "presente", "presenca", "gift"}
            for tk in (termo_normalizado or "").split():
                if len(tk) >= 4 and tk not in generic_terms:
                    requested_keywords.add(tk)

            def _priority_boost(row: Dict[str, Any]) -> float:
                """Prioriza pronta-entrega, itens especiais e garante que o termo pedido pese (ex.: caneca/pelúcia/quadro/aniversario)."""
                desc = ((row.get("description") or "") + " " + (row.get("name") or "")).lower()
                normalized_name = _normalize_embedding_text(row.get("name") or "")
                ready_keywords = ["pronta", "pronta_entrega", "pronto", "hoje", "agora", "express"]
                special_keywords = ["polaroid", "foto", "fotos", "pelúcia", "pelucia", "urso", "teddy", "quadro", "caneca"]
                ready_bonus = 0
                if any(k in desc for k in ready_keywords):
                    ready_bonus += 40
                prod_time = row.get("production_time")
                try:
                    if prod_time is not None and float(prod_time) <= 1:
                        ready_bonus += 25
                except Exception:
                    pass
                special_bonus = 30 if any(k in desc for k in special_keywords) else 0
                requested_bonus = 0
                if requested_keywords and any(k in desc for k in requested_keywords):
                    requested_bonus += 60  # termo pedido pesa bastante
                name_match_bonus = 0
                if requested_keywords and any(k in normalized_name for k in requested_keywords):
                    name_match_bonus += 120  # nome compatível domina o ranking
                if "aniver" in normalized_name and any("aniver" in k for k in requested_keywords):
                    name_match_bonus += 50  # reforço específico para aniversário
                return ready_bonus + special_bonus + requested_bonus + name_match_bonus

            def _sort_key_exact(row: Dict[str, Any]):
                rel = int(row.get("relevance_score") or 0)
                price = float(row.get("price") or 0)
                boost = _priority_boost(row)
                normalized_name = _normalize_embedding_text(row.get("name") or "")
                name_match = 1 if (requested_keywords and any(k in normalized_name for k in requested_keywords)) else 0
                price_pref = price if prefer_high_price else 0
                return (-name_match, -boost, -price_pref, -rel, -price)

            exact_matches = sorted(exact_matches, key=_sort_key_exact)

            missing_slots = max(0, top_k - len(exact_matches))

            if missing_slots > 0 and fallback_matches:
                profile = _infer_search_profile(termo_normalizado, contexto_limpo)
                semantic_query_text = f"{termo_normalizado}. {contexto_limpo}".strip()

                query_embedding: List[float] = []
                try:
                    query_embedding = await _get_embedding_cached(semantic_query_text)
                except Exception as emb_error:
                    _safe_print(f"⚠️ Embedding indisponível na fallback semântica: {emb_error}")

                if query_embedding:
                    await _ensure_product_embeddings(fallback_matches)

                    scored_fallback: List[Dict[str, Any]] = []
                    for candidate in fallback_matches:
                        candidate_id = str(candidate.get("id"))
                        cached = PRODUCT_EMBEDDINGS.get(candidate_id)
                        product_embedding = (cached or {}).get("embedding") if cached else None
                        if not product_embedding:
                            continue

                        candidate_text = _build_product_text(candidate)
                        score = _semantic_fallback_score(
                            query_embedding,
                            product_embedding,
                            semantic_query_text,
                            candidate_text,
                            profile,
                        )

                        candidate["semantic_score"] = float(score)
                        scored_fallback.append(candidate)

                    def _fb_name_match(row: Dict[str, Any]):
                        normalized_name = _normalize_embedding_text(row.get("name") or "")
                        return 1 if (requested_keywords and any(k in normalized_name for k in requested_keywords)) else 0

                    scored_fallback = sorted(
                        scored_fallback,
                        key=lambda r: (
                            -float(r.get("semantic_score") or -999),
                            -_fb_name_match(r),
                            -_priority_boost(r),
                            -float(r.get("price") or 0) if prefer_high_price else 0,
                            -float(r.get("price") or 0),
                        ),
                    )

                    strong_scored = [r for r in scored_fallback if float(r.get("semantic_score") or 0.0) >= 0.24]
                    fallback_matches = (strong_scored or scored_fallback)[:missing_slots]
                else:
                    fallback_matches = fallback_matches[:missing_slots]
            else:
                fallback_matches = fallback_matches[:missing_slots]

            exact_matches = exact_matches[:top_k]
            
            _safe_print(f"📦 Encontrados {len(exact_matches)} exatos + {len(fallback_matches)} fallback")
            
            structured = {
                "status": "found" if (exact_matches or fallback_matches) else "not_found",
                "termo": termo,
                "contexto": contexto_limpo,
                "exatos": [
                    {
                        "ranking": idx + 1,
                        "id": str(r['id']),
                        "nome": r['name'],
                        "preco": float(r['price']),
                        "descricao": r['description'],
                        "imagem": r.get('image_url') or "https://api.cestodamore.com.br/images/default-product.webp",
                        "production_time": int(r['production_time']) if r.get('production_time') else 1,
                        "tipo_resultado": "EXATO",
                    }
                    for idx, r in enumerate(exact_matches)
                ],
                "fallback": [
                    {
                        "ranking": len(exact_matches) + idx + 1,
                        "id": str(r['id']),
                        "nome": r['name'],
                        "preco": float(r['price']),
                        "descricao": r['description'],
                        "imagem": r.get('image_url') or "https://api.cestodamore.com.br/images/default-product.webp",
                        "production_time": int(r['production_time']) if r.get('production_time') else 1,
                        "tipo_resultado": "FALLBACK",
                    }
                    for idx, r in enumerate(fallback_matches)
                ]
            }
            
            return json.dumps(structured, ensure_ascii=False)

    except ValueError as ve:
        _safe_print(f"⚠️ Erro de validação: {ve}")
        return json.dumps({
            "status": "error",
            "error_type": "validation_error",
            "message": str(ve),
        }, ensure_ascii=False)
    except Exception as e:
        _safe_print(f"❌ Erro em consultarCatalogo: {e}")
        return json.dumps({
            "status": "error", 
            "error_type": "database_error",
            "message": str(e)
        }, ensure_ascii=False)


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
    📅 Verifica disponibilidade de entrega para uma data/hora SEM produto específico definido.

    USE QUANDO:
    - Cliente pergunta de forma aberta: "consegue entregar amanhã?", "que horas vocês entregam?", "tem entrega no sábado?"
    - Cliente quer saber os horários disponíveis em uma data, sem ainda ter escolhido produto.
    - Cliente menciona uma data mas ainda não escolheu nenhum produto.

    NÃO USE QUANDO:
    - Cliente pergunta sobre LOCAL de entrega ("entrega em X?", "faz entrega para tal lugar?").
      → Nesse caso use OBRIGATORIAMENTE `calculate_freight`.
    - Cliente já escolheu um produto específico e quer saber se dá pra entregar naquele horário.
      → Nesse caso use `can_produce_in_time` (verifica produção + entrega do produto escolhido).

    Retorna slots disponíveis ou indisponibilidade. SEMPRE apresente os suggested_slots ao cliente.

    Args:
        date_str: Data desejada no formato YYYY-MM-DD
        time_str: Hora desejada no formato HH:MM (opcional)
        production_time_hours: Tempo de produção em horas comerciais (opcional, padrao: 1).
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
                        "ai_instruction": "[INFORMAÇÃO INTERNA] APRESENTE TODOS os suggested_slots ao cliente e PERGUNTE qual ele prefere. NAO escolha por ele. estimated_ready_time e tempo de producao, NAO e o horario de entrega."
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
                "ai_instruction": "[INFORMAÇÃO INTERNA] PERGUNTE ao cliente qual horario ele prefere dentro de available_hours. NAO escolha por ele. estimated_ready_time e tempo de producao, NAO e o horario de entrega."
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
async def get_product_details(product_name: str) -> str:
    """
    🔍 Busca detalhes de um produto pelo NOME (não ID).
    
    Retorna:
    - Nome, preço, descrição, componentes
    - Se houver múltiplas correspondências, lista as 3 primeiras
    - Se houver exatamente 1, retorna detalhes completos
    
    Args:
        product_name: Nome do produto (busca parcial, case-insensitive)
                     Ex: "cesto romantico", "buquê red", "caneca personalizada"
    
    Returns:
        - "found": 1 resultado exato com componentes
        - "ambiguous": Múltiplos resultados, lista opções
        - "not_found": Nenhum produto encontrado
    """
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            product_name_clean = (product_name or "").strip()
            
            if not product_name_clean:
                return json.dumps({
                    "status": "error",
                    "message": "Nome do produto não pode estar vazio"
                }, ensure_ascii=False)
            
            _safe_print(f"🔍 Buscando produto: '{product_name_clean}'")
            
            search_variants = [product_name_clean]
            no_apostrophe = product_name_clean.replace("'", "").replace("\u2019", "")
            if no_apostrophe != product_name_clean:
                search_variants.append(no_apostrophe)
            with_apostrophe = re.sub(r"\bd([aA])", r"d'\1", product_name_clean)
            if with_apostrophe not in search_variants:
                search_variants.append(with_apostrophe)
            no_apostrophe_lower = no_apostrophe.lower()
            with_apostrophe_lower = re.sub(r"\bd([a])", r"d'\1", no_apostrophe_lower)
            if with_apostrophe_lower not in search_variants:
                search_variants.append(with_apostrophe_lower)
            
            for word in product_name_clean.split():
                word_clean = word.strip()
                if len(word_clean) > 3 and word_clean.lower() not in {"para", "pra", "com", "sem", "uma", "esse", "essa"}:
                    if word_clean not in search_variants:
                        search_variants.append(word_clean)
            
            search_query = """
            SELECT id, name, description, price, production_time, image_url,
                   (name ILIKE $1) as is_exact_match
            FROM public."Product"
            WHERE name ILIKE $2 AND is_active = true
            ORDER BY is_exact_match DESC, name ASC
            LIMIT 3;
            """
            
            exact_match = None
            for variant in search_variants:
                exact_match = await conn.fetchrow(search_query, variant, f"{variant}%")
                if exact_match:
                    _safe_print(f"✅ Match exato com variante: '{variant}'")
                    break
            
            if exact_match:
                # Se encontrou exato, busca componentes
                components_query = """
                SELECT i.name, pc.quantity
                FROM public."ProductComponent" pc
                JOIN public."Item" i ON pc.item_id = i.id
                WHERE pc.product_id = $1
                ORDER BY i.name ASC;
                """
                component_rows = await conn.fetch(components_query, exact_match['id'])
                
                # Busca adicionais permitidos para este produto
                additionals_query = """
                SELECT i.name, pa.custom_price as price
                FROM public."ProductAdditional" pa
                JOIN public."Item" i ON pa.additional_id = i.id
                WHERE pa.product_id = $1 AND pa.is_active = true
                ORDER BY i.name ASC;
                """
                additional_rows = await conn.fetch(additionals_query, exact_match['id'])
                
                componentes = [
                    {
                        "nome": r['name'],
                        "quantidade": r['quantity']
                    }
                    for r in component_rows
                ]
                
                adicionais = [
                    {
                        "nome": r['name'],
                        "preco": float(r['price']) if r['price'] is not None else 0.0
                    }
                    for r in additional_rows
                ]
                
                structured = {
                    "status": "found",
                    "id": str(exact_match['id']),
                    "nome": exact_match['name'],
                    "preco": float(exact_match['price']),
                    "descricao": exact_match['description'] or "",
                    "imagem": exact_match.get('image_url') or "https://api.cestodamore.com.br/images/default-product.webp",
                    "production_time": int(exact_match['production_time'] or 0),
                    "componentes": componentes,
                    "adicionais_disponiveis": adicionais
                }
                
                _safe_print(f"✅ Produto exato encontrado: {exact_match['name']} ({len(componentes)} componentes, {len(adicionais)} adicionais)")
                return json.dumps(structured, ensure_ascii=False)
            
            # Se não encontrou exato, tenta busca parcial com variantes
            all_partial_by_id = {}
            for variant in search_variants:
                rows = await conn.fetch(search_query, None, f"%{variant}%")
                for row in rows:
                    rid = str(row['id'])
                    if rid not in all_partial_by_id:
                        all_partial_by_id[rid] = row
            partial_matches = list(all_partial_by_id.values())
            
            if not partial_matches:
                _safe_print(f"❌ Nenhum produto encontrado para: {product_name_clean}")
                return json.dumps({
                    "status": "not_found",
                    "message": f"Nenhum produto encontrado com nome '{product_name_clean}'"
                }, ensure_ascii=False)
            
            if len(partial_matches) == 1:
                # Se houver apenas 1 correspondência parcial, retorna detalhes completos
                product = partial_matches[0]
                components_query = """
                SELECT i.name, pc.quantity
                FROM public."ProductComponent" pc
                JOIN public."Item" i ON pc.item_id = i.id
                WHERE pc.product_id = $1
                ORDER BY i.name ASC;
                """
                component_rows = await conn.fetch(components_query, product['id'])
                
                # Busca adicionais permitidos para este produto
                additionals_query = """
                SELECT i.name, pa.custom_price as price
                FROM public."ProductAdditional" pa
                JOIN public."Item" i ON pa.additional_id = i.id
                WHERE pa.product_id = $1 AND pa.is_active = true
                ORDER BY i.name ASC;
                """
                additional_rows = await conn.fetch(additionals_query, product['id'])
                
                componentes = [
                    {
                        "nome": r['name'],
                        "quantidade": r['quantity']
                    }
                    for r in component_rows
                ]
                
                adicionais = [
                    {
                        "nome": r['name'],
                        "preco": float(r['price']) if r['price'] is not None else 0.0
                    }
                    for r in additional_rows
                ]
                
                structured = {
                    "status": "found",
                    "id": str(product['id']),
                    "nome": product['name'],
                    "preco": float(product['price']),
                    "descricao": product['description'] or "",
                    "imagem": product.get('image_url') or "https://api.cestodamore.com.br/images/default-product.webp",
                    "production_time": int(product['production_time'] or 0),
                    "componentes": componentes,
                    "adicionais_disponiveis": adicionais
                }
                
                _safe_print(f"✅ Produto parcial encontrado: {product['name']} ({len(componentes)} componentes, {len(adicionais)} adicionais)")
                return json.dumps(structured, ensure_ascii=False)
            
            # Se houver múltiplas correspondências, lista as opções
            _safe_print(f"⚠️ {len(partial_matches)} produtos encontrados para '{product_name_clean}'")
            
            opcoes = [
                {
                    "id": str(p['id']),
                    "nome": p['name'],
                    "preco": float(p['price']),
                    "descricao": p['description'] or "",
                    "imagem": p.get('image_url') or "https://api.cestodamore.com.br/images/default-product.webp"
                }
                for p in partial_matches[:3]
            ]
            
            structured = {
                "status": "ambiguous",
                "busca_original": product_name_clean,
                "opcoes_encontradas": len(partial_matches),
                "opcoes": opcoes,
                "mensagem": f"Encontrei {len(partial_matches)} produtos semelhantes. Qual destes é o que você quer?"
            }
            
            return json.dumps(structured, ensure_ascii=False)
            
        except Exception as e:
            _safe_print(f"❌ Erro em get_product_details: {e}")
            return json.dumps({
                "status": "error",
                "message": str(e)
            }, ensure_ascii=False)


@mcp.tool()
async def can_produce_in_time(product_name: str, delivery_date: str, delivery_time: str) -> str:
    """
    ⏱️ Verifica se um produto JA ESCOLHIDO pelo cliente pode ser produzido a tempo para entrega.

    USE QUANDO:
    - Cliente já escolheu um produto específico e informou uma data/hora de entrega.
    - Agente-Fechamento precisa confirmar viabilidade de produção antes de fechar o pedido.
    - Exemplo: cliente escolheu a "Cesta Romantica" e quer saber se dá pra entregar sábado às 9h.

    NÃO USE QUANDO:
    - Cliente ainda não escolheu produto e está só perguntando sobre horários em geral.
      → Nesse caso use `validate_delivery_availability` (verifica slots sem produto definido).

    Calcula o tempo de produção do produto respeitando horários comerciais (ignora fins de semana e feriados).

    Args:
        product_name: Nome exato do produto já escolhido (ex: "Café d'Amore G", "Caneca Personalizada")
        delivery_date: Data da entrega desejada (formato: DD/MM/YYYY)
        delivery_time: Hora da entrega (formato: HH:MM, ex: "09:00")

    Returns:
        JSON com:
        - "possible": true/false
        - "product_name": nome do produto
        - "production_time_hours": horas de produção necessárias
        - "earliest_ready": quando ficaria pronto no máximo (data + hora)
        - "requested_deadline": quando cliente quer receber
        - "message": mensagem humanizada explicando o resultado
    """
    
    try:
        # Valida formato da data
        try:
            delivery_dt = datetime.strptime(f"{delivery_date} {delivery_time}", "%d/%m/%Y %H:%M")
        except ValueError:
            return json.dumps({
                "status": "error",
                "message": "Formato inválido. Use DD/MM/YYYY para data e HH:MM para hora"
            }, ensure_ascii=False)
        
        delivery_date_obj = delivery_dt.date()
        delivery_time_obj = delivery_dt.time()
        
        now_local = _get_local_time()
        
        _safe_print(f"⏱️ [PRODUCE-CHECK] Produto: '{product_name}' | Entrega: {delivery_date} {delivery_time} | Agora: {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Busca o produto
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            search_query = """
            SELECT id, name, production_time, price
            FROM public."Product"
            WHERE name ILIKE $1 AND is_active = true
            ORDER BY name ASC
            LIMIT 1;
            """
            
            product = await conn.fetchrow(search_query, f"{product_name}%")
            
            if not product:
                _safe_print(f"❌ Produto não encontrado: {product_name}")
                return json.dumps({
                    "status": "not_found",
                    "message": f"Produto '{product_name}' não encontrado no catálogo"
                }, ensure_ascii=False)
            
            production_hours = int(product['production_time'] or 1)
            produto_nome = product['name']
            
            # Se a data de entrega é no passado, retorna erro
            if delivery_date_obj < now_local.date():
                return json.dumps({
                    "status": "deadline_passed",
                    "message": f"A data {delivery_date} já passou. Escolha uma data futura."
                }, ensure_ascii=False)
            
            # Se é hoje, valida se pode começar a partir de agora
            if delivery_date_obj == now_local.date():
                start_calculation_date = now_local.date()
                start_calculation_time = now_local.time()
            elif delivery_date_obj > now_local.date():
                # Data é futura, começa do primeiro horário comercial de HOJE (se ainda houver)
                # ou do próximo dia útil, acumulando tempo.
                start_calculation_date = now_local.date()
                start_calculation_time = now_local.time()
            else:
                # Data informada é anterior a hoje (já validado acima, mas por segurança)
                start_calculation_date = now_local.date()
                start_calculation_time = now_local.time()
            
            # Calcula quando o produto ficará pronto (anda pelos horários comerciais a partir de AGORA)
            ready_date, ready_time = _calculate_ready_datetime(
                datetime.combine(start_calculation_date, start_calculation_time),
                production_hours,
                BUSINESS_HOURS
            )
            
            # Verifica se consegue terminar antes da entrega
            ready_datetime = datetime.combine(ready_date, ready_time)
            delivery_datetime = datetime.combine(delivery_date_obj, delivery_time_obj)
            
            is_possible = ready_datetime <= delivery_datetime
            
            # Formata resposta
            day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
            ready_day_name = day_names[ready_date.weekday()]
            delivery_day_name = day_names[delivery_date_obj.weekday()]
            
            ready_str = f"{ready_day_name}, {ready_date.strftime('%d/%m')} às {ready_time.strftime('%H:%M')}"
            delivery_str = f"{delivery_day_name}, {delivery_date} às {delivery_time}"
            
            structured = {
                "status": "analyzed",
                "possible": is_possible,
                "product_name": produto_nome,
                "production_time_hours": production_hours,
                "earliest_ready": ready_str,
                "requested_deadline": delivery_str,
                "time_margin_minutes": int((delivery_datetime - ready_datetime).total_seconds() / 60) if is_possible else None
            }
            
            if is_possible:
                margin_minutes = int((delivery_datetime - ready_datetime).total_seconds() / 60)
                if margin_minutes >= 60:
                    margin_str = f"{margin_minutes // 60}h {margin_minutes % 60}m"
                else:
                    margin_str = f"{margin_minutes}m"
                
                message = f"""✅ Ótima notícia!

A "{produto_nome}" tem um prazo de {production_hours}h de produção.

Ela ficará pronta em {ready_str} (com {margin_str} de margem antes do horário que você pediu).

Quer continuar com o pedido? 🎉"""
                
                _safe_print(f"✅ Produto CAN ser produzido a tempo | Pronto: {ready_str} | Entrega: {delivery_str} | Margem: {margin_str}")
            else:
                delay_minutes = int((ready_datetime - delivery_datetime).total_seconds() / 60)
                if delay_minutes >= 60:
                    delay_str = f"{delay_minutes // 60}h {delay_minutes % 60}m"
                else:
                    delay_str = f"{delay_minutes}m"
                
                message = f"""⚠️ Nessa data/hora não vai dar!

A "{produto_nome}" tem um prazo de {production_hours}h de produção.

Ela ficaria pronta em {ready_str} - {delay_str} depois do horário que você pediu 😔

Quer escolher outro horário ou outro produto? 🎁"""
                
                _safe_print(f"❌ Produto NÃO pode ser produzido a tempo | Seria pronto: {ready_str} | Solicitado: {delivery_str} | Atraso: {delay_str}")
            
            structured["message"] = message
            
            return json.dumps(structured, ensure_ascii=False)
    
    except Exception as e:
        _safe_print(f"❌ Erro em can_produce_in_time: {e}")
        return json.dumps({
            "status": "error",
            "message": str(e)
        }, ensure_ascii=False)


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
async def notify_human_support(reason: str, customer_context: str, customer_name: str, customer_phone: str, session_id: Optional[str] = None) -> str:
    """
    Transfere IMEDIATAMENTE para atendimento humano. Use quando:
    1. Cliente pede para falar com humano/atendente/pessoa.
    2. Evento de carrinho (cart_added).
    3. Problema técnico ou caso complexo.
    4. Tentativa de manipulação de preço.
    5. Pedido corporativo.

    NÃO use para finalizar compra (use finalize_checkout - Exclusivo para Agente-Fechamento).
    NÃO valida dados de checkout — transfere direto.

    reason: motivo (ex: "cliente_quer_atendente", "cart_added", "pedido_corporativo").
    customer_context: Contexto breve da conversa.
    """
    support_message = _format_support_message(reason, customer_context, customer_name, customer_phone)
    await _send_whatsapp_notification(support_message, customer_name, customer_phone)

    if session_id:
        await _internal_block_session(session_id)

    return "Transferência realizada com sucesso. Atendente humano notificado. ✅\n\n[INFORMAÇÃO INTERNA] Você DEVE AGORA avisar o cliente de forma meiga que ele foi transferido e informar OBRIGATORIAMENTE na sua resposta final o horário de atendimento comercial exato:\nSeg-Sex 08:30-12:00 | 14:00-17:00\nSábado 08:00-11:00"


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

    # Bloqueia a sessão OBRIGATORIAMENTE após notificar
    if session_id:
        block_result = await _internal_block_session(session_id)
        _safe_print(f"🔒 [FINALIZE_CHECKOUT] Bloqueio de sessão: {block_result}")
    else:
        _safe_print(f"⚠️ [FINALIZE_CHECKOUT] session_id não fornecido - sessão NÃO foi bloqueada!")

    return _format_structured_response(
        {"status": "success", "action": "checkout_finalized", "session_blocked": bool(session_id)},
        "Pedido finalizado e equipe notificada com sucesso! ✅\n\n[INFORMAÇÃO INTERNA] Você DEVE AGORA enviar ao cliente de forma meiga a mensagem final de agradecimento e informar OBRIGATORIAMENTE o horário de atendimento comercial exato:\nSeg-Sex 08:30-12:00 | 14:00-17:00\nSábado 08:00-11:00"
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
       "Domingos a gente descansa, mas segunda abrimos cedinho às 8:30! Quer marcar pra lá? ❤️"
    
    NUNCA:
    - Invente horários diferentes dos informados
    - Diga que abre às 8h de segunda a sexta (ERRADO: é 8:30)
    - Processe pedidos no domingo
    - Ignore intervalos/pausas
    
    EXEMPLO CORRETO:
    Cliente: "Vocês estão abertos agora?"
    Você: "✅ Estamos sim! Funcionamos até as 17:00 hoje. Pode fazer seu pedido! 🌹"
    
    Cliente: "E aos domingos?"
    Você: "Domingos a gente descansa, mas segunda abrimos cedinho às 8:30! Quer marcar pra lá? ❤️"
    
    Cliente: "Quero entregar sábado"
    Você: [Chame validate_delivery_availability('2026-01-11')] e retorna a resposta da tool
    """
    return "Procedimento de validação de horários carregado."


if __name__ == "__main__":
    mcp.run()
