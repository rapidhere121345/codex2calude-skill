# Partner 接入 Claude Research MCP

这个入口直接走公网 SSH stdio，不需要安装或加入 Tailscale。它只允许调用
Claude Research MCP，不提供服务器 Shell、文件访问、桌面或端口转发。

## 首选：运行已安装 Skill 的自动配置

以下命令必须在已安装的 `claude-research` Skill 目录中运行：

```sh
python3 scripts/setup_client.py
```

脚本会生成本机专用密钥、固定服务器主机密钥，并把受限 MCP 配置写入你的
Codex 配置。它只会显示 `.pub` 公钥；不要读取、显示或发送私钥。把公钥交给
服务器管理员并等待授权，然后运行：

```sh
python3 scripts/setup_client.py --verify
```

验证通过后重启 Codex。若自动配置成功，不要再执行下面的手动 fallback，
以免产生两套密钥或配置。

## 手动 fallback

只有在无法运行已安装 Skill 的自动配置脚本时才使用以下步骤。

### Fallback 1. 在你自己的电脑生成独立密钥

Linux/macOS：

```sh
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/claude_mcp_partner_ed25519
```

不要把私钥 `~/.ssh/claude_mcp_partner_ed25519` 发给任何人。只把下面这个
公钥文件交给管理员：

```text
~/.ssh/claude_mcp_partner_ed25519.pub
```

### Fallback 2. 固定服务器主机密钥

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

### Fallback 3. 加入 Codex 配置

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
  "claude_list_conversations",
  "claude_export_conversation",
  "claude_continue",
  "claude_close",
]
```

重启 Codex 后调用 `claude_status`。应看到：

- `principal`: `partner`
- `logged_in`: `true`
- `subscription_type`: `pro`
- 当前允许模型只有 `opus`
- `opus_quota`: Opus 当前适用的 5 小时/7 天额度；如果 Claude 另行提供
  Opus 专属周额度，会显示在 `seven_day_opus`。字段为 `null` 代表未知，
  不代表尚未使用或额度无限；这里显示的是两人共用账户的额度，不是 partner
  独享额度

## 会话用法

- 独立问题：`claude_once`
- 新任务：`claude_start`（可给一个简短的人类可读标题），保存返回的
  `conversation_id`
- 跨 Codex 会话找回任务：先调用 `claude_list_conversations`，只从当前
  principal 的列表中选择 MCP `conversation_id`；不要询问或暴露 Claude
  原生 session ID
- 识别或复查任务：`claude_export_conversation` 会返回可读的用户/助手对话
  内容；只展示必要的摘录
- 继续任务：`claude_continue`，传入同一个 `conversation_id`
- 结束映射：`claude_close`；仅在该逻辑任务真正完成时使用。关闭不会删除
  Claude transcript，但当前契约会拒绝继续通过 MCP 续聊

每个逻辑任务使用一个会话。列表和导出都只作用于当前 SSH principal，
Owner 和 Partner 的会话互相不可访问；两人共用服务器上的 Claude 登录，
因此同时请求会排队。
