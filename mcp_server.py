import os
import asyncio
import json
from typing import Optional, List, Dict, Any
from fastmcp import FastMCP
import asyncpg
from dotenv import load_dotenv
from datetime import datetime, time, timedelta
import pytz
import aiohttp
from guidelines import GUIDELINES
import inspect
from functools import wraps

# Load environment variables
load_dotenv()

# Initialize FastMCP server
mcp = FastMCP("Ana - Cesto d'Amore")

# Decorator que registra ferramentas com Pydantic permissivo
def mcp_tool_relaxed():
    """
    Decorator que registra a ferramenta no FastMCP mas com Pydantic configurado
    para ignorar argumentos extras (extra='ignore').
    Isso permite que o n8n injete sessionId, action, chatInput, etc sem erro.
    """
    def decorator(func):
        sig = inspect.signature(func)
        params = sig.parameters
        param_names = list(params.keys())
        
        # Cria wrapper que valida e filtra argumentos
        @wraps(func)
        async def wrapper(**kwargs):
            # Filtra apenas os parâmetros que a função espera
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in param_names}
            return await func(**filtered_kwargs)
        
        # Copia atributos da função original
        wrapper.__doc__ = func.__doc__
        wrapper.__annotations__ = func.__annotations__
        
        # Registra no FastMCP
        return mcp.tool()(wrapper)
    
    return decorator

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

async def get_db_connection():
    """Create a connection to the Postgres database."""
    return await asyncpg.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"]
    )

def _get_local_time():
    """Get current time in Campina Grande timezone."""
    return datetime.now(CAMPINA_GRANDE_TZ)

def _format_structured_response(data: Dict[str, Any], humanized_message: str) -> str:
    """
    Format response with structured JSON + humanized message.
    Helps LLM parse data while keeping human-friendly text.
    """
    response = f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n\n{humanized_message}"
    return response

