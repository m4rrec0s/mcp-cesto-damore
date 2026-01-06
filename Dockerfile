# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install system dependencies (if needed for PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy application files
COPY mcp_server.py .
COPY mcp_wrapper.py .
COPY mcp_entry.py .
COPY guidelines.py .
COPY run_server.py .

# Create a non-root user for security
RUN useradd -m -u 1000 mcp_user && chown -R mcp_user:mcp_user /app
USER mcp_user

# Expose port for FastMCP server
EXPOSE 5000

# Health check - verify the HTTP server is responding
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1
# Run the HTTP server directly
CMD ["python", "run_server.py"]
