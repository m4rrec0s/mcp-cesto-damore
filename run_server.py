#!/usr/bin/env python3
import sys
import asyncio
import json
import logging
import traceback
import os
from datetime import datetime
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Authentication configuration
API_KEY = os.getenv("MCP_API_KEY", "default-api-key-change-me")
API_KEY_HEADER = "X-API-Key"

logger.info("="*70)
logger.info("Importing MCP server...")
try:
    from mcp_server import mcp
    logger.info("✅ MCP imported")
except Exception as e:
    logger.error(f"❌ Failed to import MCP server: {e}")
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

# Initialize our main FastAPI app
app = FastAPI(
    title="Ana - Cesto d'Amore MCP Server",
    description="MCP Server for n8n and Cesto d'Amore integration",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize start time
start_time = datetime.now()

# Mount FastMCP's internal app for SSE support
# FastMCP supports 'http', 'streamable-http', and 'sse' transports
# We use 'sse' for Server-Sent Events which is required for SSE clients
try:
    # Use SSE transport for better client compatibility
    mcp_app = mcp.http_app(transport='sse')
    app.mount("/mcp", mcp_app)
    logger.info("✅ FastMCP internal app mounted at /mcp with SSE transport")
except Exception as e:
    logger.error(f"⚠️ Could not mount FastMCP internal app: {e}")
    logger.debug(f"Error details: {e}", exc_info=True)
    # Fallback to default HTTP transport
    try:
        mcp_app = mcp.http_app()
        app.mount("/mcp", mcp_app)
        logger.info("✅ FastMCP internal app mounted at /mcp (default transport)")
    except Exception as e2:
        logger.error(f"❌ Failed to mount FastMCP app: {e2}")

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
        if hasattr(mcp, "_tools"):
            tools_count = len(mcp._tools)
        
        return {
            "status": "ok",
            "service": "Ana - Cesto d'Amore MCP",
            "uptime_seconds": (datetime.now() - start_time).total_seconds(),
            "mcp_available": True,
            "mcp_tools_count": tools_count,
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
        if hasattr(mcp, "_tools"):
            # Direct access to tools dict
            tools_list = [name for name in mcp._tools.keys() if not name.startswith('_')]
        
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
    """Execute an MCP tool."""
    tool_name = None
    try:
        data = await request.json()
        tool_name = data.get("tool")
        arguments = data.get("arguments", {})
        
        if not tool_name:
            raise HTTPException(status_code=400, detail="Missing 'tool' parameter")
        
        logger.info(f"Calling tool: {tool_name} with args: {arguments}")
        
        # Get the tool function from mcp._tools
        if hasattr(mcp, "_tools") and tool_name in mcp._tools:
            tool_func = mcp._tools[tool_name]
            # Call the tool function with arguments
            if asyncio.iscoroutinefunction(tool_func):
                result = await tool_func(**arguments)
            else:
                result = tool_func(**arguments)
        else:
            raise ValueError(f"Tool '{tool_name}' not found. Available: {list(mcp._tools.keys()) if hasattr(mcp, '_tools') else 'unknown'}")
            
        return {
            "success": True,
            "tool": tool_name,
            "result": result
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calling {tool_name}: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

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
