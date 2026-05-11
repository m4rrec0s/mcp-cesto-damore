"""
Motor de busca semântica melhorado para catálogo de produtos.
Implementa:
- Query rewriting com sinônimos
- Ranking similarity-first
- Context windows
- Product relevance scoring (PHASE 1)
"""

import asyncio
import json
from typing import List, Dict, Any, Optional, Tuple, Set
from hashlib import sha256
from query_synonym_map import get_search_variants, get_related_products_keywords
from product_relevance_scorer import rank_products_by_relevance, calculate_product_relevance_score
import math

def _normalize_text(text: str) -> str:
    """Normaliza texto para comparação."""
    if not text:
        return ""
    return text.lower().strip()

async def search_products_semantic(
    query: str,
    products: List[Dict[str, Any]],
    query_embeddings_cache: Dict[str, List[float]],
    product_embeddings_cache: Dict[str, List[float]],
    context: Dict[str, Any] = None,
    max_results: int = 10,
    get_embeddings_func = None,
) -> Dict[str, Any]:
    """
    Busca semântica com ranking similarity-first e context windows.
    
    Args:
        query: Texto da busca
        products: Lista de produtos
        query_embeddings_cache: Cache de embeddings de queries
        product_embeddings_cache: Cache de embeddings de produtos
        context: Contexto da conversa (conversation_history, presentedProductIds, etc)
        max_results: Máximo de resultados
        get_embeddings_func: Função async para obter embeddings (mock em testes)
    
    Returns:
        {
            "products": [ranked products],
            "strategy": "SEMANTIC_EXPAND" | "VECTOR" | "KEYWORD",
            "rankingDetails": {...},
            "hadFallback": bool
        }
    """
    if not query or not products:
        return {
            "products": [],
            "strategy": "VECTOR",
            "rankingDetails": {"candidatesCount": 0},
            "hadFallback": False
        }
    
    context = context or {}
    presented_product_ids = set(context.get("presentedProductIds", []))
    current_phase = context.get("currentPhase", "DISCOVERY")
    conversation_history = context.get("conversationHistory", [])
    
    # 1. QUERY REWRITING: Gera variantes de busca
    search_variants = get_search_variants(query)
    scored_products: List[Dict[str, Any]] = []
    strategy_used = "VECTOR"
    had_fallback = False
    
    # 2. BUSCA EM CASCATA: Tenta cada variante
    for variant_idx, search_variant in enumerate(search_variants):
        if len(scored_products) >= max_results * 1.5:
            break  # Já temos suficientes candidatos
        
        # Tenta busca vetorial (similarity)
        vector_results = await _vector_search(
            search_variant,
            products,
            query_embeddings_cache,
            product_embeddings_cache,
            get_embeddings_func
        )
        
        for prod in vector_results:
            # Evita duplicatas
            if not any(p["id"] == prod["id"] for p in scored_products):
                scored_products.append(prod)
                if variant_idx > 0:
                    strategy_used = "SEMANTIC_EXPAND"
        
        # Se primeira variante retornou resultados, não precisa de fallback
        if variant_idx == 0 and vector_results:
            break
    
    # 3. FALLBACK KEYWORD (se não encontrou com vector)
    if not scored_products:
        keyword_results = _keyword_search(query, products)
        scored_products.extend(keyword_results)
        had_fallback = True
    
    # 4. CONTEXT BONUS: Aumenta score baseado no contexto
    for product in scored_products:
        context_bonus = 0.0
        
        # Penaliza se já foi apresentado
        if product["id"] in presented_product_ids:
            context_bonus -= 0.1
        
        # Bônus por relevância em fase específica
        phase_bonus = _get_phase_bonus(current_phase, product)
        context_bonus += phase_bonus
        
        # Bônus por conversa recente (últimas 5 mensagens)
        recency_bonus = _get_recency_bonus(search_variant, conversation_history)
        context_bonus += recency_bonus
        
        product["contextBonus"] = context_bonus
        product["relevanceScore"] = (product.get("similarityScore", 0.0) + context_bonus)
    
    # 5. RANKING: Ordena por similaridade (similarity-first)
    scored_products.sort(
        key=lambda p: (
            -p.get("relevanceScore", 0.0),  # Relevância (com contexto)
            -float(p.get("price", 0)),       # Preço (mais caro primeiro)
        )
    )
    
    # 6. REMOVE DUPLICATAS e retorna top K
    seen_ids = set()
    final_products = []
    for prod in scored_products:
        if prod["id"] not in seen_ids:
            final_products.append(prod)
            seen_ids.add(prod["id"])
        if len(final_products) >= max_results:
            break
    
    return {
        "products": final_products,
        "strategy": strategy_used,
        "rankingDetails": {
            "originalQuery": query,
            "expandedQuery": search_variants[0] if len(search_variants) > 1 else None,
            "candidatesCount": len(scored_products),
            "minRelevanceThreshold": 0.3,
        },
        "hadFallback": had_fallback
    }


