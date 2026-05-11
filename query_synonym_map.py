"""
Mapa de sinônimos para expansão de queries de busca de produtos.
Usado para melhorar taxa de acerto quando cliente usa diferentes termos.
"""

from typing import Set, List, Dict

# Mapa de sinônimos: termo principal → variações/sinônimos
SYNONYM_MAP: Dict[str, Set[str]] = {
    # Flores e buquês
    "buque": {"buquê", "buquet", "flores", "ramo", "arranjo", "rosa", "rosas"},
    "buquê": {"buque", "buquet", "flores", "ramo", "arranjo", "rosa", "rosas"},
    "flores": {"flor", "buque", "buquê", "ramo", "arranjo", "rosas"},
    "rosa": {"rosas", "flor", "flores", "buque", "buquê"},
    "rosas": {"rosa", "flor", "flores", "buque", "buquê"},
    "arranjo": {"arranjos", "flores", "buque", "buquê", "ramo"},
    
    # Quadros e fotos
    "quadro": {"quadros", "moldura", "molduras", "foto", "fotos", "polaroide", "polaroides", "instax", "picture"},
    "moldura": {"molduras", "quadro", "quadros", "foto", "fotos"},
    "foto": {"fotos", "fotografia", "fotografias", "quadro", "quadros", "moldura", "polaroide", "instax"},
    "fotografia": {"fotografias", "foto", "fotos", "quadro", "moldura"},
    "polaroide": {"polaroides", "instax", "foto", "fotos", "quadro"},
    "instax": {"instax", "polaroide", "polaroides", "foto", "fotos"},
    
    # Canecas
    "caneca": {"canecas", "mug", "mugcup", "xícara", "xícaras"},
    "xícara": {"xícaras", "caneca", "canecas", "mug"},
    
    # Pelúcias
    "pelucia": {"pelúcia", "pelúcias", "pelucias", "urso", "ursos", "ursinho", "ursinho", "boneco", "bonecos", "brinquedo"},
    "pelúcia": {"pelucia", "pelúcias", "pelucias", "urso", "ursos", "ursinho", "boneco"},
    "urso": {"ursos", "ursinho", "pelucia", "pelúcia", "boneco", "bonecos"},
    "ursinho": {"urso", "ursos", "pelucia", "pelúcia", "boneco"},
    
    # Quebra-cabeças
    "quebra-cabeca": {"quebracabeca", "quebra cabeca", "puzzle", "jogo", "desafio"},
    "quebracabeca": {"quebra-cabeca", "quebra cabeca", "puzzle", "jogo"},
    "puzzle": {"puzzles", "quebra-cabeca", "jogo", "desafio"},
    
    # Bebidas e drinks
    "drink": {"drinks", "bebida", "bebidas", "coquetel", "cocktail", "coquetél"},
    "coquetel": {"coqueteis", "drinks", "bebida", "bebidas"},
    "bebida": {"bebidas", "drink", "drinks", "coquetel", "cerveja", "vinho"},
    "cerveja": {"cervejas", "bebida", "bebidas", "drink"},
    "vinho": {"vinhos", "bebida", "bebidas"},
    
    # Cestas e presentes
    "cesta": {"cestas", "cesto", "cestos", "presente", "presentes", "kit", "kits", "combo", "combos"},
    "cesto": {"cestos", "cesta", "cestas", "presente"},
    "presente": {"presentes", "cesta", "cesto", "kit", "combo", "regalo", "regalos"},
    "kit": {"kits", "combo", "combos", "cesta", "cesto", "presente"},
    "combo": {"combos", "kit", "kits", "cesta", "cesto", "pacote"},
    
    # Contexto de ocasiões
    "dia das mães": {"mãe", "mae", "mamãe", "mama", "presente mãe", "presente mae"},
    "mãe": {"mae", "mamãe", "mama", "dia das mães", "presente mãe"},
    "mae": {"mãe", "mamãe", "mama", "dia das mães", "presente mae"},
    "mamãe": {"mama", "mae", "mãe", "dia das mães"},
    "namoro": {"namorado", "namorada", "casal", "romance", "amor"},
    "namorado": {"namoro", "casal", "romance"},
    "namorada": {"namoro", "casal", "romance"},
    "casal": {"namoro", "namorado", "romance", "amor", "presente casal"},
    "aniversario": {"aniversário", "cumpleaños", "niver", "presente aniversário"},
    "aniversário": {"aniversario", "niver", "cumpleaños", "presente"},
    "casamento": {"casamentos", "noivos", "noiva", "noivo", "presente casamento"},
    "natal": {"natalino", "natalina", "presente natal", "decoração natal"},
    "páscoa": {"pascoa", "ovos", "presente páscoa"},
    
    # Termos genéricos
    "presente": {"presentes", "regalo", "regalos", "gift", "gifts", "lembrança", "lembranças"},
    "regalo": {"regalos", "presente", "presentes", "gift"},
    "criança": {"crianças", "criancas", "kid", "kids", "infantil", "bebe", "bebê", "bebês", "filho", "filha"},
    "criancas": {"criança", "kid", "kids", "infantil"},
    "infantil": {"criança", "crianças", "kids", "bebe", "bebê"},
    "bebê": {"bebe", "bebes", "bebês", "infantil", "criança"},
    "lindo": {"lindos", "linda", "lindas", "bonito", "bonita", "bonitos", "bonitas"},
    "bonito": {"bonitos", "bonita", "bonitas", "lindo", "linda"},
    "especial": {"especiais", "personalizado", "personalizada", "customizado", "custom"},
    
    # Termos de tamanho/quantidade
    "pequeno": {"pequena", "pequenos", "pequenas", "mini", "miniaturas", "médio"},
    "grande": {"grandes", "grande", "gigante", "enorme"},
    "médio": {"médios", "media", "médias", "medio"},
    
    # Preço e valor
    "caro": {"caros", "cara", "caras", "premium"},
    "barato": {"baratos", "barata", "baratas", "econômico", "economico"},
    "acessivel": {"acessível", "acessiveis", "acessíveis"},
    "premium": {"premiums", "caro", "caros", "luxo", "de luxo"},
    "luxo": {"de luxo", "premium", "fino", "elegante", "refinado"},
    
    # Entrega e prazos
    "entrega": {"entregas", "envio", "envios", "frete", "prazo", "prazos"},
    "frete": {"fretes", "entrega", "entregas", "envio", "valor envio"},
    "prazo": {"prazos", "entrega", "entregas", "prazo de entrega"},
    "rápido": {"rápidos", "rapido", "rapidos", "rápida", "rapida", "expedito", "urgente"},
    "hoje": {"agora", "já", "imediato"},
    "amanha": {"amanhã", "próximo dia", "próxima", "outro dia"},
    "amanhã": {"amanha", "próximo dia", "outro dia", "dia seguinte"},
}

