"""
Sistema de relevance score para produtos.
Combina múltiplos sinais para determinar a relevância de um produto.

Score = (similaridade × 0.5) + (phase_bonus × 0.25) + (historical_bonus × 0.15) + (freshness_bonus × 0.10)
"""

from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta

def calculate_product_relevance_score(
    product: Dict[str, Any],
    query: str,
    similarity_score: float,
    current_phase: str = "DISCOVERY",
    customer_history: Optional[List[Dict[str, Any]]] = None,
    presented_product_ids: Optional[List[str]] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Calcula relevance score composto para um produto.
    
    Args:
        product: Dados do produto
        query: Termo de busca
        similarity_score: Score de similaridade vetorial (0-1)
        current_phase: Fase de vendas (DISCOVERY, CURATION, CUSTOMIZATION, CHECKOUT)
        customer_history: Histórico de compras do cliente
        presented_product_ids: IDs de produtos já apresentados
        conversation_history: Histórico de conversa (últimos turnos)
    
    Returns:
        {
            "product_id": str,
            "relevance_score": float (0-1),
            "breakdown": {
                "similarity": float,
                "phase_bonus": float,
                "historical_bonus": float,
                "freshness_bonus": float,
                "presented_penalty": float
            },
            "reasons": List[str]
        }
    """
    product_id = str(product.get("id", "unknown"))
    name = str(product.get("name", ""))
    description = str(product.get("description", ""))
    price = float(product.get("price") or 0)
    
    presented_product_ids = presented_product_ids or []
    conversation_history = conversation_history or []
    customer_history = customer_history or []
    
    # =========================================================================
    # 1. SIMILARIDADE (peso 0.5)
    # =========================================================================
    similarity = min(float(similarity_score), 1.0)
    
    # =========================================================================
    # 2. PHASE BONUS (peso 0.25)
    # =========================================================================
    phase_bonus = _get_phase_bonus(current_phase, name, description, price)
    
    # =========================================================================
    # 3. HISTORICAL BONUS (peso 0.15)
    # =========================================================================
    historical_bonus = _get_historical_bonus(product_id, name, description, customer_history)
    
    # =========================================================================
    # 4. FRESHNESS BONUS (peso 0.10)
    # =========================================================================
    freshness_bonus = _get_freshness_bonus(query, name, description, conversation_history)
    
    # =========================================================================
    # 5. PRESENTED PENALTY (negativo)
    # =========================================================================
    presented_penalty = 0.0
    if product_id in presented_product_ids:
        presented_penalty = -0.15  # Penaliza repetição
    
    # =========================================================================
    # CÁLCULO FINAL
    # =========================================================================
    weights = {
        "similarity": 0.50,
        "phase_bonus": 0.25,
        "historical_bonus": 0.15,
        "freshness_bonus": 0.10,
    }
    
    final_score = (
        similarity * weights["similarity"] +
        phase_bonus * weights["phase_bonus"] +
        historical_bonus * weights["historical_bonus"] +
        freshness_bonus * weights["freshness_bonus"] +
        presented_penalty
    )
    
    # Normaliza para 0-1
    final_score = max(0.0, min(1.0, final_score))
    
    # =========================================================================
    # RAZÕES E EXPLICAÇÕES
    # =========================================================================
    reasons = []
    
    if similarity > 0.7:
        reasons.append(f"Alta similaridade semântica ({similarity:.2f})")
    elif similarity > 0.5:
        reasons.append(f"Similaridade moderada ({similarity:.2f})")
    
    if phase_bonus > 0.1:
        reasons.append(f"Bom match para fase '{current_phase}' (+{phase_bonus:.2f})")
    
    if historical_bonus > 0.1:
        reasons.append(f"Cliente tem histórico com tipo similar (+{historical_bonus:.2f})")
    
    if freshness_bonus > 0.05:
        reasons.append(f"Menção recente na conversa (+{freshness_bonus:.2f})")
    
    if presented_penalty < 0:
        reasons.append(f"Já foi apresentado anteriormente ({presented_penalty:.2f})")
    
    if not reasons:
        reasons.append(f"Potencial match {final_score:.2%} de relevância")
    
    return {
        "product_id": product_id,
        "relevance_score": final_score,
        "breakdown": {
            "similarity": similarity,
            "phase_bonus": phase_bonus,
            "historical_bonus": historical_bonus,
            "freshness_bonus": freshness_bonus,
            "presented_penalty": presented_penalty,
        },
        "reasons": reasons,
    }


def _get_phase_bonus(
    phase: str,
    product_name: str,
    product_description: str,
    price: float
) -> float:
    """
    Bonus de relevância baseado na fase de vendas.
    
    DISCOVERY (0.0-0.1):
        - Nenhum bonus específico (exploração genérica)
    
    CURATION (0.0-0.3):
        - Produtos com dados completos (imagem, descrição)
        - Produtos em faixa de preço média
    
    CUSTOMIZATION (0.05-0.2):
        - Produtos que podem ser customizados
    
    CHECKOUT (0.1-0.2):
        - Disponibilidade confirmada
        - Preço estável
    """
    combined = f"{product_name} {product_description}".lower()
    
    if phase == "DISCOVERY":
        # Fase exploratória - sem bonus específico
        return 0.0
    
    elif phase == "CURATION":
        # Foco em apresentação e dados completos
        bonus = 0.0
        
        # Bonus por dados completos
        if len(product_description) > 100:
            bonus += 0.15
        
        # Bonus por preço (faixa média)
        if 50 <= price <= 200:
            bonus += 0.10
        
        # Bonus por tipo de produto popular
        if any(kw in combined for kw in ["cesta", "buquê", "flores", "quadro", "presente"]):
            bonus += 0.05
        
        return min(bonus, 0.3)
    
    elif phase == "CUSTOMIZATION":
        # Foco em produtos que permitem customização
        bonus = 0.05
        
        if any(kw in combined for kw in ["personalizado", "custom", "gravado", "imagem"]):
            bonus += 0.15
        
        return min(bonus, 0.2)
    
    elif phase == "CHECKOUT":
        # Última confirmação
        bonus = 0.1
        
        # Bonus por preço razoável (não muito caro)
        if price <= 300:
            bonus += 0.05
        
        # Bonus por disponibilidade (inferred)
        if "disponível" in combined or "pronto" in combined:
            bonus += 0.05
        
        return min(bonus, 0.2)
    
    return 0.0


def _get_historical_bonus(
    product_id: str,
    product_name: str,
    product_description: str,
    customer_history: List[Dict[str, Any]]
) -> float:
    """
    Bonus se cliente tem histórico com produtos similares.
    
    Examina:
    - Compras anteriores do mesmo tipo
    - Preferências de preço
    - Categorias favoritas
    """
    if not customer_history:
        return 0.0
    
    combined = f"{product_name} {product_description}".lower()
    bonus = 0.0
    
    # Categorias do cliente
    client_categories = {}
    for purchase in customer_history:
        category = (purchase.get("category") or "").lower()
        if category:
            client_categories[category] = client_categories.get(category, 0) + 1
    
    # Dá bonus se categoria está no histórico
    for category, count in client_categories.items():
        if category in combined and count >= 2:
            bonus += 0.10
    
    # Bonus se exatamente o mesmo produto
    if product_id in [p.get("id") for p in customer_history]:
        bonus = min(bonus + 0.05, 0.15)  # Repete com moderação
    
    return min(bonus, 0.15)


def _get_freshness_bonus(
    query: str,
    product_name: str,
    product_description: str,
    conversation_history: List[Dict[str, str]]
) -> float:
    """
    Bonus por menção recente na conversa (query expansion match).
    
    Verifica se:
    - Produto foi mencionado recentemente pelo cliente
    - Query é relacionada a menções recentes
    """
    if not conversation_history:
        return 0.0
    
    query_lower = query.lower()
    product_combined = f"{product_name} {product_description}".lower()
    bonus = 0.0
    
    # Verifica últimas 5 mensagens
    recent_messages = conversation_history[-5:]
    for msg in recent_messages:
        msg_text = (msg.get("content") or "").lower()
        
        # Se query mencionou algo que está no histórico recente
        if query_lower in msg_text or msg_text in query_lower:
            bonus += 0.05
        
        # Se produto foi mencionado recentemente
        if product_name.lower() in msg_text:
            bonus += 0.05
    
    return min(bonus, 0.10)


def rank_products_by_relevance(
    products: List[Dict[str, Any]],
    query: str,
    similarity_scores: Dict[str, float],
    current_phase: str = "DISCOVERY",
    customer_history: Optional[List[Dict[str, Any]]] = None,
    presented_product_ids: Optional[List[str]] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """
    Rankeia produtos por relevance score composto.
    
    Returns:
        Lista de produtos ordenada por relevância, com scores e razões
    """
    scored_products = []
    
    for product in products:
        product_id = str(product.get("id", ""))
        similarity = similarity_scores.get(product_id, 0.0)
        
        score_result = calculate_product_relevance_score(
            product=product,
            query=query,
            similarity_score=similarity,
            current_phase=current_phase,
            customer_history=customer_history,
            presented_product_ids=presented_product_ids,
            conversation_history=conversation_history,
        )
        
        scored_products.append({
            **product,
            "relevance_score": score_result["relevance_score"],
            "score_breakdown": score_result["breakdown"],
            "ranking_reasons": score_result["reasons"],
        })
    
    # Ordenar por relevance score (descendente)
    scored_products.sort(key=lambda p: -p.get("relevance_score", 0))
    
    return scored_products[:top_k]
