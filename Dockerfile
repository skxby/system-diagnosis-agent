# Log Intelligence Agent V1 —— 应用镜像（web / worker / beat 共用）
#
# **镜像内不含任何密钥**（计划第 1006 行）：密钥只在运行时以环境变量注入，
# 且 .dockerignore 挡住了 .env 与 .credentials.yaml。
#
# 不用 Alpine：psycopg[binary] 在 musl 上需要额外编译工具链，
# 而本镜像只在单机跑，体积不是首要目标（计划第 1008 行：不上 K8s、不做多环境）。

FROM python:3.13-slim AS base

# Python 运行参数：
#   PYTHONDONTWRITEBYTECODE  不写 .pyc（容器里没有意义，还会让层变大）
#   PYTHONUNBUFFERED         日志实时输出，否则 docker logs 会攒着不显示
#   PIP_NO_CACHE_DIR         不在镜像里留 pip 缓存
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖单独一层：源码改动不会让依赖重装
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt && pip check

# 再拷源码。注意 .dockerignore 已排除 .env / .credentials.yaml / .venv 等
COPY . .

# 以非 root 运行：容器逃逸时的影响面更小
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/uploads /app/data \
    && chown -R appuser:appuser /app
USER appuser

# 默认起 web；worker / beat 在 compose 里覆盖 command
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
