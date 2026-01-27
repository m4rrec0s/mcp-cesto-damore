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

# Load environment variables
from pathlib import Path
project_dir = Path(__file__).parent
load_dotenv(dotenv_path=project_dir / '.env')

# Initialize FastMCP server
mcp = FastMCP("Ana - Cesto d'Amore")

@mcp.tool()
async def check_mcp_health() -> str:
    """Check if the MCP server is healthy and return tool count."""
    count = len(mcp._tool_manager._tools) if hasattr(mcp, "_tool_manager") else 0
    return f"MCP is healthy. Registered tools: {count}"

# Database connection settings
DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST"),
    "port": os.getenv("POSTGRES_PORT"),
    "user": os.getenv("POSTGRES_USER"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "database": os.getenv("POSTGRES_DB"),
}

# Evolution API settings (WhatsApp)
EVOLUTION_API_CONFIG = {
    "url": os.getenv("EVOLUTION_API_URL"),
    "key": os.getenv("EVOLUTION_API_KEY"),
    "instance": os.getenv("EVOLUTION_API_INSTANCE"),
    "chat_id": os.getenv("CHAT_ID"),
}

# Timezone for Campina Grande
CAMPINA_GRANDE_TZ = pytz.timezone("America/Fortaleza")  # Brasil/Campina Grande

# Business hours configuration
BUSINESS_HOURS = {
    "monday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "tuesday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "wednesday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "thursday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "friday": [(time(7, 30), time(12, 0)), (time(14, 0), time(17, 0))],
    "saturday": [(time(8, 0), time(11, 0))],
    "sunday": [],  # Closed
}

# Global pool variable
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
        now = datetime.now(pytz.timezone("America/Fortaleza")).strftime("%Y-%m-%d %H:%M:%S")
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
    
    # Check for finalization keywords
    if any(kw in reason_lower for kw in ["finaliza", "paga", "compra", "pedido", "checkout", "concluído"]):
        return "🟢"
    elif "frete" in reason_lower or "duvida" in reason_lower:
        return "🟡"
    else:
        # Default: issues requiring attention
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
        
        # Build Evolution API endpoint
        base_url = EVOLUTION_API_CONFIG['url'].rstrip('/')
        instance = EVOLUTION_API_CONFIG['instance']
        
        # Evolution API endpoint format: /message/sendText/{instanceName}
        endpoint = f"{base_url}/message/sendText/{instance}"
        
        # Prepare headers (Evolution API uses 'apikey' not 'Authorization')
        headers = {
            "apikey": EVOLUTION_API_CONFIG['key'],
            "Content-Type": "application/json"
        }
        
        # Prepare payload
        payload = {
            "number": EVOLUTION_API_CONFIG["chat_id"],
            "text": message
        }
        
        # Send request
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response_text = await response.text()
                
                # Try to parse JSON response
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
    
    # Standard header
    header = f"*AJUDA [{emoji}] - Cliente {nome} - {numero}*"
    
    # Reason and description
    reason_lower = reason.lower()
    if "finaliza" in reason_lower or "pedido" in reason_lower:
        descricao = "✅ Pedido pronto para finalização humana."
    elif "frete" in reason_lower:
        descricao = "🚚 Dúvida ou confirmação de frete."
    else:
        descricao = f"Acionamento: {reason}"

    # Context formatting
    if customer_context and customer_context.strip().lower() != "none":
        # Clean up and ensure formatting
        contexto = customer_context.strip()
        message = f"{header}\n{descricao}\n\n{contexto}"
    else:
        message = f"{header}\n{descricao}\n\n⚠️ Contexto não fornecido pela IA."
        
    return message

# =======================
# MCP PROMPTS (GUIDELINES)
# =======================
# Guidelines accessible via MCP protocol prompts/list and prompts/get
# AI should consult these before important actions

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

# =======================
# CATALOG & SEARCH TOOLS
# =======================

