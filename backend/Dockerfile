FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
COPY mcp_server/requirements.txt mcp_server/requirements.txt
COPY seed/requirements.txt seed/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt -r mcp_server/requirements.txt -r seed/requirements.txt
COPY backend backend
COPY mcp_server mcp_server
COPY seed seed
USER 10001:10001
CMD ["python", "-m", "uvicorn", "backend.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
