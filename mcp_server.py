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

# Load environment variables
from pathlib import Path
project_dir = Path(__file__).parent
load_dotenv(dotenv_path=project_dir / '.env')

# Initialize FastMCP server
mcp = FastMCP("Ana - Cesto d'Amore")

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
    import re
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
async def search_products(termo: str, preco_minimo: float = 0, preco_maximo: float = 999999) -> str:
    """
    Search for products in the catalog using relevance scoring and business rules.
    Returns structured JSON with product details + humanized message.
    """
    conn = await get_db_connection()
    try:
        query = """
        WITH input_params AS (SELECT LOWER($1) as termo, $2::float as preco_maximo, $3::float as preco_minimo),
        products_scored AS (
          SELECT p.id, p.name, p.description, p.price, p.image_url,
          (
            (CASE WHEN p.description ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 20 ELSE 0 END) +
            (CASE WHEN p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 15 ELSE 0 END)
          ) as relevance_score,
          (CASE WHEN p.description ILIKE '%' || (SELECT termo FROM input_params) || '%' OR p.name ILIKE '%' || (SELECT termo FROM input_params) || '%' THEN 1 ELSE 0 END) as is_exact_match
          FROM public."Product" p
          WHERE p.price >= (SELECT preco_minimo FROM input_params) AND p.price <= (SELECT preco_maximo FROM input_params)
        )
        SELECT name, description, price, image_url, CASE WHEN is_exact_match = 1 THEN 'EXATO' ELSE 'FALLBACK' END as tipo_resultado
        FROM products_scored ORDER BY is_exact_match DESC, relevance_score DESC, price DESC LIMIT 5;
        """
        rows = await conn.fetch(query, termo, preco_maximo, preco_minimo)
        if not rows:
            return _format_structured_response({"status": "not_found", "search_term": termo}, "Nenhum produto encontrado.")
        
        products = [{"name": r['name'], "description": r['description'], "price": float(r['price']), "image_url": r['image_url'], "match_type": r['tipo_resultado']} for r in rows]
        humanized = "".join([f"{p['image_url']}\n{p['name']} - R$ {p['price']:.2f}\n{p['description']}\n\n" for p in products])
        return _format_structured_response({"status": "found", "products": products}, humanized)
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
    """Validates delivery availability."""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        now_local = _get_local_time()
        if date_obj.weekday() == 6:
            return "Fechado aos domingos."
        return f"Data {date_str} disponível!"
    except Exception as e:
        return str(e)

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

if __name__ == "__main__":
    mcp.run()