def expand_query_with_synonyms(query: str, max_expansions: int = 5) -> str:
    """
    Expande uma query com sinônimos para melhorar recuperação de produtos.
    
    Exemplo:
        "buquê de rosa" → "buquê buque flores ramo arranjo rosa rosas"
    
    Args:
        query: Texto original da busca
        max_expansions: Máximo de sinônimos para adicionar
    
    Returns:
        Query expandida com sinônimos relevantes
    """
    if not query or not isinstance(query, str):
        return query
    
    normalized = query.lower().strip()
    terms = normalized.split()
    
    expanded_terms: Set[str] = set(terms)
    
    # Para cada termo, adiciona sinônimos
    for term in terms:
        # Busca exata
        if term in SYNONYM_MAP:
            synonyms = SYNONYM_MAP[term]
            expanded_terms.update(list(synonyms)[:max_expansions])
        
        # Busca com prefixo (para plurais, etc)
        for key, synonyms in SYNONYM_MAP.items():
            if term.startswith(key) or key.startswith(term):
                expanded_terms.update(list(synonyms)[:max_expansions // 2])
    
    # Retorna em ordem: termo original + sinônimos
    result = [term for term in terms] + [s for s in expanded_terms if s not in terms]
    return " ".join(result[:max_expansions + len(terms)])


def get_search_variants(query: str) -> List[str]:
    """
    Gera múltiplas variantes de uma query para busca em cascata.
    
    Retorna:
        Lista de queries em ordem de prioridade (original, expandida, genérica)
    
    Exemplo:
        "buquê rosa" → [
            "buquê rosa",                    # Original
            "buquê rosa flores ramo",        # Expandida
            "buque rosas arranjo",           # Sinônimos puros
            "flores"                          # Fallback genérico
        ]
    """
    if not query or not isinstance(query, str):
        return [query] if query else [""]
    
    variants = [query]  # Original sempre primeiro
    
    # Variante expandida
    expanded = expand_query_with_synonyms(query)
    if expanded != query:
        variants.append(expanded)
    
    # Variante com sinônimos puros (sem original)
    terms = query.lower().split()
    pure_synonyms = []
    for term in terms:
        if term in SYNONYM_MAP:
            pure_synonyms.extend(list(SYNONYM_MAP[term])[:2])
    if pure_synonyms:
        variants.append(" ".join(pure_synonyms[:5]))
    
    # Fallback genérico: primeira palavra significativa
    significant_terms = [t for t in terms if len(t) > 3 and t not in {"para", "com", "sem", "uma", "um", "dos", "das"}]
    if significant_terms:
        variants.append(significant_terms[0])
    
    return variants[:4]  # Máximo 4 variantes


def get_related_products_keywords(product_name: str, product_description: str = "") -> Set[str]:
    """
    Extrai keywords relacionados de um produto para indexação.
    
    Usado para melhorar matching quando cliente busca por sinônimos.
    """
    combined = f"{product_name} {product_description}".lower()
    related = set()
    
    for key, synonyms in SYNONYM_MAP.items():
        if key in combined:
            related.add(key)
            related.update(synonyms)
    
    return related


# Teste rápido
if __name__ == "__main__":
    test_queries = [
        "buquê rosa",
        "quadro foto",
        "caneca personalizada",
        "presente namorada",
        "flores para mãe",
    ]
    
    for query in test_queries:
        print(f"\n📝 Query: '{query}'")
        variants = get_search_variants(query)
        for i, variant in enumerate(variants, 1):
            print(f"   {i}. {variant}")
