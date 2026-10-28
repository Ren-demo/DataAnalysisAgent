# ============================================================
# 后端镜像：FastAPI + LangGraph + pandas / Milvus 客户端
#
# 多阶段构建：
#   builder 装编译工具并按 requirements.txt 装依赖到独立 venv
#   runtime 只带 venv 与应用代码，不带 gcc / libpq-dev
#
# 构建（在项目根目录）：
#   docker build -t chongqing-ai-backend ./backend
# 或直接由 docker compose 构建。
# ============================================================

FROM python:3.13.18-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg2 / grpcio 等多数包有预编译 wheel；保留编译工具是为了在
# 某个平台缺 wheel 时仍能构建成功，而不是直接失败。
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./

# 依赖版本以 requirements.txt 的下限声明为准（项目不再维护锁文件）。
# python-docx / pypdf / tavily-python 三个可选依赖已包含在该文件中，
# 这样镜像具备完整能力，而不是「缺包静默降级」。
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.13.18-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH"

# tzdata 让容器内时间与日志走 Asia/Shanghai；libpq5 仅约 1MB，
# 为将来直接使用 psycopg2（非 binary 版）留出自洽的运行环境。
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tzdata libpq5 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app

# 非 root 运行。data/ 是 SQLite（uploads.db / docs.db）、上传文件与知识库的落盘位置，
# compose 里以命名卷挂载；首次挂载会继承这里的属主，从而保证 appuser 可写。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app /opt/venv
USER appuser

EXPOSE 8000

# proxy-headers：Node 反代会带 X-Forwarded-For/Proto，让 FastAPI 看到真实客户端信息。
# 单 worker 是刻意的：应用内有进程内缓存与后台任务，且全部是 async I/O，
# 多 worker 不会提升本场景的吞吐，反而会放大 Milvus / LLM 并发。
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]