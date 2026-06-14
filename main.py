#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Main application entry point - OpenAI format conversion
"""

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.openai_api import router as openai_router

# Create FastAPI app
app = FastAPI(
    title="OpenAI Compatible API Server",
    description="OpenAI-compatible API server for Z.AI chat service",
    version="1.0.0-dev",
)


@app.on_event("startup")
async def startup_event():
    """Startup event handler to pre-heat the WAF Token bridge"""
    import asyncio
    try:
        from src.waf_bridge import waf_token_bridge
        from src.helpers import info_log, error_log
        # 异步启动并热身浏览器，不阻塞 FastAPI 启动
        asyncio.create_task(waf_token_bridge.get_waf_token())
        info_log("[CDP] 成功启动 WAF Token 桥接后台热身任务")
    except Exception as e:
        from src.helpers import error_log
        error_log(f"[CDP] 启动 WAF Token 桥接后台热身任务失败: {e}")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Include API router
app.include_router(openai_router)


@app.options("/")
async def handle_options():
    """Handle OPTIONS requests"""
    return Response(status_code=200)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "OpenAI Compatible API Server",
        "version": "1.0.0-dev",
        "description": "OpenAI格式转换功，含工具调用"
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy"}


@app.on_event("shutdown")
def shutdown_event():
    """Shutdown event handler to clean up background processes"""
    try:
        from src.waf_bridge import waf_token_bridge
        waf_token_bridge.clean_up()
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    import os
    import platform
    import multiprocessing

    if platform.system() == "Windows":
        workers = 1
    else:
        # (2 × CPU核心数) + 1，环境变量可覆盖
        cpu_count = multiprocessing.cpu_count()
        default_workers = (2 * cpu_count) + 1
        workers = int(os.getenv("UVICORN_WORKERS", str(default_workers)))

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.LISTEN_PORT,
        workers=workers,
        http="httptools",
        reload=False,
        log_level="info",
    )

