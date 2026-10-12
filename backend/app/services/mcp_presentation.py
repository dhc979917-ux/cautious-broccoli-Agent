"""Safe connection labels for the read-only MCP directory."""

import json
from urllib.parse import urlsplit, urlunsplit

from app.services.mcp_credentials import has_inline_credentials


def connection_label(server):
    if server.type == 'stdio':
        try:
            args = json.loads(server.args_json or '[]')
        except (ValueError, TypeError):
            args = []
        if not isinstance(args, list) or any(not isinstance(argument, str) for argument in args) or (
                has_inline_credentials(server.command, args, None)):
            return '本地（连接参数已隐藏）'
        command = ' '.join([server.command or '', *args]).strip()
        return f'本地（{command}）' if command else '本地（未配置命令）'
    parsed = urlsplit(server.url or '')
    host = parsed.netloc.rsplit('@', 1)[-1]
    address = urlunsplit((parsed.scheme, host, parsed.path, '', ''))
    return f'远程（{address}）' if address else '远程（未配置地址）'
