# 统一边缘应用镜像：同一镜像 + 显式多节点配置（compose.multi-edge.yml）。
# 构建上下文必须是 cloud_edge_project 根目录：
#   docker build -f edge_service/Dockerfile -t cloud-edge/edge-service .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY edge_service/requirements.txt ./edge_service/requirements.txt
RUN pip install --no-cache-dir \
    --requirement requirements.txt \
    --requirement edge_service/requirements.txt

COPY common ./common
COPY core ./core
COPY configs ./configs
COPY scenarios ./scenarios
COPY contracts ./contracts
COPY edge_service ./edge_service

# 节点身份、端口与数据目录全部由环境变量决定，镜像本身保持中立。
ENV EDGE_SERVICE_HOST=0.0.0.0 \
    EDGE_SERVICE_PORT=8001 \
    EDGE_CONTROL_HOST=0.0.0.0 \
    EDGE_CONTROL_PORT=8011 \
    EDGE_V12_DATABASE_PATH=/app/data/edge_v12.db \
    EDGE_BEARING_WINDOW_CACHE_DIR=/app/data/bearing_windows \
    EDGE_CLOUD_REVIEW_CACHE_DIR=/app/data/edge_cloud_review \
    EDGE_RAW_SAMPLE_DIRECTORY=/app/data/raw_analysis_samples \
    EDGE_PACKET_ROUTE_ERROR_LOG=/app/data/edge_packet_route_errors.jsonl

RUN mkdir -p /app/data

EXPOSE 8001 8011

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)); assert data.get('ready', False)"]

CMD ["python", "edge_service/run_edge_service.py", "--host", "0.0.0.0", "--port", "8001"]
