FROM python:3.12-slim
LABEL org.opencontainers.image.title="NAS MCP Console" \
      org.opencontainers.image.description="Private credential vault with a visual admin console and MCP access" \
      org.opencontainers.image.source="https://github.com/jinshenganyuci/nas-mcp-console"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NAS_MCP_DATA=/data
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt \
    && install -d -m 0700 -o 1000 -g 1000 /data
COPY app ./app
USER 1000:1000
EXPOSE 8090
CMD ["uvicorn", "app.server:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log", "--no-proxy-headers"]