def _safe_print(message: str) -> None:
    """
    Safe print that handles Unicode errors gracefully.
    Encodes message to avoid UnicodeEncodeError on Windows.
    """
    try:
        print(message.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
    except:
        try:
            print(message.encode('ascii', errors='replace').decode('ascii'))
        except:
            pass

def _get_emoji_for_reason(reason: str) -> str:
    """
    Map support reason to emoji indicator.
    🔴 = Critical (product unavailable, customization, price manipulation)
    🟡 = Medium (freight doubts)
    🟢 = Success (checkout completion)
    """
    reason_lower = reason.lower()
    
    if reason_lower in ["end_of_checkout"]:
        return "🟢"
    elif reason_lower in ["freight_doubt"]:
        return "🟡"
    else:
        # Default: critical issues
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
        
        # Log request details for debugging
        _safe_print(f"\n[WHATSAPP NOTIFICATION DEBUG]")
        _safe_print(f"Endpoint: {endpoint}")
        _safe_print(f"Instance: {instance}")
        _safe_print(f"Chat ID: {EVOLUTION_API_CONFIG['chat_id']}")
        _safe_print(f"Headers: apikey='***', Content-Type='application/json'")
        _safe_print(f"Payload: {payload}")
        _safe_print(f"Message preview: {message[:100]}...")
        
        # Send request
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response_text = await response.text()
                
                _safe_print(f"\n[RESPONSE]")
                _safe_print(f"Status: {response.status}")
                _safe_print(f"Response text: {response_text}")
                
                # Try to parse JSON response
                try:
                    response_data = await response.json()
                except:
                    response_data = {"raw": response_text}
                
                if response.status in [200, 201]:
                    _safe_print(f"[SUCCESS] Mensagem enviada com sucesso!")
                    return {
                        "success": True,
                        "status_code": response.status,
                        "message_id": response_data.get("message", {}).get("key", {}).get("id"),
                        "response": response_data,
                        "endpoint_used": endpoint
                    }
                else:
                    # Extract error message from response
                    error_msg = response_data.get("message", response_data.get("error", f"HTTP {response.status}"))
                    _safe_print(f"[ERROR] Erro ao enviar: {error_msg}")
                    return {
                        "success": False,
                        "status_code": response.status,
                        "error": str(error_msg),
                        "response": response_data,
                        "endpoint_used": endpoint
                    }
    
    except asyncio.TimeoutError:
        _safe_print(f"[ERROR] Timeout ao conectar com Evolution API")
        return {
            "success": False,
            "error": "Timeout",
            "message": "Falha ao conectar com Evolution API (timeout)"
        }
    except aiohttp.ClientError as e:
        _safe_print(f"[ERROR] Erro de conexao: {type(e).__name__}: {str(e)}")
        return {
            "success": False,
            "error": str(type(e).__name__),
            "message": str(e)
        }
    except Exception as e:
        _safe_print(f"[ERROR] Erro inesperado: {type(e).__name__}: {str(e)}")
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
    [DESCRICAO_DA_SITUACAO]
    
    Priority codes:
    🔴 = Urgent/Critical issues
    🟡 = Medium priority
    🟢 = Success/Positive
    """
    emoji = _get_emoji_for_reason(reason)
    
    # Default client info if not provided
    nome = customer_name or "Desconhecido"
    numero = customer_phone or "Sem contato"
    
    # Build description based on reason
    reason_lower = reason.lower()
    
    reason_descriptions = {
        "price_manipulation": "Cliente tentando negociar/reduzir preco",
        "product_unavailable": "Produto solicitado nao esta no catalogo",
        "complex_customization": "Solicitacao de personalizacao complexa",
        "end_of_checkout": "Finalizacao de compra - aguardando confirmacao de pagamento",
        "customer_insistence": "Cliente insistindo apos multiplas recusas",
        "technical_error": "Erro tecnico no sistema",
        "freight_doubt": "Duvida sobre frete e entrega",
    }
    
    descricao = reason_descriptions.get(reason_lower, f"Acionamento: {reason}")
    
    # Add customer context if provided
    if customer_context:
        descricao += f"\n\nContexto: {customer_context}"
    
    # Build final message (no emojis, only ASCII)
    message = f"*AJUDA [{emoji}] - Cliente {nome} - {numero}*\n{descricao}"
    
    return message

@mcp_tool_relaxed()
async def search_guidelines(query: str) -> str:
    """
    Searches the service guidelines and documentation for relevant information based on a query.
    Acts like a simple RAG (Retrieval-Augmented Generation) to find the best documentation snippet.
    Returns structured JSON with matched guidelines.
    """
    import re
    # Stop words to ignore for better relevance
    STOP_WORDS = {"o", "a", "os", "as", "um", "uma", "de", "do", "da", "em", "para", "com", "no", "na", "que", "está", "procurando", "cliente"}
    
    query_clean = query.lower().strip()
    query_terms = [t for t in re.findall(r'\w+', query_clean) if t not in STOP_WORDS and len(t) > 2]
    
    if not query_terms:
        query_terms = re.findall(r'\w+', query_clean)

    results = []
    
    for category, content in GUIDELINES.items():
        score = 0
        content_lower = content.lower()
        cat_lower = category.lower()
        
        if query_clean in cat_lower or cat_lower in query_clean:
            score += 20
        
        match_count = 0
        for term in query_terms:
            if term in cat_lower:
                score += 15
                match_count += 1
            if term in content_lower:
                occurrences = content_lower.count(term)
                score += min(occurrences, 5) * 2 
                match_count += 1
        
        if len(query_terms) > 1 and match_count >= len(query_terms):
            score += 10
            
        if score > 0:
            results.append((score, category, content))
            
    results.sort(key=lambda x: x[0], reverse=True)
    
    if not results:
        return _format_structured_response(
            {"status": "not_found", "available_categories": list(GUIDELINES.keys())},
            f"Não encontrei documentação específica para '{query}'. Disponíveis: {', '.join(GUIDELINES.keys())}"
        )
    
    top_results = results[:2]
    structured_data = {
        "status": "found",
        "query": query,
        "matches": [
            {"category": cat, "relevance_score": score} 
            for score, cat, _ in top_results
        ]
    }
    
    humanized = "Aqui estão as informações mais relevantes encontradas:\n\n"
    for _, cat, text in top_results:
        humanized += f"--- Categoria: {cat} ---\n{text}\n\n"
        
    return _format_structured_response(structured_data, humanized)

@mcp_tool_relaxed()
async def get_service_guideline(category: str) -> str:
    """
    Returns specific customer service guidelines based on a category.
    Available categories: core, inexistent_products, delivery_rules, customization, 
    closing_protocol, location, mass_orders, faq_production, indecision.
    """
    return GUIDELINES.get(category, f"Guidelines for '{category}' not found. Available: {', '.join(GUIDELINES.keys())}")

@mcp_tool_relaxed()
async def search_products(termo: str, preco_minimo: float = 0, preco_maximo: float = 999999) -> str:
    """
    Search for products in the catalog using relevance scoring and business rules.
    Returns structured JSON with product details + humanized message.
    """
    conn = await get_db_connection()
    try:
        query = """
        WITH input_params AS (
          SELECT 
            LOWER($1) as termo,
            $2::float as preco_maximo,
            $3::float as preco_minimo
        ),
        products_scored AS (
          SELECT 
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            COALESCE(
              jsonb_agg(DISTINCT c.name) FILTER (WHERE c.name IS NOT NULL), 
              '[]'::jsonb
            ) AS categories,
            (
              (CASE WHEN p.description ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 20 ELSE 0 END) +
              (CASE WHEN p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 15 ELSE 0 END) +
              (CASE WHEN p.description ILIKE '%quadro%' THEN 5 ELSE 0 END) +
              (CASE WHEN p.description ILIKE '%polaroides%' THEN 5 ELSE 0 END) +
              (CASE WHEN p.description ILIKE '%caneca%' THEN 3 ELSE 0 END) +
              (CASE WHEN p.description ILIKE '%quebra%' THEN 3 ELSE 0 END) +
              (CASE WHEN p.description ILIKE '%pronta_entrega%' OR p.description ILIKE '%hoje%' OR p.description ILIKE '%agora%' THEN 2 ELSE 0 END)
            ) as relevance_score,
            (CASE WHEN p.description ILIKE '%' || (SELECT termo FROM input_params) || '%' OR p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 1 ELSE 0 END) as is_exact_match
          FROM public."Product" p
          LEFT JOIN public."ProductCategory" pc ON pc.product_id = p.id
          LEFT JOIN public."Category" c ON c.id = pc.category_id
          WHERE 
            p.price >= (SELECT preco_minimo FROM input_params)
            AND p.price <= (SELECT preco_maximo FROM input_params)
          GROUP BY p.id, p.name, p.description, p.price, p.image_url
        ),
        exact_matches AS (
          SELECT * FROM products_scored WHERE is_exact_match = 1
        ),
        fallback_matches AS (
          SELECT * FROM products_scored WHERE is_exact_match = 0
          ORDER BY relevance_score DESC, price DESC
          LIMIT 3
        )
        SELECT 
          name, description, price, image_url,
          CASE WHEN is_exact_match = 1 THEN 'EXATO' ELSE 'FALLBACK' END as tipo_resultado
        FROM (
          SELECT * FROM exact_matches
          UNION ALL
          SELECT * FROM fallback_matches
        ) AS combined
        ORDER BY is_exact_match DESC, relevance_score DESC, price DESC;
        """
        rows = await conn.fetch(query, termo, preco_maximo, preco_minimo)
        
        if not rows:
            return _format_structured_response(
                {"status": "not_found", "search_term": termo},
                "Nenhum produto encontrado para os critérios informados."
            )
        
        products = []
        for row in rows:
            products.append({
                "name": row['name'],
                "description": row['description'],
                "price": float(row['price']),
                "image_url": row['image_url'],
                "match_type": row['tipo_resultado']
            })
        
        structured_data = {
            "status": "found",
            "search_term": termo,
            "total_results": len(products),
            "products": products
        }
        
        humanized = ""
        if products[0]['match_type'] == 'FALLBACK':
            humanized = "Não achei opções exatas, mas confira essas que são um sucesso:\n\n"
        
        for prod in products:
            humanized += f"{prod['image_url']}\n"
            humanized += f"{prod['name']} - R$ {prod['price']:.2f}\n"
            humanized += f"{prod['description']}\n\n"
        
        return _format_structured_response(structured_data, humanized)
    finally:
        await conn.close()

@mcp_tool_relaxed()
async def get_adicionais() -> str:
    """
    Fetch all available add-ons (adicionais) from the Item table in the database.
    Returns structured JSON with add-ons + humanized message.
    """
    conn = await get_db_connection()
    try:
        rows = await conn.fetch('SELECT name, base_price as price, description, image_url FROM public."Item" WHERE type = \'ADDITIONAL\'')
        if not rows:
            rows = await conn.fetch('SELECT name, base_price as price, description, image_url FROM public."Item" LIMIT 10')
            
        if not rows:
            return _format_structured_response(
                {"status": "not_found"},
                "Nenhum adicional encontrado no sistema."
            )
        
        adicionais = []
        for row in rows:
            adicionais.append({
                "name": row['name'],
                "price": float(row['price']),
                "description": row['description'],
                "image_url": row['image_url']
            })
        
        structured_data = {
            "status": "found",
            "total_adicionais": len(adicionais),
            "adicionais": adicionais
        }
        
        humanized = "✨ PARA TORNAR AINDA MAIS ESPECIAL:\n\n"
        for item in adicionais:
            humanized += f"{item['image_url']}\n"
            humanized += f"{item['name']} - R$ {item['price']:.2f}\n"
            humanized += f"{item['description']}\n\n"
        
        return _format_structured_response(structured_data, humanized)
    finally:
        await conn.close()

@mcp_tool_relaxed()
async def validate_delivery_availability(date_str: str, time_str: Optional[str] = None) -> str:
    """
    Validates if a delivery is possible on a given date (YYYY-MM-DD) and optional time (HH:MM).
    Returns structured JSON with validation status + humanized message.
    
    Business hours: Mon-Fri 07:30-12:00 & 14:00-17:00, Sat 08:00-11:00.
    Enforces 1h production time minimum.
    Rejects Sundays.
    """
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        now_local = _get_local_time()
        
        # CRITICAL: Use local Campina Grande time
        current_date = now_local.date()
        current_time = now_local.time()
        
        validation_result = {
            "date": date_str,
            "current_time": now_local.strftime("%H:%M"),
            "current_date": current_date.isoformat(),
            "is_valid": False,
            "reason": None,
            "available_windows": None
        }
        
        # 1. SUNDAY CHECK (Absolute rejection)
        if date_obj.weekday() == 6:  # Sunday
            validation_result["reason"] = "sunday_closed"
            return _format_structured_response(
                validation_result,
                "Ops! Aos domingos a gente não funciona 😔 Sou uma assistente virtual, mas segunda-feira nosso time tá entregando normalmente! Quer agendar pra segunda ou outro dia? 💕"
            )
        
        # 2. PAST DATE CHECK
        if date_obj < current_date:
            validation_result["reason"] = "past_date"
            return _format_structured_response(
                validation_result,
                "Essa data já passou! 😅 Escolha uma data futura para sua entrega."
            )
        
        # 3. BUSINESS HOURS SETUP
        day_name = date_obj.strftime("%A").lower()
        day_name_pt = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][date_obj.weekday()]
        
        # Get hours for this day
        windows = BUSINESS_HOURS.get(day_name, [])
        
        if not windows:
            validation_result["reason"] = "no_business_hours"
            validation_result["available_windows"] = []
            return _format_structured_response(
                validation_result,
                f"Eita, {day_name_pt} não temos horário disponível. Nossos dias de funcionamento são seg-sex e sábado!"
            )
        
        # 4. PRODUCTION BUFFER (1 hour minimum)
        is_today = (date_obj == current_date)
        min_delivery_time = None
        
        if is_today:
            min_delivery_time = (now_local + timedelta(hours=1)).time()
        
        # 5. TIME SLOT VALIDATION (if provided)
        target_time = None
        if time_str:
            try:
                target_time = datetime.strptime(time_str, "%H:%M").time()
            except ValueError:
                validation_result["reason"] = "invalid_time_format"
                return _format_structured_response(
                    validation_result,
                    "Formato de horário inválido. Use HH:MM."
                )
            
            # Check if time is in any window
            in_window = any(start <= target_time <= end for start, end in windows)
            
            if not in_window:
                validation_result["available_windows"] = [
                    {"start": w[0].strftime("%H:%M"), "end": w[1].strftime("%H:%M")} 
                    for w in windows
                ]
                validation_result["reason"] = "time_outside_business_hours"
                
                hours_str = ", ".join([f"{w[0].strftime('%H:%M')}-{w[1].strftime('%H:%M')}" for w in windows])
                return _format_structured_response(
                    validation_result,
                    f"Eita, nesse horário não conseguimos realizar a entrega. Nossos horários são {hours_str}. Quer marcar para outro horário dentro desses turnos?"
                )
            
            # Check if time respects 1h production buffer
            if is_today and target_time < min_delivery_time:
                validation_result["available_windows"] = [
                    {"start": w[0].strftime("%H:%M"), "end": w[1].strftime("%H:%M")} 
                    for w in windows
                ]
                validation_result["reason"] = "insufficient_production_time"
                
                return _format_structured_response(
                    validation_result,
                    f"Eita, nesse horário não conseguimos deixar o pedido pronto a tempo (precisamos de pelo menos 1h de preparo). Nosso horário atual é {current_time.strftime('%H:%M')}. Quer marcar para um pouco mais tarde?"
                )
        
        # SUCCESS CASE
        validation_result["is_valid"] = True
        validation_result["available_windows"] = [
            {"start": w[0].strftime("%H:%M"), "end": w[1].strftime("%H:%M")} 
            for w in windows
        ]
        validation_result["reason"] = "valid"
        
        humanized = f"A data {date_str} "
        if time_str:
            humanized += f"às {time_str} "
        humanized += "está disponível! ✅\n"
        
        hours_str = ", ".join([f"{w[0].strftime('%H:%M')}-{w[1].strftime('%H:%M')}" for w in windows])
        humanized += f"Turnos disponíveis: {hours_str}."
        
        if is_today:
            humanized += f"\nObservação: Para hoje, considere que precisamos de 1h para produção."
        
        return _format_structured_response(validation_result, humanized)
        
    except ValueError as e:
        return _format_structured_response(
            {"error": str(e)},
            f"Erro na validação: {str(e)}. Use o formato YYYY-MM-DD para data."
        )

@mcp_tool_relaxed()
async def calculate_freight(city: str, payment_method: str) -> str:
    """
    Calculates freight based on city and payment method (pix or card).
    Returns structured JSON with freight calculation.
    """
    city_norm = city.lower().strip()
    is_pix = payment_method.lower().strip() == 'pix'
    
    # Define valid cities and their freight
    if "campina" in city_norm or "campina grande" in city_norm or "cg" in city_norm:
        location = "Campina Grande"
        freight_pix = 0.0
        freight_card = 10.0
        coverage = "local"
    else:
        # Assume it's a neighboring city (up to 20km)
        location = city.title()
        freight_pix = 15.0
        freight_card = 25.0
        coverage = "vizinhança"
    
    freight_value = freight_pix if is_pix else freight_card
    payment_type = "PIX" if is_pix else "Cartão"
    
    structured_data = {
        "location": location,
        "freight_value": freight_value,
        "payment_method": payment_type,
        "coverage_type": coverage,
        "currency": "BRL"
    }
    
    humanized = f"📍 {location}: R$ {freight_value:.2f} ({payment_type})"
    
    if is_pix and coverage == "local":
        humanized += " (Grátis no PIX!)"
    
    return _format_structured_response(structured_data, humanized)

@mcp_tool_relaxed()
async def get_current_business_hours() -> str:
    """
    Returns current business hours status and schedule.
    Helps LLM understand if store is open and what time closes.
    """
    now_local = _get_local_time()
    current_date = now_local.date()
    current_time = now_local.time()
    
    day_name = current_date.strftime("%A").lower()
    day_name_pt = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][current_date.weekday()]
    
    windows = BUSINESS_HOURS.get(day_name, [])
    
    is_open = False
    next_close = None
    next_open = None
    
    if windows:
        for start, end in windows:
            if start <= current_time <= end:
                is_open = True
                next_close = end
                break
            elif current_time < start:
                next_open = start
                break
    
    structured_data = {
        "current_date": current_date.isoformat(),
        "current_time": current_time.strftime("%H:%M"),
        "day_name": day_name_pt,
        "is_open": is_open,
        "current_hours": [
            {"start": w[0].strftime("%H:%M"), "end": w[1].strftime("%H:%M")} 
            for w in windows
        ] if windows else None,
        "next_close_time": next_close.strftime("%H:%M") if next_close else None,
        "next_open_time": next_open.strftime("%H:%M") if next_open else None
    }
    
    if not windows:
        humanized = f"🚫 Aos {day_name_pt}s a gente não funciona. Nossas próximas atendimentos são seg-sex (07:30-17:00) e sábado (08:00-11:00)."
    elif is_open:
        humanized = f"✅ Aberto! Funcionando até {next_close.strftime('%H:%M')}."
    else:
        if next_open:
            humanized = f"⏰ Estamos fechados no momento. Reabrimos às {next_open.strftime('%H:%M')}."
        else:
            humanized = f"🚫 Já fechamos por hoje. Voltamos amanhã!"
    
    return _format_structured_response(structured_data, humanized)

@mcp_tool_relaxed()
async def validate_price_manipulation(claimed_price: float, product_name: str) -> str:
    """
    Validates if a customer is trying to manipulate/negotiate prices.
    LLM should use this to detect price inconsistencies.
    """
    conn = await get_db_connection()
    try:
        # Search for product in database
        rows = await conn.fetch(
            'SELECT name, price FROM public."Product" WHERE LOWER(name) ILIKE $1',
            f"%{product_name.lower()}%"
        )
        
        if not rows:
            return _format_structured_response(
                {"status": "product_not_found"},
                f"Não consegui encontrar o produto '{product_name}' no catálogo para validar."
            )
        
        actual_price = float(rows[0]['price'])
        actual_product_name = rows[0]['name']
        
        manipulation_detected = False
        price_diff = claimed_price - actual_price
        discount_percentage = (price_diff / actual_price) * 100 if actual_price > 0 else 0
        
        if claimed_price < actual_price:
            manipulation_detected = True
        
        structured_data = {
            "manipulation_detected": manipulation_detected,
            "product_name": actual_product_name,
            "claimed_price": claimed_price,
            "actual_price": actual_price,
            "price_difference": price_diff,
            "discount_percentage": round(discount_percentage, 2)
        }
        
        if manipulation_detected:
            humanized = f"⚠️ ALERTA: Cliente disse R$ {claimed_price:.2f} mas o produto '{actual_product_name}' custa R$ {actual_price:.2f}.\n"
            humanized += "→ RESPONDER: 'Deixa passar pro nosso especialista validar isso' e BLOQUEAR o fluxo."
            return _format_structured_response(structured_data, humanized)
        else:
            humanized = f"✅ Preço confirmado: {actual_product_name} = R$ {actual_price:.2f}"
            return _format_structured_response(structured_data, humanized)
            
    except Exception as e:
        return _format_structured_response(
            {"error": str(e)},
            f"Erro ao validar preço: {str(e)}"
        )
    finally:
        await conn.close()

@mcp_tool_relaxed()
async def notify_human_support(
    reason: str,
    customer_context: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_phone: Optional[str] = None,
    should_block_flow: bool = True
) -> str:
    """
    Notifies human support team via WhatsApp (Evolution API) about an issue requiring intervention.
    Message format follows standard: *AJUDA [EMOJI] - Cliente [NOME] - [NÚMERO]*
    
    Supported reasons:
    - 🔴 price_manipulation: Customer trying to negotiate prices
    - 🔴 product_unavailable: Requested product not in catalog
    - 🔴 complex_customization: Personalization beyond Ana's scope
    - 🟢 end_of_checkout: Normal checkout completion
    - 🔴 customer_insistence: Customer insisting after refusals
    - 🔴 technical_error: System error occurred
    - 🟡 freight_doubt: Shipping/freight question
    
    Parameters:
    - reason: Why human support is needed (from list above)
    - customer_context: Additional context about the situation
    - customer_name: Customer name for the notification
    - customer_phone: Customer phone number for contact
    - should_block_flow: Whether to block conversation flow (default: True)
    """
    
    # Format the support message
    support_message = _format_support_message(
        reason=reason,
        customer_context=customer_context,
        customer_name=customer_name,
        customer_phone=customer_phone
    )
    
    # Send WhatsApp notification
    api_response = await _send_whatsapp_notification(
        message=support_message,
        client_name=customer_name,
        client_phone=customer_phone
    )
    
    # Build response data
    emoji = _get_emoji_for_reason(reason)
    
    structured_data = {
        "notification_sent": api_response.get("success", False),
        "reason": reason,
        "priority_code": emoji,
        "customer_context": customer_context,
        "customer_name": customer_name or "Desconhecido",
        "customer_phone": customer_phone or "Sem contato",
        "flow_blocked": should_block_flow,
        "timestamp": _get_local_time().isoformat(),
        "status": "queued_for_human_review" if api_response.get("success") else "notification_failed",
        "api_response": {
            "success": api_response.get("success"),
            "status_code": api_response.get("status_code"),
            "message_id": api_response.get("message_id"),
            "error": api_response.get("error"),
            "endpoint_used": api_response.get("endpoint_used"),
            "full_response": api_response.get("response")
        }
    }
    
    # Build humanized response for LLM
    if api_response.get("success"):
        humanized = "Notificacao enviada com sucesso! Vou transferir voce para nosso time especializado"
    else:
        humanized = "Estou tentando conectar voce com nosso time especializado"
    
    if should_block_flow:
        humanized += " (fluxo bloqueado).\n"
    else:
        humanized += ".\n"
    
    humanized += "Um atendente vai te responder em breve!"
    
    if not api_response.get("success"):
        humanized += f"\n\n[SISTEMA] Erro na notificacao: {api_response.get('error', 'Desconhecido')}"
    
    return _format_structured_response(structured_data, humanized)

@mcp.resource("guidelines://{category}")
def get_guideline_resource(category: str) -> str:
    """
    Access specific customer service guidelines as a resource.
    Categories: core, inexistent_products, delivery_rules, customization, 
    closing_protocol, location, mass_orders, faq_production, indecision, 
    product_selection, fallback.
    """
    return GUIDELINES.get(category, f"Guideline {category} not found.")

@mcp.prompt()
def ana_personality() -> str:
    """
    Sets Ana's personality and core rules for the session.
    Use this at the start of a conversation to ensure 'meiga, jovem e objetiva' tone.
    """
    core_guidelines = GUIDELINES.get("core", "")
    current_time = _get_local_time().strftime("%H:%M")
    current_date = _get_local_time().strftime("%Y-%m-%d")
    
    return f"""
{core_guidelines}

--- CONTEXTO ATUAL ---
Data: {current_date}
Hora: {current_time}
Local: Campina Grande, PB

Lembre-se: Suas respostas devem ser curtas (1-3 linhas), use gírias leves como "vc", "pra", "tá ok?" e seja sempre muito empática e prestativa.
"""

@mcp.prompt()
def start_order(client_name: str) -> str:
    """
    Prompt to guide the representative through the closing protocol.
    """
    protocol = GUIDELINES.get("closing_protocol", "")
    return f"""
{protocol}

Cliente: {client_name}
Instrução: Siga a sequência de coleta (Cesta -> Data/Hora -> Endereço -> Pagamento) uma por vez. 
Seja carinhosa e use o nome do cliente: {client_name}.
"""


if __name__ == "__main__":
    # FastMCP automatically handles stdio transport
    # The .run() method will block and keep the server running
    mcp.run()