async def _vector_search(
    query: str,
    products: List[Dict[str, Any]],
    query_embeddings_cache: Dict[str, List[float]],
    product_embeddings_cache: Dict[str, List[float]],
    get_embeddings_func
) -> List[Dict[str, Any]]:
    """Busca por similaridade vetorial."""
    if not get_embeddings_func:
        return []
    
    # Obter embedding da query
    query_hash = sha256(query.encode()).hexdigest()
    if query_hash in query_embeddings_cache:
        query_embedding = query_embeddings_cache[query_hash]
    else:
        try:
            query_embedding = await get_embeddings_func([query])
            query_embedding = query_embedding[0] if query_embedding else None
            if query_embedding:
                query_embeddings_cache[query_hash] = query_embedding
        except:
            return []
    
    if not query_embedding:
        return []
    
    scored = []
    for product in products:
        prod_id = str(product.get("id"))
        
        # Obter embedding do produto
        if prod_id in product_embeddings_cache:
            prod_embedding = product_embeddings_cache[prod_id]
        else:
            # Calcula embedding do produto se não tiver
            prod_text = f"{product.get('name', '')} {product.get('description', '')}"
            try:
                prod_embedding = await get_embeddings_func([prod_text])
                prod_embedding = prod_embedding[0] if prod_embedding else None
                if prod_embedding:
                    product_embeddings_cache[prod_id] = prod_embedding
            except:
                continue
        
        if not prod_embedding:
            continue
        
        # Calcula similaridade
        similarity = _cosine_similarity(query_embedding, prod_embedding)
        if similarity > 0.3:  # Threshold mínimo
            scored.append({
                **product,
                "similarityScore": similarity,
                "rankingReason": f"VECTOR:{similarity:.3f}"
            })
    
    # Retorna ordenado por similarity
    return sorted(scored, key=lambda p: -p.get("similarityScore", 0))[:10]


def _keyword_search(query: str, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Busca textual (fallback)."""
    query_lower = query.lower()
    query_words = set(query_lower.split())
    
    scored = []
    for product in products:
        name_lower = (product.get("name") or "").lower()
        desc_lower = (product.get("description") or "").lower()
        combined = f"{name_lower} {desc_lower}"
        
        # Conta quantidade de palavras que match
        matched_words = len(query_words.intersection(set(combined.split())))
        coverage = matched_words / max(1, len(query_words))
        
        if coverage > 0.3:  # Pelo menos 30% das palavras matched
            scored.append({
                **product,
                "similarityScore": min(coverage, 1.0),
                "rankingReason": f"KEYWORD:{coverage:.2f}"
            })
    
    return sorted(scored, key=lambda p: -p.get("similarityScore", 0))[:10]


def _get_phase_bonus(phase: str, product: Dict[str, Any]) -> float:
    """Calcula bônus de relevância por fase de vendas."""
    name_lower = (product.get("name") or "").lower()
    desc_lower = (product.get("description") or "").lower()
    combined = f"{name_lower} {desc_lower}"
    
    if phase == "DISCOVERY":
        # Mais geral, sem preferência específica
        if "recomendado" in combined or "popular" in combined:
            return 0.05
    
    elif phase == "CURATION":
        # Produtos bem-definidos, com informações completas
        if product.get("price") and product.get("image_url"):
            return 0.1  # Bonus por ter dados completos
        if "especial" in combined or "destaque" in combined:
            return 0.08
    
    elif phase == "CUSTOMIZATION":
        # Produtos específicos que cliente escolheu
        # Não dar bonus, manter foco
        return 0.0
    
    elif phase == "CHECKOUT":
        # Manter o produto em foco
        return 0.0
    
    return 0.0


def _get_recency_bonus(search_term: str, conversation_history: List[Dict[str, str]]) -> float:
    """Calcula bônus por menção recente na conversa."""
    if not conversation_history or len(conversation_history) == 0:
        return 0.0
    
    search_lower = search_term.lower()
    bonus = 0.0
    
    # Verifica últimas 5 mensagens
    for msg in conversation_history[-5:]:
        msg_text = (msg.get("content") or "").lower()
        if search_lower in msg_text:
            bonus += 0.05
    
    return min(bonus, 0.15)  # Cap em 0.15


def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calcula similaridade do cosseno entre dois vetores."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return dot_product / (norm1 * norm2)
