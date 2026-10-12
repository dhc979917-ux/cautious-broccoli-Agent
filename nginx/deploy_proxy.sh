#!/bin/bash
# ============================================================
# Agent 平台 Nginx 反向代理一键部署脚本
# 在 Linux 服务器上以 root 执行
#
# 用法:
#   chmod +x deploy_proxy.sh
#   ./deploy_proxy.sh <用户名> <服务器公网IP>
#
# 示例:
#   ./deploy_proxy.sh admin 203.0.113.50
# ============================================================
set -euo pipefail

AUTH_USER="${1:-admin}"
SERVER_IP="${2:-}"
NGINX_CONF_DIR="/etc/nginx/conf.d"
HTPASSWD_FILE="/etc/nginx/.htpasswd"
CONF_SRC="$(dirname "$0")/agent_proxy.conf"
CONF_DST="$NGINX_CONF_DIR/agent_proxy.conf"
FRONTEND_DIST="/opt/agent_platform/frontend/dist"

echo "=========================================="
echo "  Agent 平台 Nginx 反向代理部署"
echo "  认证用户: $AUTH_USER"
echo "  服务器IP: ${SERVER_IP:-未指定(需手动编辑配置)}"
echo "=========================================="

# --- 1. 安装依赖 ---
if command -v yum &>/dev/null; then
    echo "[1/7] 安装 nginx + httpd-tools (CentOS/RHEL)..."
    yum install -y nginx httpd-tools
elif command -v apt &>/dev/null; then
    echo "[1/7] 安装 nginx + apache2-utils (Ubuntu/Debian)..."
    apt update -y
    apt install -y nginx apache2-utils
else
    echo "[ERROR] 不支持的包管理器，请手动安装 nginx 和 htpasswd 工具"
    exit 1
fi

# --- 2. 创建 htpasswd 密码文件 ---
echo "[2/7] 创建 basic_auth 密码文件..."
if [ -f "$HTPASSWD_FILE" ]; then
    echo "  密码文件已存在，跳过创建。如需重建请先删除: rm $HTPASSWD_FILE"
else
    htpasswd -c "$HTPASSWD_FILE" "$AUTH_USER"
    echo "  密码文件已创建: $HTPASSWD_FILE"
fi

# --- 3. 确保前端构建产物目录存在 ---
echo "[3/7] 检查前端构建产物..."
if [ ! -d "$FRONTEND_DIST" ]; then
    echo "  [警告] $FRONTEND_DIST 不存在，请先部署前端构建产物"
    echo "  临时创建空目录以通过 nginx -t..."
    mkdir -p "$FRONTEND_DIST"
    echo "<html><body>前端未部署</body></html>" > "$FRONTEND_DIST/index.html"
else
    echo "  前端构建产物已就绪: $FRONTEND_DIST"
fi

# --- 4. 部署 nginx 配置 ---
echo "[4/7] 部署 nginx 配置..."
if [ ! -f "$CONF_SRC" ]; then
    echo "  [错误] 找不到配置文件: $CONF_SRC"
    exit 1
fi
cp "$CONF_SRC" "$CONF_DST"

# 替换服务器IP占位符
if [ -n "$SERVER_IP" ]; then
    sed -i "s/<SERVER_PUBLIC_IP>/$SERVER_IP/g" "$CONF_DST"
    echo "  服务器IP已设置为: $SERVER_IP"
else
    echo "  [警告] 未指定服务器IP，请手动编辑 $CONF_DST 替换 <SERVER_PUBLIC_IP>"
fi

# --- 5. 移除默认配置避免冲突 ---
echo "[5/7] 移除 nginx 默认配置..."
if [ -f "$NGINX_CONF_DIR/default.conf" ]; then
    mv "$NGINX_CONF_DIR/default.conf" "$NGINX_CONF_DIR/default.conf.bak"
    echo "  已备份: default.conf -> default.conf.bak"
else
    echo "  无默认配置需要移除"
fi

# --- 6. 配置 firewalld 防火墙 ---
echo "[6/7] 配置 firewalld 防火墙..."
if command -v firewall-cmd &>/dev/null; then
    # 开放 80 端口
    firewall-cmd --permanent --add-port=80/tcp
    # 移除 8080 的公开访问（如果之前开放过）
    firewall-cmd --permanent --remove-port=8080/tcp 2>/dev/null || true
    # 显式拒绝外部对 8080 的访问（loopback 不受影响）
    firewall-cmd --permanent --zone=public \
        --add-rich-rule='rule family="ipv4" port port="8080" protocol="tcp" reject' 2>/dev/null || true
    # 重新加载
    firewall-cmd --reload
    echo "  防火墙规则已配置 (80 开放, 8080 封堵)"
else
    echo "  [警告] firewalld 未安装，请手动配置防火墙"
fi

# --- 7. 测试并重载 nginx ---
echo "[7/7] 测试并重载 nginx..."
nginx -t
systemctl enable nginx
systemctl reload nginx 2>/dev/null || systemctl start nginx
echo ""
echo "=========================================="
echo "  部署完成!"
echo "=========================================="
echo ""
echo "验证步骤:"
echo "  1. 从白名单IP访问 http://${SERVER_IP:-<服务器IP>}"
echo "     应弹出认证框，输入 $AUTH_USER 和密码"
echo "  2. 直接访问 http://${SERVER_IP:-<服务器IP>}:8080"
echo "     应被防火墙拒绝"
echo "  3. 访问 http://${SERVER_IP:-<服务器IP>}/health"
echo "     应返回后端健康检查响应（无需认证）"
echo ""
echo "后端IP变更时:"
echo "  编辑 $CONF_DST"
echo "  修改 upstream 中的 server 行"
echo "  执行: nginx -t && systemctl reload nginx"
echo ""
