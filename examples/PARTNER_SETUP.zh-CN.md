# Partner 接入 Claude Research MCP

这个入口直接走公网 SSH stdio，不需要安装或加入 Tailscale。它只允许调用
Claude Research MCP，不提供服务器 Shell、文件访问、桌面或端口转发。

## 1. 在你自己的电脑生成独立密钥

Linux/macOS：

```sh
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/claude_mcp_partner_ed25519
```

不要把私钥 `~/.ssh/claude_mcp_partner_ed25519` 发给任何人。只把下面这个
公钥文件交给管理员：

```text
~/.ssh/claude_mcp_partner_ed25519.pub
```

## 2. 固定服务器主机密钥

管理员确认你的公钥已加入后，创建
`~/.ssh/claude_mcp_known_hosts`，内容必须是：

```text
[44.255.229.140]:22022 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOposESz9sift7F1deDCPoJZucWQ69k1Ln4b1auELh5s
```

主机 ED25519 指纹应为：

```text
SHA256:YVHHRs5TyOc5v1vkGZ40vW6jaDyULwM8LQeywygLH/Q
```

如果 SSH 显示其他指纹或提示主机密钥变化，请停止连接并联系管理员。

## 3. 加入 Codex 配置

把以下内容加入 `~/.codex/config.toml`；Windows 用户把路径替换成自己电脑上
私钥和 `known_hosts` 文件的绝对路径。

```toml
[mcp_servers.claude_research]
command = "ssh"
args = [
  "-T",
  "-p", "22022",
  "-i", "/HOME/PARTNER/.ssh/claude_mcp_partner_ed25519",
  "-o", "BatchMode=yes",
  "-o", "IdentitiesOnly=yes",
  "-o", "StrictHostKeyChecking=yes",
  "-o", "UserKnownHostsFile=/HOME/PARTNER/.ssh/claude_mcp_known_hosts",
  "-o", "ConnectTimeout=10",
  "-o", "ServerAliveInterval=30",
  "-o", "ServerAliveCountMax=2",
  "ubuntu@44.255.229.140",
]
startup_timeout_sec = 20
tool_timeout_sec = 720
enabled_tools = [
  "claude_status",
  "claude_once",
  "claude_start",
  "claude_continue",
  "claude_close",
]
```

重启 Codex 后调用 `claude_status`。应看到：

- `principal`: `partner`
- `logged_in`: `true`
- `subscription_type`: `pro`
- 当前允许模型只有 `opus`

## 4. 会话用法

- 独立问题：`claude_once`
- 新任务：`claude_start`，保存返回的 `conversation_id`
- 继续任务：`claude_continue`，传入同一个 `conversation_id`
- 结束映射：`claude_close`

每个逻辑任务使用一个会话。Owner 和 Partner 的会话互相不可访问；两人共用
服务器上的 Claude 登录，因此同时请求会排队。
