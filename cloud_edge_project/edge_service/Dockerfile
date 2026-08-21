# 统一边缘应用镜像：同一镜像 + 显式多节点配置（compose.multi-edge.yml）。
# 构建上下文必须是 cloud_edge_project 根目录：
#   docker build -f edge_service/Dockerfile -t cloud-edge/edge-service .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

COPY requirements-moment.txt ./requirements-moment.txt
# momentfm 以 git+https 形式安装，镜像内需要 git。
# PIP_INDEX_URL / PIP_EXTRA_INDEX_URL 可在构建时覆盖为镜像源（如国内网络环境）。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=""
ARG EDGE_BUILD_REVISION=unknown
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git \
    && git config --system http.version HTTP/1.1 \
    && rm -rf /var/lib/apt/lists/*

# 先缓存 PyPI 依赖（包括 Torch/CUDA）；MOMENT 的 GitHub 安装失败时可复用此层。
RUN sed '/^momentfm[[:space:]]*@/d' requirements-moment.txt > /tmp/requirements-pypi.txt \
    && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} \
        $(if [ -n "${PIP_EXTRA_INDEX_URL}" ]; then echo "--extra-index-url ${PIP_EXTRA_INDEX_URL}"; fi) \
        --requirement /tmp/requirements-pypi.txt \
    && rm /tmp/requirements-pypi.txt

# 固定提交的 MOMENT 单独成层，GitHub 短暂失败后重试不会重新下载 Torch/CUDA。
RUN sed -n '/^momentfm[[:space:]]*@/p' requirements-moment.txt > /tmp/requirements-moment-git.txt \
    && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} \
        $(if [ -n "${PIP_EXTRA_INDEX_URL}" ]; then echo "--extra-index-url ${PIP_EXTRA_INDEX_URL}"; fi) \
        --requirement /tmp/requirements-moment-git.txt \
    && rm /tmp/requirements-moment-git.txt

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
    EDGE_CLOUD_REVIEW_CACHE_DIR=/app/data/edge_cloud_review \
    EDGE_RAW_SAMPLE_DIRECTORY=/app/data/raw_analysis_samples \
    EDGE_PACKET_ROUTE_ERROR_LOG=/app/data/edge_packet_route_errors.jsonl \
    EDGE_MODEL_ROOT=/app/data/models \
    EDGE_MODEL_UPDATE_POLLER_ENABLED=false \
    EDGE_BUILD_REVISION=${EDGE_BUILD_REVISION}

RUN groupadd --system --gid 10001 edge \
    && useradd --system --uid 10001 --gid edge --home-dir /nonexistent \
        --shell /usr/sbin/nologin edge \
    && mkdir -p /app/data \
    && chown -R edge:edge /app/data

EXPOSE 8001 8011

USER 10001:10001

# 阶段 5：容器健康探针使用 liveness（关键线程存活），
# readiness（/health/ready）用于调度与流量控制，短暂断连不应标记容器 unhealthy。
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8001/health/live', timeout=3)); assert data.get('alive', False)"]

CMD ["python", "edge_service/run_edge_service.py", "--host", "0.0.0.0", "--port", "8001"]
