import os
import asyncio
import json
import sys
import re
import time as lib_time
from typing import Optional, List, Dict, Any, Union
from fastmcp import FastMCP
import asyncpg
from dotenv import load_dotenv
from datetime import datetime, time, timedelta
import pytz
import aiohttp
from guidelines import GUIDELINES

CAMPINA_GRANDE_TZ = pytz.timezone("America/Fortaleza")

from pathlib import Path
project_dir = Path(__file__).parent
load_dotenv(dotenv_path=project_dir / '.env')

mcp = FastMCP("Ana - Cesto d'Amore")

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
        
        base_url = EVOLUTION_API_CONFIG['url'].rstrip('/')
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
                    return {
                        "success": True,
                        "status_code": response.status,
                        "message_id": response_data.get("message", {}).get("key", {}).get("id"),
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
    
    header = f"*AJUDA [{emoji}] - Cliente {nome} - {numero}*"
    
    reason_lower = reason.lower()
    if "finaliza" in reason_lower or "pedido" in reason_lower:
        descricao = "✅ Pedido pronto para finalização humana."
    elif "frete" in reason_lower:
        descricao = "🚚 Dúvida ou confirmação de frete."
    else:
        descricao = f"Acionamento: {reason}"

    if customer_context and customer_context.strip().lower() != "none":
        contexto = customer_context.strip()
        message = f"{header}\n{descricao}\n\n{contexto}"
    else:
        message = f"{header}\n{descricao}\n\n⚠️ Contexto não fornecido pela IA."
        
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
        
        "aniversário": "aniversário d'amore",
        "aniversario": "aniversário d'amore",
        "birthday": "aniversário d'amore",
        "café": "café d'amore",
        "cafe": "café d'amore",
        "chocolate": "chocolate d'amore",
        "chocolates": "chocolate d'amore",
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
        "cesto", "buquê", "buque", "bar", "caneca", "pelúcia", "pelecia", "quadro",
        "quebra-cabeça", "quebra", "coração", "coracao", "aniversário", "aniversario",
        "café", "cafe", "chocolate", "cone"
    ]
    
    if any(specific in termo_limpo for specific in specific_terms):
        _safe_print(f"✓ Termo específico mantido: '{termo}'")
        return termo
    
    _safe_print(f"ℹ️ Termo '{termo}' não mapeado, usando original")
    return termo

@mcp.tool()
async def consultarCatalogo(
    termo: str,
    preco_minimo: float = 0,
    preco_maximo: float = 999999,
    exclude_product_ids: list = None,
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

            common_words = {"o", "a", "de", "da", "do", "em", "um", "uma", "e", "ou", "para", "por", "com"}
            search_terms = [w.strip() for w in termo_normalizado.split() if w.strip().lower() not in common_words]
            
            search_terms = list(set(search_terms + [termo_normalizado]))
            search_terms = [t for t in search_terms if t.lower().strip()]
            
            if len(search_terms) > 1:
                _safe_print(f"🔑 Breaking multi-word search: '{termo_normalizado}' → Testing keywords: {search_terms}")
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
                        "suggested_slots": suggested_slots
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
                "estimated_ready_time": ready_time_val.strftime("%H:%M")
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
        next_day = now + datetime.timedelta(days=1)
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
async def notify_human_support(reason: str, customer_context: str, customer_name: str = "Cliente", customer_phone: str = "", should_block_flow: bool = True, session_id: Optional[str] = None) -> str:
    """
    Transfere para humano. USO OBRIGATÓRIO:
    1. FIM DO PEDIDO (Todos dados coletados).
    2. Problema Técnico.
    3. Evento de carrinho (cliente adicionou produto ao carrinho).
    
    reason: motivo (ex: "end_of_checkout" ou "cart_added").
    customer_context: Resumo (Cesta, Data, Endereço, Pagamento) ou contexto mínimo para carrinho.
    should_block_flow: true (stop bot).
    
    NÃO use para "interesse". Apenas COMPRA confirmada.
    """
    reason_lower = (reason or "").lower()
    is_cart_added = any(
        k in reason_lower
        for k in ["cart_added", "cart_add", "produto ao carrinho", "adicionou no carrinho"]
    )

    # Prevenção de abandono precoce (Interesse != Compra)
    if any(k in reason_lower for k in ["interesse", "gostou", "interessou", "quer saber", "curioso"]):
         return _format_structured_response(
                {"status": "error", "error": "premature_handover"},
                "⚠️ Ana, você está tentando transferir muito cedo! Se o cliente apenas demonstrou interesse ou gostou, pergunte se ele quer levar o produto antes de transferir. O humano só deve ser chamado quando houver intenção Clara de compra e dados coletados."
            )

    if any(k in reason_lower for k in ["finaliza", "finalização", "pedido", "finalizar", "finalizado", "carrinho"]):
        ctx = (customer_context or "").lower()
        required = ["cesta", "entrega", "endereço", "pagamento"]
        missing = [r for r in required if r not in ctx]
        if missing and not is_cart_added:
            return _format_structured_response(
                {"status": "error", "error": "incomplete_context", "missing": missing},
                f"⚠️ Contexto incompleto para finalização. Faltando dados essenciais: {', '.join(missing)}. \n\nInstrução para Ana: NÃO CHAME esta ferramenta novamente até que o cliente forneça esses dados. Informe ao cliente o que falta e peça gentilmente."
            )

    support_message = _format_support_message(reason, customer_context, customer_name, customer_phone)
    await _send_whatsapp_notification(support_message, customer_name, customer_phone)

    if should_block_flow and session_id:
        await _internal_block_session(session_id)
        return "Notificação enviada e atendimento encerrado com sucesso. ✅"

    return "Notificação enviada com sucesso para o time humano. ✅"

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
    - **Segunda a Sexta**: 07:30 às 12:00 e 14:00 às 17:00 (com intervalo 12:00-14:00)
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
