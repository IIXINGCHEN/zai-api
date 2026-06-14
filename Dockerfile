# ===== 构建阶段 =====
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== 运行阶段 =====
FROM python:3.13-slim

# 安装 Chromium 以及运行 headless 浏览器所需的依赖以支持 WAF Token 自动拦截
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /install /usr/local

COPY main.py .
COPY src/ ./src/

EXPOSE 8080
CMD ["python", "main.py"]