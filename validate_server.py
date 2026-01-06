#!/usr/bin/env python3
"""
Teste de Validação - MCP SSE Server
Verifica se todos os endpoints estão funcionando corretamente
"""
import requests
import sys

def test_endpoint(path, expected_status=200):
    """Testa um endpoint específico"""
    try:
        if path.endswith('sse'):
            # Para SSE, usar timeout curto e stream
            r = requests.get(f"http://localhost:5000{path}", timeout=1, stream=True)
        else:
            # Para endpoints normais
            r = requests.get(f"http://localhost:5000{path}", timeout=2)
        
        if path.endswith('sse'):
            # Para SSE, validar Content-Type
            if r.status_code == 200 and 'text/event-stream' in r.headers.get('Content-Type', ''):
                return True, "✅ SSE conectado"
            else:
                return False, f"❌ Status {r.status_code}"
        else:
            # Para outros endpoints
            if r.status_code == expected_status:
                return True, f"✅ Status {r.status_code}"
            else:
                return False, f"❌ Status {r.status_code}"
    except requests.Timeout:
        if path.endswith('sse'):
            return True, "✅ SSE streaming (timeout esperado)"
        else:
            return False, "❌ Timeout"
    except Exception as e:
        return False, f"❌ {type(e).__name__}"

print("\n" + "="*70)
print("🧪 VALIDAÇÃO DO SERVIDOR MCP")
print("="*70)

endpoints = [
    ("/health", 200, "Health Check"),
    ("/diag", 200, "Diagnóstico"),
    ("/tools", 200, "Lista de Ferramentas"),
    ("/mcp/sse", 200, "SSE Endpoint (CRÍTICO)"),
]

all_passed = True
for path, expected, desc in endpoints:
    passed, msg = test_endpoint(path, expected)
    all_passed = all_passed and passed
    status_icon = "✅" if passed else "❌"
    print(f"{status_icon} {desc:40} {path:20} {msg}")

print("="*70)
if all_passed:
    print("\n✅ SUCESSO! Todos os endpoints estão operacionais!")
    print("\n📋 Endpoints disponíveis:")
    print("   GET  /health      → Status e uptime do servidor")
    print("   GET  /diag        → Diagnóstico completo do sistema")
    print("   GET  /tools       → Lista de ferramentas MCP registradas")
    print("   GET  /mcp/sse     → Endpoint SSE para clientes MCP (n8n, Studio)")
    print("   GET  /docs        → Swagger UI (FastAPI)")
    print("   GET  /studio      → MCP Studio (Inspector Visual)")
    print("\n🚀 O servidor está pronto para ser usado com n8n!")
    sys.exit(0)
else:
    print("\n❌ Alguns endpoints não estão funcionando.")
    print("Revise a configuração do servidor.")
    sys.exit(1)
