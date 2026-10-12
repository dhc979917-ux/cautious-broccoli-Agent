"""服务预览代理：跑在独立 sidecar 容器里的一段极简 TCP 转发脚本。

为什么是 sidecar 而不是后端直接转发：
  沙箱容器（as_ws_*）挂在 Docker 默认 bridge 上，后端容器挂在业务网络上，
  两者跨网桥被 DOCKER-ISOLATION 直接 DROP，后端根本连不到沙箱 IP。
  而宿主 iptables 对同一网桥内的转发是放行的（``-i docker0 -j ACCEPT``），
  所以把代理容器挂到"沙箱所在的那张网"上就能直连，再把代理容器的端口发布到宿主，
  用户即可用 http://<宿主>:<端口>/ 直接访问。

为什么是裸 TCP 而不是 nginx：
  Agent 起的服务可能是 HTTP、WebSocket、SSE 甚至非 HTTP 协议；裸字节转发对上层
  协议完全透明，不用改写 Location/Cookie path，也不用为每种框架调 base path。
  代价是没法在这一层做 HTTP 鉴权，因此预览的访问控制靠"不可枚举的高位端口 +
  到期自动摘除 + 并发上限"，并在提示词与页面上明确它是临时预览。

镜像：默认复用沙箱镜像（只要有 python3 就够），脚本用 ``python3 -u -c`` 内联注入，
不需要额外构建和分发镜像；要换成 nginx 等专用镜像时改 PREVIEW_PROXY_IMAGE 即可。
"""

# sidecar 容器监听端口（容器内），宿主端口由 preview_service 分配后映射到它
PROXY_LISTEN_PORT = 8080

# 目标不可达时返回的页面。用户点开的预览可能已经随沙箱回收而失效，
# 给一个明确的中文说明页，比浏览器的 ERR_CONNECTION_REFUSED 好得多。
_DOWN_PAGE_BODY = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#f4f5f7; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  .card { max-width:560px; margin:24px; padding:32px 36px; background:#fff; border:1px solid #e3e5e8;
          border-radius:8px; box-shadow:0 1px 2px rgba(16,24,40,.05); }
  h1 { margin:0 0 12px; font-size:20px; color:#1f2328; }
  p { margin:0 0 10px; font-size:14px; line-height:1.7; color:#4a5058; }
  code { background:#f2f3f5; border:1px solid #e3e5e8; border-radius:4px; padding:1px 6px;
         font-size:13px; color:#1f2328; }
  .tag { display:inline-block; margin-bottom:14px; padding:3px 10px; border-radius:999px;
         background:#fdf3f2; color:#b42318; font-size:12px; border:1px solid #f7c9c4; }
</style>
</head>
<body>
<div class="card">
  <span class="tag">__TAG__</span>
  <h1>__TITLE__</h1>
  <p>__REASON__</p>
  <p>服务预览是临时的：它跑在会话的隔离沙箱里，沙箱被回收后预览地址随即失效，平台不会长期托管它。</p>
  <p>要继续使用，请回到会话下载平台打包的源码压缩包，按包内 <code>HOW_TO_RUN.md</code> 的步骤在自己的环境里启动。</p>
</div>
</body>
</html>
"""

# 内联注入到 sidecar 容器的转发脚本（在容器内的 python3 里执行）
PROXY_SCRIPT = r'''
import asyncio
import os

TARGET = os.environ.get("TP_TARGET_HOST", "127.0.0.1")
TPORT = int(os.environ.get("TP_TARGET_PORT", "80") or 80)
LPORT = int(os.environ.get("TP_LISTEN_PORT", "8080") or 8080)
TITLE = os.environ.get("TP_PAGE_TITLE", "预览已结束")
TAG = os.environ.get("TP_PAGE_TAG", "PREVIEW UNAVAILABLE")
REASON = os.environ.get(
    "TP_PAGE_REASON",
    "该预览对应的会话沙箱已被回收，服务不再运行。",
)

BODY = (
    "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>" + TITLE + "</title>\n<style>\n"
    ":root{color-scheme:light dark}\n"
    "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    "background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
    "'Microsoft YaHei',sans-serif}\n"
    ".card{max-width:560px;margin:24px;padding:32px 36px;background:#fff;border:1px solid #e3e5e8;"
    "border-radius:8px;box-shadow:0 1px 2px rgba(16,24,40,.05)}\n"
    "h1{margin:0 0 12px;font-size:20px;color:#1f2328}\n"
    "p{margin:0 0 10px;font-size:14px;line-height:1.7;color:#4a5058}\n"
    "code{background:#f2f3f5;border:1px solid #e3e5e8;border-radius:4px;padding:1px 6px;"
    "font-size:13px;color:#1f2328}\n"
    ".tag{display:inline-block;margin-bottom:14px;padding:3px 10px;border-radius:999px;"
    "background:#fdf3f2;color:#b42318;font-size:12px;border:1px solid #f7c9c4}\n"
    "</style>\n</head>\n<body>\n<div class=\"card\">\n"
    "<span class=\"tag\">" + TAG + "</span>\n<h1>" + TITLE + "</h1>\n"
    "<p>" + REASON + "</p>\n"
    "<p>服务预览是临时的：它跑在会话的隔离沙箱里，沙箱被回收后预览地址随即失效，"
    "平台不会长期托管它。</p>\n"
    "<p>要继续使用，请回到会话下载平台打包的源码压缩包，按包内 <code>HOW_TO_RUN.md</code> "
    "的步骤在自己的环境里启动。</p>\n</div>\n</body>\n</html>\n"
)


def _down_page():
    body = BODY.encode("utf-8")
    head = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Length: " + str(len(body)) + "\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    # 平台标记头：preview_service 靠它区分"应用自己返回 503"和"沙箱内没有服务在监听"
    head = head.replace(
        "Cache-Control:", "X-Taskpilot-Preview: unreachable\r\nCache-Control:", 1
    )
    return head.encode("utf-8") + body


DOWN = _down_page()


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass


async def _close(writer):
    try:
        writer.close()
    except Exception:
        pass


async def handle(client_reader, client_writer):
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(TARGET, TPORT), timeout=5
        )
    except Exception:
        # 目标不可达：先尽量把客户端的请求读掉一点，避免浏览器在写请求体时收到 RST
        try:
            await asyncio.wait_for(client_reader.read(65536), timeout=0.5)
        except Exception:
            pass
        try:
            client_writer.write(DOWN)
            await client_writer.drain()
        except Exception:
            pass
        await _close(client_writer)
        return
    try:
        await asyncio.gather(
            _pipe(client_reader, up_writer),
            _pipe(up_reader, client_writer),
        )
    finally:
        await _close(up_writer)
        await _close(client_writer)


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LPORT)
    print("preview-proxy listening on 0.0.0.0:%d -> %s:%d" % (LPORT, TARGET, TPORT), flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
'''


def build_down_page(title: str, tag: str, reason: str) -> bytes:
    """本地渲染失效页（用于测试与文档核对；容器内由 PROXY_SCRIPT 自己渲染）。"""
    html = (
        _DOWN_PAGE_BODY
        .replace("__TITLE__", title or "预览已结束")
        .replace("__TAG__", tag or "PREVIEW UNAVAILABLE")
        .replace("__REASON__", reason or "")
    )
    return html.encode("utf-8")
