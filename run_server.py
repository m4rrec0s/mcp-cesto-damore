#!/usr/bin/env python3
import sys
import asyncio
import json
import logging
import traceback
import os
from datetime import datetime
import time

from typing import Dict, Any, List, Optional
import inspect

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Configure logging with better formatting
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Authentication configuration
API_KEY = os.getenv("MCP_API_KEY", "default-api-key-change-me")
API_KEY_HEADER = "X-API-Key"

logger.info("="*70)
logger.info("Importando servidor MCP...")

# MCP initialization flag
mcp_initialized = False
mcp_init_time = None

try:
    from mcp_server import mcp
    logger.info("✅ MCP importado com sucesso")
except Exception as e:
    logger.error(f"❌ Falha ao importar servidor MCP: {e}")
    traceback.print_exc()
    sys.exit(1)

# Authentication dependency
async def verify_api_key(x_api_key: str = Header(None)) -> str:
    """Verify API key from X-API-Key header."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header"
        )
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key"
        )
    return x_api_key

# Initialize FastMCP app first to get lifespan
mcp_app = None
try:
    # Use SSE transport for better client compatibility (required by n8n/Easypanel)
    mcp_app = mcp.http_app(transport='sse')
    logger.info("✅ Aplicação interna FastMCP inicializada (transporte SSE)")
    mcp_initialized = True
    mcp_init_time = datetime.now()
except Exception as e:
    logger.error(f"❌ Falha ao inicializar aplicação FastMCP: {e}")
    logger.debug(f"Detalhes do erro: {e}", exc_info=True)
    sys.exit(1)

# Middleware para garantir que MCP está inicializado (ASGI implementation to avoid AssertionError with SSE)
class MCPInitializationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not mcp_initialized:
            response = JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "error": "Servidor MCP ainda não está inicializado",
                    "status": "initializing",
                    "message": "Tente novamente em alguns segundos"
                }
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

# Initialize our main FastAPI app with FastMCP's lifespan
app = FastAPI(
    title="Ana - Cesto d'Amore MCP Server",
    description="MCP Server for n8n and Cesto d'Amore integration",
    version="1.0.0",
    lifespan=mcp_app.lifespan
)

# Adicionar middleware de verificação de inicialização
app.add_middleware(MCPInitializationMiddleware)

# Mount the FastMCP app
app.mount("/mcp", mcp_app)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize start time
start_time = datetime.now()

# Add error handler for better debugging
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle all exceptions and return structured error response."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": str(exc),
            "type": type(exc).__name__,
            "path": str(request.url),
            "timestamp": datetime.now().isoformat()
        }
    )

@app.get("/", tags=["System"])
async def root():
    """Root endpoint - returns API information and available routes."""
    return {
        "service": "Ana - Cesto d'Amore MCP Server",
        "version": "1.0.0",
        "status": "online",
        "authentication": "Required (X-API-Key header)",
        "timestamp": datetime.now().isoformat(),
        "routes": {
            "system": {
                "health": {"method": "GET", "path": "/health", "requires_auth": False},
                "diag": {"method": "GET", "path": "/diag", "requires_auth": True},
                "info": {"method": "GET", "path": "/", "requires_auth": False},
            },
            "mcp": {
                "tools": {"method": "GET", "path": "/tools", "requires_auth": True},
                "call": {"method": "POST", "path": "/call", "requires_auth": True},
            },
            "documentation": {
                "swagger": {"method": "GET", "path": "/docs", "requires_auth": False},
                "openapi": {"method": "GET", "path": "/openapi.json", "requires_auth": False},
            },
            "mcp_internal": {
                "sse": {"method": "GET", "path": "/mcp/sse", "requires_auth": False, "note": "Server-Sent Events"},
                "http": {"method": "POST", "path": "/mcp", "requires_auth": False, "note": "HTTP transport"},
            }
        }
    }

@app.get("/health", tags=["System"])
async def health():
    """Health check endpoint."""
    uptime = (datetime.now() - start_time).total_seconds()
    return {
        "status": "ok",
        "service": "Ana - Cesto d'Amore MCP",
        "uptime_seconds": uptime,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/diag", tags=["System"], dependencies=[Depends(verify_api_key)])
async def diagnostic(api_key: str = Depends(verify_api_key)):
    """Diagnostic endpoint - check all systems."""
    try:
        tools_count = 0
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            tools_count = len(mcp._tool_manager._tools)
        
        init_delay = None
        if mcp_init_time:
            init_delay = (datetime.now() - mcp_init_time).total_seconds()
        
        return {
            "status": "ok",
            "service": "Ana - Cesto d'Amore MCP",
            "uptime_seconds": (datetime.now() - start_time).total_seconds(),
            "mcp_available": True,
            "mcp_initialized": mcp_initialized,
            "mcp_init_time": mcp_init_time.isoformat() if mcp_init_time else None,
            "seconds_since_init": init_delay,
            "mcp_tools_count": tools_count,
            "mcp_tools": list(mcp._tool_manager._tools.keys()) if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools") else [],
            "mcp_has_internal_app": hasattr(mcp, "http_app"),
            "cors_enabled": True,
            "timestamp": datetime.now().isoformat(),
            "endpoints": {
                "health": "/health",
                "tools": "/tools",
                "call": "/call (POST)",
                "dashboard": "/",
                "docs": "/docs",
                "studio": "/studio",
                "diagnostic": "/diag",
                "mcp_internal": "/mcp"
            }
        }
    except Exception as e:
        logger.error(f"Error in diagnostic: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": str(e),
                "type": type(e).__name__
            }
        )

@app.get("/tools", tags=["MCP"], dependencies=[Depends(verify_api_key)])
async def list_tools(api_key: str = Depends(verify_api_key)):
    """List available MCP tools, filtering out internal methods."""
    try:
        tools_list = []
        
        # Get tools using FastMCP internal access
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            # Direct access to tools dict
            tools_list = [name for name in mcp._tool_manager._tools.keys() if not name.startswith('_')]
        
        # If we couldn't get tools, try other methods
        if not tools_list:
            logger.warning("Could not retrieve tools list from FastMCP._tools")
        
        return {
            "success": True,
            "count": len(tools_list),
            "tools": sorted(list(set(tools_list)))
        }
    except Exception as e:
        logger.error(f"Error listing tools: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/call", tags=["MCP"], dependencies=[Depends(verify_api_key)])
async def call_tool(request: Request, api_key: str = Depends(verify_api_key)):
    """Execute an MCP tool - compatível com formatos do n8n, VS Code e MCP padrão.
    
    Suporta três formatos de entrada:
    1. {"tool": "name", "input": {...}} - Formato n8n (2025/2026)
    2. {"tool": "name", "arguments": {...}} - Formato MCP padrão
    3. {"tool": "name", "param1": "value1", ...} - Formato flat (fallback)
    """
    tool_name = None
    try:
        # Verificar se MCP está inicializado
        if not mcp_initialized:
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "error": "Servidor MCP ainda não está totalmente inicializado",
                    "error_code": "MCP_NOT_READY",
                    "retry_after": 2
                }
            )
        
        data = await request.json()
        tool_name = data.get("tool")
        
        if not tool_name:
            raise HTTPException(status_code=400, detail="Missing 'tool' parameter")

        # ── Normalização de formato ───────────────────────────────────────
        # Prioridade: input > arguments > root level (menos conhecido)
        if "input" in data and isinstance(data["input"], dict):
            # Formato n8n atual: argumentos dentro de "input"
            arguments = data["input"]
            logger.debug(f"Usando formato 'input' (estilo n8n)")
        elif "arguments" in data and isinstance(data["arguments"], dict):
            # Formato padrão MCP
            arguments = data["arguments"]
            logger.debug(f"Usando formato 'arguments' (MCP padrão)")
        else:
            # Fallback: extrai tudo que não é metadado conhecido
            known_keys = {
                "tool", "toolCallId", "sessionId", "action", "chatInput",
                "message", "chatId", "pushName", "messages", "messageId",
                "timestamp", "metadata", "input", "arguments"
            }
            arguments = {
                k: v for k, v in data.items()
                if k not in known_keys and not k.startswith("_")
            }
            logger.debug(f"Usando formato root level (fallback)")

        logger.info(f"[CALL] Ferramenta: {tool_name} | Argumentos: {list(arguments.keys())}")
        logger.debug(f"[DEBUG] Chaves do payload: {list(data.keys())}")
        logger.debug(f"[DEBUG] Argumentos extraídos: {arguments}")

        # ── Execução da ferramenta ────────────────────────────────────────
        if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
            if tool_name not in mcp._tool_manager._tools:
                available_tools = list(mcp._tool_manager._tools.keys())
                logger.warning(f"Ferramenta '{tool_name}' não encontrada. Disponíveis: {available_tools}")
                raise ValueError(f"Tool '{tool_name}' not found. Available: {available_tools}")
            
            tool_obj = mcp._tool_manager._tools[tool_name]
            tool_func = tool_obj.fn
            
            # Inspect tool function to get allowed parameters
            sig = inspect.signature(tool_func)
            allowed_params = set(sig.parameters.keys())
            
            # Filter arguments to only include allowed parameters
            filtered_args = {k: v for k, v in arguments.items() if k in allowed_params}
            
            logger.debug(f"[DEBUG] Argumentos filtrados para {tool_name}: {list(filtered_args.keys())}")
            logger.debug(f"[DEBUG] Argumentos não utilizados: {set(arguments.keys()) - set(filtered_args.keys())}")
            
            # Chama a ferramenta com os argumentos normalizados
            if asyncio.iscoroutinefunction(tool_func):
                result = await tool_func(**filtered_args)
            else:
                result = tool_func(**filtered_args)
        else:
            available_tools = list(mcp._tool_manager._tools.keys()) if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools") else []
            raise ValueError(f"Tool '{tool_name}' not found. Available: {available_tools}")
            
        return {
            "success": True,
            "tool": tool_name,
            "result": result
        }
    except HTTPException:
        raise
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        logger.error(f"[ERROR] Erro ao chamar ferramenta {tool_name}: {error_type}: {error_msg}", exc_info=True)
        
        # Retornar erro com mais informações para debug
        return JSONResponse(
            status_code=400 if error_type == "ValueError" else 500,
            content={
                "success": False,
                "error": error_msg,
                "error_type": error_type,
                "tool": tool_name,
                "timestamp": datetime.now().isoformat(),
                "mcp_initialized": mcp_initialized,
                "hint": "Verifique se a ferramenta existe e se os argumentos estão corretos"
            }
        )

@app.get("/dashboard", tags=["Internal"])
async def studio():
    """MCP Inspector Visual (placeholder)."""
    return {
        "service": "Ana - Cesto d'Amore MCP Studio",
        "status": "available",
        "note": "Use /docs for API documentation or /mcp for MCP protocol endpoint",
        "mcp_endpoint": "/mcp",
        "docs_endpoint": "/docs"
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
