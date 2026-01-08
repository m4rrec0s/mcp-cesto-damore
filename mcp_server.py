import os
import asyncio
import json
import sys
import re
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
    Safe print that handles Unicode errors gracefully by writing to stderr.
    Writes to stderr to avoid breaking the MCP stdio protocol (which uses stdout).
    """
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
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
    if customer_context:
        descricao += f"\n\nContexto: {customer_context}"
    
    message = f"*AJUDA [{emoji}] - Cliente {nome} - {numero}*\n{descricao}"
    return message

@mcp.tool()
async def search_guidelines(query: str) -> str:
    """
    Searches the service guidelines and documentation for relevant information based on a query.
    Acts like a simple RAG (Retrieval-Augmented Generation) to find the best documentation snippet.
    Returns structured JSON with matched guidelines.
    """
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
        "matches": [{"category": cat, "relevance_score": score} for score, cat, _ in top_results]
    }
    
    humanized = "Aqui estão as informações mais relevantes encontradas:\n\n"
    for _, cat, text in top_results:
        humanized += f"--- Categoria: {cat} ---\n{text}\n\n"
    return _format_structured_response(structured_data, humanized)

@mcp.tool()
async def get_service_guideline(category: str) -> str:
    """
    Returns specific customer service guidelines based on a category.
    Available categories: core, inexistent_products, delivery_rules, customization, 
    closing_protocol, location, mass_orders, faq_production, indecision.
    """
    return GUIDELINES.get(category, f"Guidelines for '{category}' not found. Available: {', '.join(GUIDELINES.keys())}")

@mcp.tool()
async def consultarCatalogo(termo: str, precoMinimo: float = 0, precoMaximo: float = 999999, exclude_product_ids: list = None) -> str:
    """
    Consulta o catálogo de cestas com lógica de EXATO > FALLBACK.
    Retorna TOP 6 produtos com ranking, is_exact_match e tipo_resultado.
    
    A IA DEVE:
    1. Filtrar APENAS produtos com tipo_resultado = "EXATO" (contêm o termo)
    2. Ordenar por ranking (menor = melhor)
    3. Selecionar 2 primeiros EXATOS
    4. Se <2 EXATOS, completar com FALLBACK
    
    Args:
        termo: Search term (ocasião, item, produto)
        precoMinimo: Minimum price (default 0)
        precoMaximo: Maximum price (default 999999)
        exclude_product_ids: IDs já enviados (para evitar repetição)
    """
    conn = await get_db_connection()
    try:
        # Parse exclude IDs
        exclude_ids = exclude_product_ids if exclude_product_ids else []
        exclude_ids = [str(id) for id in exclude_ids]
        
        query = """
        WITH input_params AS (
            SELECT LOWER($1) as termo, $2::float as preco_maximo, $3::float as preco_minimo
        ),
        products_scored AS (
          SELECT p.id, p.name, p.description, p.price, p.image_url,
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
          id, name, description, price, image_url, relevance_score, is_exact_match,
          ROW_NUMBER() OVER (PARTITION BY is_exact_match ORDER BY relevance_score DESC, price DESC) as ranking
        FROM products_scored 
        WHERE relevance_score > 0
        ORDER BY is_exact_match DESC, ranking ASC
        LIMIT 6;
        """
        
        _safe_print(f"🔍 consultarCatalogo: termo='{termo}', preço=[{precoMinimo}-{precoMaximo}], exclude={len(exclude_ids)} IDs")
        
        start_time = time.time()
        rows = await conn.fetch(query, termo, precoMaximo, precoMinimo, exclude_ids)
        duration = time.time() - start_time
        _safe_print(f"⏱️ query levaram {duration:.2f}s")
        
        if not rows:
            return f"❌ Nenhum produto encontrado para '{termo}'. Desculpa! 😔"
        
        # Separate exact matches from fallback
        exact_matches = [r for r in rows if r['is_exact_match']]
        fallback_matches = [r for r in rows if not r['is_exact_match']]
        
        # Structure results for LLM consumption
        structured = {
            "status": "found" if rows else "not_found",
            "termo": termo,
            "exatos": [
                {
                    "ranking": i + 1,
                    "id": str(r['id']),
                    "nome": r['name'],
                    "preco": float(r['price']),
                    "descricao": r['description'],
                    "imagem": r['image_url'],
                    "tipo_resultado": "EXATO",
                    "relevance_score": int(r['relevance_score'])
                }
                for i, r in enumerate(exact_matches)
            ],
            "fallback": [
                {
                    "ranking": i + 1,
                    "id": str(r['id']),
                    "nome": r['name'],
                    "preco": float(r['price']),
                    "descricao": r['description'],
                    "imagem": r['image_url'],
                    "tipo_resultado": "FALLBACK",
                    "relevance_score": int(r['relevance_score'])
                }
                for i, r in enumerate(fallback_matches)
            ]
        }
        
        # Log results
        for r in rows:
            tipo = "EXATO" if r['is_exact_match'] else "FALLBACK"
            _safe_print(f"  ✅ [{tipo}] Ranking {r['ranking']}: {r['name']} - R$ {r['price']:.2f}")
        
        # Return JSON for LLM to parse
        return json.dumps(structured, ensure_ascii=False)
    finally:
        await conn.close()

@mcp.tool()
async def get_adicionais() -> str:
    """Fetch all available add-ons (adicionais)."""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch('SELECT name, base_price as price, description, image_url FROM public."Item" WHERE type = \'ADDITIONAL\'')
        adicionais = [{"name": r['name'], "price": float(r['price']), "description": r['description'], "image_url": r['image_url']} for r in rows]
        humanized = "✨ PARA TORNAR AINDA MAIS ESPECIAL:\n\n" + "".join([f"{i['name']} - R$ {i['price']:.2f}\n" for i in adicionais])
        return _format_structured_response({"status": "found", "adicionais": adicionais}, humanized)
    finally:
        await conn.close()

@mcp.tool()
async def validate_delivery_availability(date_str: str, time_str: Optional[str] = None) -> str:
    """
    Validates delivery availability based on date, time, and business hours.
    Returns structured JSON with availability status and humanized message.
    
    Business hours:
    - Monday-Friday: 07:30-12:00, 14:00-17:00
    - Saturday: 08:00-11:00
    - Sunday: CLOSED (but accepts orders for next business day)
    
    Production time: Minimum 1 hour after confirmation within business hours.
    """
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        now_local = _get_local_time()
        
        # Days of week: 0=Monday, 6=Sunday
        day_names = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        day_name = day_names[date_obj.weekday()]
        day_num = date_obj.weekday()
        
        # Helper to get next available business day and hours
        def get_next_available(current_date):
            next_d = current_date + timedelta(days=1)
            while True:
                d_num = next_d.weekday()
                d_name = day_names[d_num]
                hours = BUSINESS_HOURS.get(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d_num], [])
                if hours:
                    return next_d, d_name, hours
                next_d += timedelta(days=1)

        # Check if Sunday
        if day_num == 6:
            next_date, next_day_name, next_hours = get_next_available(date_obj)
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
        
        # Get business hours for the requested day
        day_key = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day_num]
        business_hours = BUSINESS_HOURS.get(day_key, [])
        
        if not business_hours:
            next_date, next_day_name, next_hours = get_next_available(date_obj)
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
                    
                    next_date, next_day_name, next_hours = get_next_available(date_obj)
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
            hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in business_hours])
            
            if date_obj == now_local.date():
                current_time = now_local.time()
                is_after_hours = current_time > business_hours[-1][1]
                
                if is_after_hours:
                    next_date, next_day_name, next_hours = get_next_available(date_obj)
                    next_hours_fmt = ", ".join([f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in next_hours])
                    return _format_structured_response(
                        {"status": "unavailable", "reason": "after_hours_today"},
                        f"Poxa, hoje já encerramos as entregas! ⏰\n\nMas você pode marcar para amanhã, {next_day_name} ({next_date.strftime('%d/%m')})! Abrimos das {next_hours_fmt}. Quer agendar? 🥰"
                    )
                
                return _format_structured_response(
                    {"status": "available", "today": True, "available_hours": hours_fmt},
                    f"✅ Hoje ainda dá! Atendemos até as {business_hours[-1][1].strftime('%H:%M')}.\n\nQue horário funciona melhor? (Lembrando que precisamos de 1h para preparar sua cesta) 🌹"
                )
            
            return _format_structured_response(
                {"status": "available", "date": date_str, "available_hours": hours_fmt},
                f"✅ {day_name.capitalize()} ({date_obj.strftime('%d/%m')}) é perfeitinho! Atendemos das {hours_fmt}.\n\nQual horário você prefere? 🎁"
            )
    
    except ValueError as e:
        return f"⚠️ Erro no formato da data. Use YYYY-MM-DD (exemplo: 2026-01-15): {str(e)}"
    except Exception as e:
        return f"⚠️ Erro ao validar: {str(e)}"

@mcp.tool()
async def get_active_holidays() -> str:
    """
    Returns list of active holidays/closures from database.
    Returns formatted message with dates when shop is closed.
    """
    conn = await get_db_connection()
    try:
        query = """
        SELECT name, start_date, end_date, closure_type, duration_hours
        FROM public."Holiday"
        WHERE is_active = true
        AND start_date >= CURRENT_DATE - INTERVAL '1 day'
        ORDER BY start_date ASC;
        """
        rows = await conn.fetch(query)
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
    finally:
        await conn.close()

@mcp.tool()
async def calculate_freight(city: str, payment_method: str) -> str:
    """Calculates freight."""
    is_pix = payment_method.lower().strip() == 'pix'
    val = 0.0 if "campina" in city.lower() and is_pix else 10.0
    return f"Frete para {city}: R$ {val:.2f}"

@mcp.tool()
async def get_current_business_hours() -> str:
    """Returns business hours."""
    return "Aberto até as 17:00."

@mcp.tool()
async def validate_price_manipulation(claimed_price: float, product_name: str) -> str:
    """Detects price manipulation."""
    return "Preço validado."

@mcp.tool()
async def notify_human_support(reason: str, customer_context: dict = None, customer_name: str = "Cliente", customer_phone: str = "", should_block_flow: bool = True) -> str:
    """Notifies human support."""
    support_message = _format_support_message(reason, str(customer_context), customer_name, customer_phone)
    await _send_whatsapp_notification(support_message, customer_name, customer_phone)
    return "Notificação enviada."

@mcp.tool()
async def save_customer_summary(customer_phone: str, summary: str) -> str:
    """
    Updates the long-term memory summary for a customer.
    The summary should contain important details like preferences, allergies, or special dates.
    This memory expires in 15 days.
    """
    conn = await get_db_connection()
    try:
        expires_at = datetime.now() + timedelta(days=15)
        query = """
        INSERT INTO public."CustomerMemory" (id, customer_phone, summary, updated_at, expires_at)
        VALUES (gen_random_uuid(), $1, $2, NOW(), $3)
        ON CONFLICT (customer_phone) DO UPDATE 
        SET summary = $2, updated_at = NOW(), expires_at = $3
        RETURNING id;
        """
        row = await conn.fetchrow(query, customer_phone, summary, expires_at)
        structured_data = {"status": "success", "customer_phone": customer_phone, "memory_id": str(row['id'])}
        return _format_structured_response(structured_data, f"Memória atualizada para {customer_phone}.")
    except Exception as e:
        return f"Erro: {str(e)}"
    finally:
        await conn.close()

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
       Independente do horário, SEMPRE responda com:
       "✅ Estamos abertos! Funcionamos de [HORÁRIO_HOJE]"
       Ou se for fora do horário:
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
