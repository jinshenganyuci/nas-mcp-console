FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NAS_MCP_DATA=/data
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY app ./app
USER 1000:1000
EXPOSE 8090
CMD ["uvicorn", "app.server:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log", "--no-proxy-headers"]
