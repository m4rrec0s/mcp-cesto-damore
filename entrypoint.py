#!/usr/bin/env python
"""
Entry point for MCP Server in Docker.
Keeps the FastMCP server running with proper signal handling.
"""
import sys
import asyncio
import signal
from mcp_server import mcp

# Flag to control graceful shutdown
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    print(f"\n[INFO] Received signal {signum}, shutting down...")
    shutdown_event.set()

async def run_mcp():
    """Run the MCP server."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    print("[INFO] Starting MCP Server...")
    try:
        # Run the FastMCP server
        # This will block and handle stdio transport
        mcp.run()
    except KeyboardInterrupt:
        print("[INFO] MCP Server interrupted")
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] MCP Server failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    # For stdio transport in Docker, we need asyncio event loop
    try:
        asyncio.run(run_mcp())
    except KeyboardInterrupt:
        print("\n[INFO] MCP Server shutdown complete")
        sys.exit(0)
