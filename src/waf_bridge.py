#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
WAF Token 桥接模块 - 控制后台 Headless Chrome 动态获取 completions 的 WAF Token (captcha_verify_param)
"""

import os
import sys
import json
import time
import random
import asyncio
import subprocess
import platform
try:
    import winreg
except ImportError:
    winreg = None

from typing import Optional, Any
import httpx
import websockets

from .config import settings
from .helpers import info_log, error_log, debug_log


class WAFTokenBridge:
    """
    CDP 远程调试桥接类
    负责查找、启动并控制 Headless Chrome 提取 ZAI completions 的未核销 WAF Token
    """
    
    _instance: Optional['WAFTokenBridge'] = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
        
    def __init__(self):
        if self._initialized:
            return
        self.chrome_process: Optional[subprocess.Popen] = None
        self.port = 19222  # 使用 19222 端口，避免与系统已有的 9222 端口冲突
        self.ws_url: Optional[str] = None
        self.user_data_dir = os.path.join(
            os.path.expanduser("~"), 
            ".cache", 
            "zai-bridge-profile"
        )
        self._initialized = True

    def _get_chrome_path(self) -> str:
        """从注册表或常规路径查找 Chrome 安装路径"""
        import platform
        system = platform.system()
        
        if system == "Windows":
            if winreg is not None:
                for hkey in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                            path, _ = winreg.QueryValueEx(key, "")
                            if path and os.path.exists(path):
                                return path
                    except Exception:
                        continue
                        
            common_paths = [
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                os.path.expandvars("%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe")
            ]
            for p in common_paths:
                if os.path.exists(p):
                    return p
            raise RuntimeError("Chrome executable not found on Windows.")
        elif system == "Linux":
            common_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/chrome",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/local/bin/google-chrome",
            ]
            for p in common_paths:
                if os.path.exists(p):
                    return p
            raise RuntimeError("Chrome/Chromium executable not found on Linux.")
        else:
            raise RuntimeError(f"Unsupported operating system: {system}")

    async def _ensure_chrome_running(self):
        """确保开启了远程调试的 Chrome 正在运行"""
        # 1. 检查端口是否已经有 Chrome 在运行
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{self.port}/json", timeout=2.0)
                if resp.status_code == 200:
                    debug_log(f"[CDP] 发现已在端口 {self.port} 运行 of Chrome 实例")
                    return
        except Exception:
            pass

        # 2. 如果没有，则冷启动一个新的 Chrome 实例
        chrome_path = self._get_chrome_path()
        info_log(f"[CDP] 未发现运行 of Chrome，正在冷启动 Chrome: {chrome_path}")
        
        args = [
            chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--headless=new",  # 默认使用全新 Headless 模式运行，用户无感知
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        
        # 针对 Linux/Docker 环境，额外添加必需的安全和沙箱配置，以防在容器内启动崩溃
        if platform.system() == "Linux":
            args.extend([
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox"
            ])
            
        args.append("https://chat.z.ai/")
        
        # 启动后台进程
        self.chrome_process = subprocess.Popen(
            args, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        
        # 等待端口响应
        for attempt in range(1, 15):
            await asyncio.sleep(0.5)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{self.port}/json", timeout=1.0)
                    if resp.status_code == 200:
                        info_log(f"[CDP] 后台 Chrome 成功在端口 {self.port} 启动")
                        return
            except Exception:
                pass
        raise RuntimeError("Failed to start and connect to Chrome debugging port.")

    async def _get_ws_url(self) -> str:
        """获取或创建 ZAI 页面的 WebSocket 调试 URL"""
        await self._ensure_chrome_running()
        
        # 最多尝试 5 次获取 ZAI 页面
        for attempt in range(1, 6):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{self.port}/json", timeout=3.0)
                    targets = resp.json()
                    
                    # 寻找已打开 of chat.z.ai 页面
                    for t in targets:
                        url = t.get("url", "")
                        if "chat.z.ai" in url and t.get("webSocketDebuggerUrl"):
                            return t.get("webSocketDebuggerUrl")
                    
                    # 如果有调试连接但未打开 ZAI 页面，就尝试新建一个 Tab 打开 ZAI 主页
                    info_log("[CDP] 未发现 chat.z.ai 页面，正在新建页面...")
                    resp_new = await client.get(
                        f"http://127.0.0.1:{self.port}/json/new?url=https://chat.z.ai/", 
                        timeout=5.0
                    )
                    try:
                        target = resp_new.json()
                        ws_url = target.get("webSocketDebuggerUrl")
                        if ws_url:
                            return ws_url
                    except Exception:
                        pass
            except Exception as e:
                debug_log(f"[CDP] 尝试获取 WebSocket 接口失败，第 {attempt} 次重试...", error=str(e))
                
            await asyncio.sleep(1.0)
            
        raise RuntimeError("Failed to obtain ZAI WebSocket URL after multiple attempts.")

    async def _send_cdp_cmd(self, ws: websockets.WebSocketClientProtocol, method: str, params: dict) -> dict:
        """向 WebSocket 发送 CDP 指令并阻塞等待响应"""
        cmd_id = random.randint(1, 1000000)
        payload = {
            "id": cmd_id,
            "method": method,
            "params": params
        }
        await ws.send(json.dumps(payload))
        
        while True:
            resp_str = await ws.recv()
            resp = json.loads(resp_str)
            if resp.get("id") == cmd_id:
                return resp.get("result", {})

    async def _evaluate_js(self, ws: websockets.WebSocketClientProtocol, expr: str) -> Any:
        """在页面 Context 执行 JS"""
        res = await self._send_cdp_cmd(ws, "Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True
        })
        exception_details = res.get("exceptionDetails")
        if exception_details:
            error_log(f"[CDP] JS 执行报错: {exception_details}")
            raise RuntimeError(f"JS Eval Error: {exception_details.get('exception', {}).get('description')}")
        return res.get("result", {}).get("value")

    async def get_token_from_pool(self) -> str:
        """从 tokens.txt 中读取当前有效的 JWT Token"""
        from .token_pool import get_token_pool
        pool = await get_token_pool()
        return await pool.get_token()

    async def get_waf_token(self) -> str:
        """通过 CDP 控制浏览器模拟提问获取未核销的 WAF Token"""
        async with self._lock:
            ws_url = await self._get_ws_url()
            current_jwt = await self.get_token_from_pool()
            
            # 临时清空代理环境变量，防止 localhost 连接走代理导致卡死
            proxy_keys = ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']
            saved_proxies = {}
            for key in proxy_keys:
                if key in os.environ:
                    saved_proxies[key] = os.environ[key]
                    del os.environ[key]
            
            try:
                async with websockets.connect(ws_url) as ws:
                    # 0. 确保当前页面在 chat.z.ai 域名下，防止空白页跨域安全报错导致 LocalStorage 操作失败
                    url = await self._evaluate_js(ws, "window.location.href")
                    if "chat.z.ai" not in url or "/c/" in url:
                        info_log(f"[CDP] 当前页面为 {url}，正在导航至 Z.ai 首页重置状态")
                        await self._send_cdp_cmd(ws, "Page.navigate", {"url": "https://chat.z.ai/"})
                        for _ in range(100):
                            await asyncio.sleep(0.1)
                            state = await self._evaluate_js(ws, "document.readyState")
                            if state == "complete":
                                break
                        # 额外等待 2.5 秒，确保首页 React 挂载并准备好接受输入点击事件
                        await asyncio.sleep(2.5)
                                
                    # 1. 检查登录态 Token 是否存在 (采用 try-catch 容错)
                    token_expr = """
                    (function() {
                      try {
                        return window.localStorage.getItem('token');
                      } catch(e) {
                        return 'security_error';
                      }
                    })()
                    """
                    browser_jwt = await self._evaluate_js(ws, token_expr)
                    
                    if browser_jwt == 'security_error':
                        await asyncio.sleep(1.0)
                        browser_jwt = await self._evaluate_js(ws, token_expr)
                    
                    # 优化：只要浏览器 LocalStorage 中已有任意 Token 且不为 security_error，说明已经是登录状态
                    # 只有在完全没有 Token (即为空) 时，才去同步写入 current_jwt 并刷新页面，防止每次请求无谓地刷新导致超时
                    if not browser_jwt or browser_jwt == 'security_error':
                        info_log("[CDP] 发现浏览器未登录，正在写入 Token 并同步刷新")
                        set_token_expr = """
                        (function() {
                          try {
                            window.localStorage.setItem('token', %s);
                            return 'ok';
                          } catch(e) {
                            return 'write_error';
                          }
                        })()
                        """ % json.dumps(current_jwt)
                        write_res = await self._evaluate_js(ws, set_token_expr)
                        if write_res == 'ok':
                            # 强制重定向到 chat.z.ai 根目录以激活登录态，不使用 location.reload() 避免卡在 /login
                            await self._evaluate_js(ws, "window.location.href = 'https://chat.z.ai/'")
                            # 等待页面加载完成
                            for _ in range(100):
                                await asyncio.sleep(0.1)
                                state = await self._evaluate_js(ws, "document.readyState")
                                if state == "complete":
                                    break
                            # 额外等待 2.5 秒以待 React 初始化挂载
                            await asyncio.sleep(2.5)
                        else:
                            error_log("[CDP] 写入 localStorage Token 失败")
                    
                    # 1.5 再次检查以防冷启动后页面卡在 /login 等页面上
                    current_url = await self._evaluate_js(ws, "window.location.href")
                    if "/login" in current_url:
                        info_log("[CDP] 发现页面处于登录页，强制跳转回主页")
                        await self._evaluate_js(ws, "window.location.href = 'https://chat.z.ai/'")
                        for _ in range(50):
                            await asyncio.sleep(0.1)
                            state = await self._evaluate_js(ws, "document.readyState")
                            if state == "complete":
                                break
                        # 额外等待 2.5 秒以待登录状态切换和首屏就绪
                        await asyncio.sleep(2.5)
                                
                    # 2. 注入 completions fetch 劫持拦截脚本
                    patch_js = """
                    (function() {
                      if (!window.__fetch_patched) {
                        window.__fetch_patched = true;
                        const originalFetch = window.fetch;
                        window.__last_captcha_token = null;
                        
                        window.fetch = async function(...args) {
                          const url = args[0];
                          const options = args[1] || {};
                          
                          if (typeof url === 'string' && url.includes('/api/v2/chat/completions')) {
                            try {
                              const body = JSON.parse(options.body);
                              if (body.captcha_verify_param) {
                                window.__last_captcha_token = body.captcha_verify_param;
                              }
                            } catch (e) {}
                            
                            return new Promise((resolve) => {
                              const mockStream = new ReadableStream({
                                start(controller) {
                                  controller.enqueue(new TextEncoder().encode('data: [DONE]\\n\\n'));
                                  controller.close();
                                }
                              });
                              resolve(new Response(mockStream, {
                                status: 200,
                                headers: { 'Content-Type': 'text/event-stream' }
                              }));
                            });
                          }
                          return originalFetch.apply(this, args);
                        };
                        return 'Fetch patch installed successfully';
                      }
                      return 'Fetch patch already installed';
                    })()
                    """
                    await self._evaluate_js(ws, patch_js)
                    
                    # 3. 驱动页面输入并发送提问，触发防风控逻辑生成 WAF Token
                    send_js = """
                    (function() {
                      if (window.__last_captcha_token) {
                        return 'already_has_token';
                      }
                      
                      // 只匹配真正的聊天输入框 (ID 为 chat-input 或 textarea)，不匹配登录输入框
                      const input = document.querySelector('#chat-input') || document.querySelector('textarea');
                      if (!input) return 'no_input';
                      
                      input.value = 'test_completions';
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      
                      // 匹配真正的聊天发送按钮，支持 ID 缺失情况
                      let btn = document.getElementById('send-message-button') || 
                                document.querySelector('button.bg-black') ||
                                document.querySelector('.flex.justify-center.items-center.p-2.bg-black');
                      
                      // 兜底：如果通过常规选择器未找到，则在输入框父级链中寻找
                      if (!btn && input) {
                        let p = input.parentElement;
                        for (let i = 0; i < 5 && p; i++) {
                          const found = p.querySelector('button.bg-black') || 
                                        p.querySelector('button[type="submit"]') ||
                                        p.querySelector('button.rounded-full');
                          if (found) {
                            btn = found;
                            break;
                          }
                          p = p.parentElement;
                        }
                      }
                      
                      if (!btn) return 'no_btn';
                      
                      btn.click();
                      return 'clicked';
                    })()
                    """
                    
                    # 循环等待输入框渲染出来并成功点击发送
                    send_status = None
                    for attempt in range(1, 50):  # 最多等 5 秒
                        send_status = await self._evaluate_js(ws, send_js)
                        if send_status in ("clicked", "already_has_token"):
                            break
                        # 如果没有检测到输入框且页面还是在 /login，强制再次跳转
                        if send_status == "no_input":
                            cur_url = await self._evaluate_js(ws, "window.location.href")
                            if "/login" in cur_url:
                                await self._evaluate_js(ws, "window.location.href = 'https://chat.z.ai/'")
                        await asyncio.sleep(0.1)
                    debug_log(f"[CDP] 发送模拟请求状态: {send_status}")
                    
                    # 4. 轮询提取 window.__last_captcha_token 的内容
                    get_token_js = """
                    (function() {
                      const tk = window.__last_captcha_token;
                      if (tk) {
                        window.__last_captcha_token = null; // 消费后立刻清空
                        return tk;
                      }
                      return null;
                    })()
                    """
                    
                    for attempt in range(1, 150):  # 最多等 15 秒
                        await asyncio.sleep(0.1)
                        waf_token = await self._evaluate_js(ws, get_token_js)
                        if waf_token:
                            debug_log(f"[CDP] 成功拦截获取到未核销 WAF Token (尝试次数: {attempt})")
                            return waf_token
                        
                        # 优化：如果在轮询中每隔 3.0 秒（即 30 次尝试）仍未获取到 Token，说明上次点击可能由于 React 未加载完毕而失效
                        # 此时自动重新输入并点击发送以实现自愈
                        if attempt % 30 == 0:
                            debug_log(f"[CDP] 轮询 {attempt} 次未拿到 Token，尝试重新发送提问")
                            retry_status = await self._evaluate_js(ws, send_js)
                            debug_log(f"[CDP] 重新发送模拟请求状态: {retry_status}")
                            
                    raise RuntimeError("CDP WAF Token acquisition timed out (15s).")
            finally:
                # 恢复代理环境变量
                for key, val in saved_proxies.items():
                    os.environ[key] = val

    def clean_up(self):
        """释放后台 Chrome 进程"""
        if self.chrome_process:
            try:
                self.chrome_process.terminate()
                self.chrome_process.wait(timeout=2.0)
                info_log("[CDP] 成功关闭后台 Chrome 进程")
            except Exception as e:
                error_log(f"[CDP] 关闭 Chrome 进程失败: {e}")
            self.chrome_process = None


# 全局单例
waf_token_bridge = WAFTokenBridge()
