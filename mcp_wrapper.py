#!/usr/bin/env python3
"""
Wrapper MCP para Claude Desktop
Executa o servidor HTTP e expõe a interface MCP via stdio
"""
import subprocess
import sys
import os
import time
import asyncio
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("mcp-wrapper")

def start_http_server():
    """Inicia o servidor HTTP em background"""
    logger.info("Iniciando servidor HTTP MCP...")
    
    # Obter o diretório do script
    script_dir = Path(__file__).parent.absolute()
    
    # Variáveis de ambiente
    env = os.environ.copy()
    env.update({
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT", "5432"),
        "POSTGRES_USER": os.getenv("POSTGRES_USER", "cestoadore"),
        "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD", "senha123"),
        "POSTGRES_DB": os.getenv("POSTGRES_DB", "cesto_damore"),
        "EVOLUTION_API_URL": os.getenv("EVOLUTION_API_URL", ""),
        "EVOLUTION_API_KEY": os.getenv("EVOLUTION_API_KEY", ""),
        "EVOLUTION_API_INSTANCE": os.getenv("EVOLUTION_API_INSTANCE", ""),
        "CHAT_ID": os.getenv("CHAT_ID", ""),
    })
    
    try:
        # Iniciar o servidor em subprocess
        process = subprocess.Popen(
            [sys.executable, str(script_dir / "run_server.py")],
            cwd=str(script_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        logger.info(f"Servidor HTTP iniciado (PID: {process.pid})")
        
        # Aguardar um pouco para o servidor ficar pronto
        time.sleep(3)
        
        # Testar se o servidor está respondendo
        try:
            import requests
            response = requests.get("http://localhost:5000/health", timeout=2)
            if response.status_code == 200:
                logger.info("✅ Servidor HTTP está operacional")
                return process
        except Exception as e:
            logger.warning(f"Servidor ainda inicializando... {e}")
            time.sleep(2)
            return process
            
    except Exception as e:
        logger.error(f"Erro ao iniciar servidor HTTP: {e}")
        raise

async def main():
    """Função principal"""
    logger.info("=== Wrapper MCP para Claude Desktop ===")
    logger.info("Iniciando servidor MCP...")
    
    # Importar o FastMCP server
    try:
        from mcp_server import mcp
        logger.info("✅ MCP server importado")
    except Exception as e:
        logger.error(f"❌ Erro ao importar MCP server: {e}")
        sys.exit(1)
    
    # Iniciar o servidor HTTP em background
    http_process = start_http_server()
    
    try:
        # Executar o servidor MCP via stdio
        logger.info("Iniciando MCP stdio transport...")
        await mcp.run_stdio_async()
    except KeyboardInterrupt:
        logger.info("Encerrando...")
    except Exception as e:
        logger.error(f"Erro: {e}")
    finally:
        if http_process:
            logger.info("Encerrando servidor HTTP...")
            http_process.terminate()
            try:
                http_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                http_process.kill()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Erro fatal: {e}")
        sys.exit(1)