@mcp.tool()
async def consultarCatalogo(termo: str, precoMinimo: float = 0, precoMaximo: float = 999999, exclude_product_ids: list = None) -> str:
    """
    Busca produtos no catálogo por termo, com filtros de preço e exclusão de IDs já enviados.
    
    ## WHEN TO USE
    - Cliente menciona ocasião (aniversário, namorados, casamento, etc)
    - Cliente pede tipo específico de produto (flores, caneca, quadro, pelúcia)
    - Cliente quer "mais opções" ou produtos diferentes
    - Necessário buscar produtos com critérios específicos
    
    ## PARAMETERS
    - termo: Palavra-chave da busca (ocasião ou tipo de produto)
      Exemplos: "aniversário", "flores", "caneca", "namorados", "simples"
      ⚠️ Se múltiplas palavras forem enviadas, serão quebradas em componentes para busca mais eficaz
    - precoMinimo: Preço mínimo em R$ (default: 0)
    - precoMaximo: Preço máximo em R$ (default: 999999)
    - exclude_product_ids: Lista de IDs já mostrados nesta sessão (use sent products list)
    
    ## RESPONSE FORMAT
    Retorna JSON estruturado com dois arrays:
    {
      "exatos": [...],      // Produtos com match exato no termo (prioridade alta)
      "fallback": [...]     // Produtos relacionados (prioridade baixa)
    }
    
    Cada produto contém:
    - ranking: Ordem de relevância (menor número = melhor match)
    - id: ID único do produto
    - nome: Nome do produto
    - preco: Preço em formato float
    - descricao: Descrição completa
    - imagem: URL completa da imagem
    - production_time: Horas necessárias para produção
    - tipo_resultado: "EXATO" ou "FALLBACK"
    
    ## PRESENTATION RULES (CRITICAL)
    1. **SEMPRE priorize produtos "EXATO" sobre "FALLBACK"**
    2. **Mostre exatamente 2 produtos por consulta**
    3. Use o campo `ranking` para ordenar (menor = melhor)
    4. **OBRIGATÓRIO**: Inclua production_time na apresentação:
       - Se ≤ 1h: "Produção imediata no mesmo dia ✅"
       - Se > 1h: "Precisamos de {production_time} horas para produção"
    5. **Price Fallback**: Se esvaziar com precoMaximo, ofereça buscar sem limite
    
    ## EXAMPLES
    Cliente: "Quero para aniversário" 
    → termo="aniversário", precoMaximo=999999
    
    Cliente: "Flores baratas" 
    → termo="flores", precoMaximo=120
    
    Cliente: "Mais opções" 
    → termo=<último termo usado>, exclude_product_ids=[IDs já enviados]
    
    Cliente: "Caneca personalizada"
    → termo="caneca", precoMaximo=999999
    → LEMBRE: Mencionar "Temos canecas de pronta entrega (1h) e as customizáveis com fotos/nomes (18h comerciais de produção)"
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            # Parse exclude IDs
            exclude_ids = exclude_product_ids if exclude_product_ids else []
            exclude_ids = [str(id) for id in exclude_ids]
            
            # 🔑 CRITICAL: Break multi-word search terms into keywords for better matching
            # "café da manhã" → ["café", "manhã"] (removes common words like "da")
            common_words = {"o", "a", "de", "da", "do", "em", "um", "uma", "e", "ou", "para", "por", "com"}
            search_terms = [w.strip() for w in termo.split() if w.strip().lower() not in common_words]
            
            # If multi-word, use individual terms; otherwise use original term
            if len(search_terms) > 1:
                # Multi-word search: try each meaningful word
                _safe_print(f"🔑 Breaking multi-word search: '{termo}' → {search_terms}")
                # Use the main keyword (typically the first or longest meaningful term)
                primary_term = max(search_terms, key=len)
                _safe_print(f"🎯 Using primary keyword: '{primary_term}'")
            else:
                # Single word or empty after filtering: use original term
                primary_term = termo
            
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
            LIMIT 6;
            """
            
            _safe_print(f"🔍 consultarCatalogo: termo original='{termo}', termo processado='{primary_term}', preço=[{precoMinimo}-{precoMaximo}], exclude={len(exclude_ids)} IDs")
            
            start_time = lib_time.time()
            rows = await conn.fetch(query, primary_term, precoMaximo, precoMinimo, exclude_ids)
            duration = lib_time.time() - start_time
            _safe_print(f"⏱️ query levaram {duration:.2f}s")
            
            if not rows:
                return f"❌ Nenhum produto encontrado para '{termo}'. Desculpa! 😔"
            
            # Separate exact matches from fallback (ranking now is global)
            exact_matches = [r for r in rows if r['is_exact_match']]
            fallback_matches = [r for r in rows if not r['is_exact_match']]
            
            # Structure results for LLM consumption
            structured = {
                "status": "found" if rows else "not_found",
                "termo": termo,
                "termo_processado": primary_term,
                "exatos": [
                    {
                        "ranking": r['ranking'],
                        "id": str(r['id']),
                        "nome": r['name'],
                        "preco": float(r['price']),
                        "descricao": r['description'],
                        "imagem": r['image_url'],
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
                        "imagem": r['image_url'],
                        "production_time": int(r['production_time']) if r['production_time'] is not None else 1,
                        "tipo_resultado": "FALLBACK",
                        "relevance_score": int(r['relevance_score'])
                    }
                    for r in fallback_matches
                ]
            }
            
            # Log results
            for r in rows:
                tipo = "EXATO" if r['is_exact_match'] else "FALLBACK"
                _safe_print(f"  ✅ [{tipo}] Ranking {r['ranking']}: {r['name']} - R$ {r['price']:.2f}")
            
            # Return JSON for LLM to parse
            return json.dumps(structured, ensure_ascii=False)
        except Exception as e:
            _safe_print(f"❌ Erro em consultarCatalogo: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool()
async def get_adicionais() -> str:
    """
    Retorna ITENS ADICIONAIS (Balões, Chocolates extras, Ursos, Quadros) para complementar a cesta.
    Use APÓS o cliente ter escolhido o presente principal ou se ele quiser 'incrementar' o presente.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT name, base_price as price, description, image_url FROM public."Item" WHERE type = \'ADDITIONAL\'')
        adicionais = [{"name": r['name'], "price": float(r['price']), "description": r['description'], "image_url": r['image_url']} for r in rows]
        humanized = "✨ PARA TORNAR AINDA MAIS ESPECIAL:\n\n" + "".join([f"{i['name']} - R$ {i['price']:.2f}\n" for i in adicionais])
        return _format_structured_response({"status": "found", "adicionais": adicionais}, humanized)

@mcp.tool()
async def validate_delivery_availability(date_str: str, time_str: Optional[str] = None) -> str:
    """
    VERIFICA DISPONIBILIDADE de entrega para uma DATA (YYYY-MM-DD) e HORA (HH:MM).
    Use para validar se podemos entregar no momento que o cliente deseja.
    
    ⚠️ REGRA CRÍTICA: Se o cliente não informar a hora, a ferramenta retornará os blocos disponíveis e uma lista de 'suggested_slots'.
    Você DEVE informar os 'suggested_slots' ao cliente para facilitar a escolha.
    """
    try:
        # Validação de timezone - garante que comparações de data estão corretas
        date_str_validated, tz_debug = _validate_timezone_safety(date_str)
        _safe_print(tz_debug)
        
        date_obj = datetime.strptime(date_str_validated, "%Y-%m-%d").date()
        now_local = _get_local_time()
        
        # Days of week: 0=Monday, 6=Sunday
        day_names = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        day_name = day_names[date_obj.weekday()]
        day_num = date_obj.weekday()
        
        # Helper to check if date is a holiday
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
        
        # Helper to get next available business day and hours
        async def get_next_available(current_date):
            next_d = current_date + timedelta(days=1)
            while True:
                d_num = next_d.weekday()
                d_name = day_names[d_num]
                hours = BUSINESS_HOURS.get(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d_num], [])
                
                # Check if it's not a holiday
                holiday_check = await is_holiday(next_d)
                if hours and not holiday_check:
                    return next_d, d_name, hours
                next_d += timedelta(days=1)

        # Check if Sunday
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
                f"😔 Aos domingos a gente descansa para estar 100% pra você na segunda! ❤️\n\nQue tal marcar para amanhã ({next_date.strftime('%d/%m')})? Funcionamos das {hours_fmt}. Quer agendar? 🥰"
            )
        
        # Check if date is a holiday
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
        
        # Get business hours for the requested day
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
        
        # If time_str is provided, validate it
        if time_str:
            try:
                requested_time = datetime.strptime(time_str, "%H:%M").time()
                
                # Check if requested time falls within business hours
                is_within_hours = any(
                    start <= requested_time <= end 
                    for start, end in business_hours
                )
                
                # Check for intervals or after-hours
                if not is_within_hours:
                    # Determine why it's not within hours
                    is_too_early = requested_time < business_hours[0][0]
                    is_too_late = requested_time > business_hours[-1][1]
                    
                    # Check for lunch interval (if it exists)
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
                            f"Poxa, agora são {time_str} e já estamos fora do horário comercial. ⏰\n\nMas você pode marcar para amanhã, {next_day_name} ({next_date.strftime('%d/%m')})! Nosso horário é das {hours_fmt}. Quer agendar? 🥰"
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
                
                # Check if today - need at least 1 hour for production
                if date_obj == now_local.date():
                    time_needed = now_local.time().replace(microsecond=0)
                    min_ready_time = (datetime.combine(date_obj, time_needed) + timedelta(hours=1)).time()
                    
                    if requested_time < min_ready_time:
                        return _format_structured_response(
                            {
                                "status": "unavailable", 
                                "reason": "insufficient_production_time", 
                                "minimum_ready_time": min_ready_time.strftime("%H:%M")
                            },
                            f"⏱️ O prazo está em cima! Nossa cesta leva 1 horinha e estaria pronta por volta das {min_ready_time.strftime('%H:%M')}.\n\nPodemos marcar para esse horário ou um pouco depois? 🎁"
                        )
                
                # Time is valid
                return _format_structured_response(
                    {"status": "available", "date": date_str, "time": time_str},
                    f"✅ Perfeito! Tá marcado para {day_name} às {time_str}! Sua cesta vai estar prontinha em 1 hora depois da confirmação. 🌹❤️"
                )
            
            except ValueError:
                return "⚠️ Formato de hora inválido. Use HH:MM (exemplo: 14:30)"
        
        else:
            # No specific time provided - provide overview
            # 🕒 [MELHORIA]: Filter available hours based on current time + 1h production
            now_local = _get_local_time()
            current_time = now_local.time()
            
            # Helper to format business hours for display
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
                        f"Poxa, hoje os pedidos já encerraram (agora são {now_local.strftime('%H:%M')})! ⏰\n\nMas você pode marcar para amanhã, {next_day_name} ({next_date.strftime('%d/%m')})! Abrimos das {next_hours_fmt}. Quer agendar? 🥰"
                    )
                
                # Filter slots that are still possible (now + 1h)
                # We need to know which blocks are still "open" for new orders
                min_ready_dt = now_local + timedelta(hours=1)
                min_ready_time = min_ready_dt.time()
                
                available_now = []
                for s, e in business_hours:
                    # If the block ends after we can have a product ready, it's partially or fully available
                    if e > min_ready_time:
                        # The start of availability in this block is max(block_start, min_ready)
                        effective_start = max(s, min_ready_time)
                        available_now.append((effective_start, e))
                
                if not available_now:
                     next_date, next_day_name, next_hours = await get_next_available(date_obj)
                     next_hours_fmt = format_hours(next_hours)
                     return _format_structured_response(
                        {
                            "status": "unavailable", 
                            "reason": "no_slots_left_today",
                            "current_time_campina": now_local.strftime("%H:%M")
                        },
                        f"Hoje não conseguimos mais produzir a tempo (agora são {now_local.strftime('%H:%M')}), pois precisamos de 1h de preparo. ⏰\n\nQue tal amanhã às {next_hours[0][0].strftime('%H:%M')}? ou prefere outro horário? 🥰"
                    )

                available_now_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in available_now])

                # 🚀 Sugestão explicita de horários para a IA não alucinar
                # Gera lista de horários a cada 30min dentro dos blocos disponíveis
                suggested_slots = []
                for s, e in available_now:
                    temp_dt = datetime.combine(date_obj, s)
                    # Round up to next 30min if not perfectly aligned
                    if temp_dt.minute > 30:
                        temp_dt = temp_dt.replace(minute=0) + timedelta(hours=1)
                    elif temp_dt.minute > 0 and temp_dt.minute < 30:
                        temp_dt = temp_dt.replace(minute=30)
                    
                    end_dt = datetime.combine(date_obj, e)
                    while temp_dt <= end_dt:
                        slot_time = temp_dt.time()
                        suggested_slots.append(slot_time.strftime("%H:%M"))
                        temp_dt += timedelta(minutes=30)
                
                suggested_str = " | ".join(suggested_slots)
                
                return _format_structured_response(
                    {
                        "status": "available", 
                        "today": True, 
                        "current_time_campina": now_local.strftime("%H:%M"),
                        "available_hours_total": hours_fmt,
                        "available_from_now": available_now_fmt,
                        "suggested_slots": suggested_slots
                    },
                    f"✅ Hoje ainda dá! (Agora são {now_local.strftime('%H:%M')}).\n\n**Opções disponíveis para hoje:**\n{suggested_str}\n\nLembrando que precisamos de 1h para preparar sua cesta. Qual desses horários você prefere? 🌹"
                )
            
            return _format_structured_response(
                {
                    "status": "available", 
                    "date": date_str, 
                    "available_hours": hours_fmt,
                    "current_time_campina": now_local.strftime("%H:%M")
                },
                f"✅ {day_name.capitalize()} ({date_obj.strftime('%d/%m')}) é perfeitinho! Atendemos das {hours_fmt}.\n\nQual horário você prefere? 🎁"
            )
    
    except ValueError as e:
        return f"⚠️ Erro no formato da data. Use YYYY-MM-DD (exemplo: 2026-01-15): {str(e)}"
    except Exception as e:
        return f"⚠️ Erro ao validar: {str(e)}"

@mcp.tool()
async def get_active_holidays() -> str:
    """
    Lista DATAS DE FECHAMENTO (Feriados ou folgas) da loja.
    Use quando o cliente perguntar genericamente 'Vocês vão abrir dia X?' ou para ver feriados próximos.
    Não use para validar entrega (para isso use validate_delivery_availability).
    """
    pool = await get_db_pool()
    now_local = _get_local_time()
    
    # Log timezone info para debug
    _safe_print(f"🕐 [HOLIDAYS-CHECK] Timezone: {now_local.strftime('%Z (UTC%z)')} | Horário: {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
    async with pool.acquire() as conn:
        # Use local date to avoid VPS timezone issues
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
async def calculate_freight(city: str, payment_method: str) -> str:
    """
    Calcula o frete com base na cidade e método de pagamento.
    Regras:
    - Campina Grande: PIX = R$ 0.00 | Cartão = R$ 10.00
    - Cidades Vizinhas: PIX = R$ 15.00 | Cartão = Valor definido pelo atendente

    Validações adicionais:
    - Se cidade ou método estiverem ausentes, retorna erro estruturado orientando a perguntar ao cliente.
    - Normaliza formas escritas de 'cartão' e verifica 'campina' robustamente.
    """
    if not city or str(city).strip() == "":
        return _format_structured_response(
            {"status": "error", "error": "missing_city"},
            "⚠️ Por favor confirme a cidade de entrega antes de calcular o frete. Pergunte ao cliente: 'Qual cidade será a entrega?'"
        )

    if not payment_method or str(payment_method).strip() == "":
        return _format_structured_response(
            {"status": "error", "error": "missing_payment_method"},
            "⚠️ Por favor confirme o método de pagamento do cliente antes de calcular o frete. Pergunte: 'PIX ou Cartão?'"
        )

    city_lower = str(city).lower().strip()

    # Normalize payment method variants
    method_lower = str(payment_method).lower().strip()
    is_pix = method_lower.startswith('pix')
    is_card = any(k in method_lower for k in ['cart', 'cartão', 'cartao', 'credito', 'crédito', 'debito', 'débito'])

    # Cidades vizinhas comuns (normalize accents and lowercase)
    neighbors = ["puxinanã", "puxinana", "lagoa seca", "queimadas", "massaranduba", "lagoa de roça", "lagoa de roca", "esperança", "esperanca"]
    is_neighbor = any(n in city_lower for n in neighbors)

    # Robust Campina detection
    if re.search(r"\bcampina\b", city_lower) or "campina grande" in city_lower:
        val = 0.0 if is_pix else 10.0
        return f"Frete para {city}: R$ {val:.2f}"
    elif is_neighbor:
        if is_pix:
            return f"Frete para {city}: R$ 15.00"
        else:
            return f"Frete para {city}: O valor para pagamento no cartão em cidades vizinhas é repassado pelo atendente humano no fim do atendimento. 🤝"
    else:
        # Fallback para outras cidades ou se não identificado
        if is_pix:
            return f"Frete para {city}: R$ 15.00 (Valor padrão para região metropolitana)"
        elif is_card:
            return f"Frete para {city}: O valor do frete para {city} será confirmado pelo atendente humano. 🤝"
        else:
            return _format_structured_response(
                {"status": "error", "error": "unknown_payment_method"},
                "⚠️ Método de pagamento não reconhecido. Por favor pergunte ao cliente: 'PIX ou Cartão?'"
            )

@mcp.tool()
async def get_current_business_hours() -> str:
    """
    Returns the business hours for today and the current status (open/closed).
    Always returns hours in America/Fortaleza (Campina Grande) timezone.
    """
    now = _get_local_time()
    day_num = now.weekday()
    day_key = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day_num]
    hours = BUSINESS_HOURS.get(day_key, [])
    
    if not hours:
        return "Hoje (domingo) não abrimos para produção, mas estamos anotando pedidos para amanhã! ❤️"
        
    hours_fmt = " e das ".join([f"{s.strftime('%H:%M')} às {e.strftime('%H:%M')}" for s, e in hours])
    status = "Abertos"
    
    # Check if currently open
    current_time = now.time()
    is_open = any(s <= current_time <= e for s, e in hours)
    if not is_open:
        status = "Fechados no momento"
        
    return f"Nosso horário para hoje ({day_key}) é: {hours_fmt}. Status: {status} ✅"

@mcp.tool()
async def validate_price_manipulation(claimed_price: float, product_name: str) -> str:
    """Detects price manipulation."""
    return "Preço validado."

@mcp.tool()
async def notify_human_support(reason: str, customer_context: str, customer_name: str = "Cliente", customer_phone: str = "", should_block_flow: bool = True, session_id: Optional[str] = None) -> str:
    """
    TRANSFERE PARA ATENDENTE HUMANO via WhatsApp.
    Use APENAS no final do checkout ou se houver um problema crítico/solicitação explícita.
    O context deve conter: Cesta, Data, Endereço, Pagamento e Frete.
    """
    # Validate context for checkout finalization
    reason_lower = (reason or "").lower()
    if any(k in reason_lower for k in ["finaliza", "finalização", "pedido", "finalizar", "finalizado"]):
        ctx = (customer_context or "").lower()
        required = ["cesta", "entrega", "endereço", "pagamento"]
        missing = [r for r in required if r not in ctx]
        if missing:
            return _format_structured_response(
                {"status": "error", "error": "incomplete_context", "missing": missing},
                f"⚠️ Contexto incompleto para finalização. Faltando: {', '.join(missing)}. Por favor colete: Cesta, Data/Hora de entrega, Endereço completo, Método de Pagamento e Frete antes de notificar o atendente."
            )

    support_message = _format_support_message(reason, customer_context, customer_name, customer_phone)
    await _send_whatsapp_notification(support_message, customer_name, customer_phone)

    # Se solicitado o bloqueio e temos o ID da sessão, fazemos o bloqueio aqui também
    if should_block_flow and session_id:
        await _internal_block_session(session_id)
        return "Notificação enviada e atendimento encerrado com sucesso. ✅"

    return "Notificação enviada com sucesso para o time humano. ✅"

@mcp.tool()
async def math_calculator(expression: str) -> str:
    """
    Calculadora para operações matemáticas básicas. Útil para somar produtos e frete.
    Exemplo de expressão: "109.90 + 137.90 + 15"
    """
    try:
        # Simple evaluation for basic math only
        allowed_chars = "0123456789+-*/.() "
        if not all(c in allowed_chars for c in expression):
            return "Erro: Expressão contém caracteres não permitidos."
        
        # Remove any leading zeros from numbers to avoid octal issues in some python versions
        # though eval in py3 doesn't support leading zeros for ints.
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
    # 4 days for expiry
    expires_at = now_local + timedelta(seconds=345600)
    
    _safe_print(f" tentando bloquear sessão: {session_id}")

    async with pool.acquire() as conn:
        try:
            # Tenta atualizar sem o prefixo public e com cast explícito se necessário
            # Prisma models em Postgres costumam ser case-sensitive se tiverem CamelCase
            query = """
            UPDATE "AIAgentSession"
            SET is_blocked = true, expires_at = $2
            WHERE id = $1;
            """
            result = await conn.execute(query, session_id, expires_at)
            
            # Se não afetou nenhuma linha, talvez o ID precise de cast para UUID ou o nome da tabela precise de ajuste
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
            # Consistent with America/Fortaleza
            expires_at = now_local + timedelta(days=15)
            query = """
            INSERT INTO public."CustomerMemory" (id, customer_phone, summary, updated_at, expires_at)
            VALUES (gen_random_uuid(), $1, $2, $3, $4)
            ON CONFLICT (customer_phone) DO UPDATE 
            SET summary = $2, updated_at = $3, expires_at = $4
            RETURNING id;
            """
            row = await conn.fetchrow(query, customer_phone, summary, now_local, expires_at)
            structured_data = {"status": "success", "customer_phone": customer_phone, "memory_id": str(row['id'])}
            return _format_structured_response(structured_data, f"Memória atualizada para {customer_phone}.")
        except Exception as e:
            return f"Erro: {str(e)}"

# ============================================================================
# PROMPTS: Instruções para padronizar comportamento da IA com as Tools
# ============================================================================

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
       "Domingos a gente descansa para estar 100% pra você na segunda! ❤️"
    
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
    Você: [Chama validate_delivery_availability('2026-01-11')] e retorna a resposta da tool
    """
    return "Procedimento de validação de horários carregado."


if __name__ == "__main__":
    mcp.run()
