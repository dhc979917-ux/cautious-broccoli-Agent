# Nginx 反向代理部署指南（裸机）

本指南面向 Linux 服务器裸机部署，不涉及 Docker。实现以下功能：

- Nginx 监听 80 端口作为统一入口
- upstream 定义后端地址，IP 变更只改一行
- IP 白名单 + basic_auth 双重访问控制
- 防火墙封堵后端端口，防止绕过代理
- 403 拦截防止暴露 Nginx 默认页
- proxy_set_header 传递真实客户端 IP

## 前置条件

- Linux 服务器（CentOS 7+ 或 Ubuntu 18.04+）
- 已安装或可安装 Nginx
- 后端应用运行在 127.0.0.1:8080（可改）
- 前端已 npm run build，产物在 /opt/agent_platform/frontend/dist
- root 权限

## 一键部署（推荐）

```bash
# 将仓库中的 nginx/ 目录上传到服务器，然后执行:
chmod +x deploy_proxy.sh
./deploy_proxy.sh <用户名> <服务器公网IP>

# 示例:
./deploy_proxy.sh admin 203.0.113.50
```

脚本会自动完成安装依赖、创建密码文件、部署配置、配置防火墙、重载 Nginx。

## 手动部署（分步执行）

### 步骤 1: 安装 Nginx 和 htpasswd 工具

```bash
# CentOS / RHEL
yum install -y nginx httpd-tools
systemctl enable nginx

# Ubuntu / Debian
apt update -y
apt install -y nginx apache2-utils
systemctl enable nginx
```

### 步骤 2: 创建 basic_auth 密码文件

```bash
# 创建密码文件（-c 表示新建，仅首次使用）
htpasswd -c /etc/nginx/.htpasswd myuser
# 系统提示输入密码并确认

# 添加第二个用户（不要用 -c，否则会覆盖）
# htpasswd /etc/nginx/.htpasswd anotheruser

# 验证
cat /etc/nginx/.htpasswd
```

### 步骤 3: 部署 Nginx 配置

```bash
# 复制配置文件
cp agent_proxy.conf /etc/nginx/conf.d/agent_proxy.conf

# 替换服务器IP占位符（将 203.0.113.50 改成你的实际IP）
sed -i 's/<SERVER_PUBLIC_IP>/203.0.113.50/g' /etc/nginx/conf.d/agent_proxy.conf

# 移除默认配置避免冲突
mv /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.bak 2>/dev/null

# 测试配置语法
nginx -t

# 重载
systemctl reload nginx
```

### 步骤 4: 配置 firewalld 防火墙

```bash
# 开放 80 端口（Nginx 对外入口）
firewall-cmd --permanent --add-port=80/tcp

# 移除 8080 的公开访问（如果之前开放过）
firewall-cmd --permanent --remove-port=8080/tcp 2>/dev/null

# 显式拒绝外部对 8080 的访问
# loopback 流量不经过 public zone，所以 127.0.0.1 访问不受影响
firewall-cmd --permanent --zone=public \
    --add-rich-rule='rule family="ipv4" port port="8080" protocol="tcp" reject'

# 重新加载使生效
firewall-cmd --reload

# 验证: ports 中有 80/tcp，rich-rules 中有 8080 reject
firewall-cmd --list-all
```

### 步骤 5: 额外加固（建议）

建议后端 uvicorn 绑定 127.0.0.1 而非 0.0.0.0，这样即使防火墙遗漏也无法被外部直接访问:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## 后端 IP 变更方法

只需修改配置文件中 upstream 块的 server 这一行:

文件路径: /etc/nginx/conf.d/agent_proxy.conf

```nginx
upstream backend_app {
    # !!! 后端IP变更时只需修改下面这一行 !!!
    server 127.0.0.1:8080;    # 改成新地址，如: server 10.0.1.100:8080;
}
```

修改后执行:

```bash
nginx -t && systemctl reload nginx
```

对外访问地址 http://<服务器公网IP> 完全不变。

## 验证清单

| 序号 | 测试项 | 预期结果 |
|------|--------|----------|
| 1 | nginx -t | 语法检查通过 |
| 2 | 从白名单IP访问 http://<公网IP> | 弹出认证框 |
| 3 | 输入正确用户名密码 | 前端页面正常加载 |
| 4 | 从非白名单IP访问 | 返回 403 Forbidden |
| 5 | 直接访问 http://<公网IP>:8080 | 防火墙拒绝（超时或拒绝） |
| 6 | 访问 http://<公网IP>/health | 后端健康检查响应（无需认证） |
| 7 | 登录后发起聊天 | WebSocket 连接正常 |
| 8 | 调用 API 接口 | 后端日志中 X-Forwarded-For 显示真实IP |

## 配置文件结构说明

| 配置块 | 作用 |
|--------|------|
| upstream backend_app | 定义后端地址，IP变更只改这里 |
| server (default_server) | 拦截未匹配请求返回403，防默认页泄露 |
| server (main) | 主业务server，处理认证+白名单+代理 |
| location / | 前端SPA静态文件 |
| location /api/v1/ws/ | WebSocket 代理（聊天） |
| location /api/ | REST API 代理 |
| location = /health | 健康检查（免认证） |

## 文件清单

| 文件 | 用途 |
|------|------|
| nginx/agent_proxy.conf | Nginx 配置文件（部署到 /etc/nginx/conf.d/） |
| nginx/deploy_proxy.sh | 一键部署脚本 |
| nginx/DEPLOY_PROXY.md | 本文档 |
| nginx/nginx.conf | Docker 部署用配置（不受本次改动影响） |
| deploy/nginx.conf | Docker 容器内部配置（不受本次改动影响） |

## 常见问题

Q: 访问 http://<IP> 返回 403?
A: 检查 server_name 是否正确设置了服务器公网IP。如果 server_name 不匹配请求的 Host 头，会落到 default_server 返回 403。

Q: basic_auth 认证框反复弹出?
A: 检查 /etc/nginx/.htpasswd 文件是否存在且格式正确。用 cat /etc/nginx/.htpasswd 验证。

Q: WebSocket 连接失败?
A: 确认 location /api/v1/ws/ 在 location /api/ 之前声明（前缀匹配取最长）。检查 proxy_set_header Upgrade 和 Connection 是否设置。

Q: 防火墙规则不生效?
A: 确认 firewalld 在运行: systemctl status firewalld。执行 firewall-cmd --reload 重新加载。用 firewall-cmd --list-all 验证规则。
