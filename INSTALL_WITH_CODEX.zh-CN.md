# 交给另一个 Codex 完成安装

发送前，Owner 需要完成三件事：确认这种共享访问符合 Anthropic 的账号条款
且 Partner 位于支持地区；给 Partner 授予本私有仓库的 Read 权限；生成一把只
用于本 MCP 的 Partner 密钥，并提前把公钥绑定到服务器的 `partner`
forced-command。私钥是该 MCP 的 bearer credential，只能通过受控的一对一渠道
发送，绝不能放进 Git、公开链接或群聊。

把密钥附件和下面整段话一起交给 Partner：

```text
请使用 $skill-installer 从 https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/claude-research 安装 claude-research Skill。不要读取、展示、上传或转发我附带的私钥内容。安装后在 Skill 目录运行 python3 scripts/setup_client.py --principal partner --import-key <我附带的私钥绝对路径> --verify。脚本必须显示 pre-authorized import，并验证服务器恰好提供七个 claude 工具；成功后通过系统安全删除机制删除交付私钥和压缩包，重启 Codex，再调用 $claude-research。遇到已有密钥、host pin、MCP 配置冲突或主机指纹不符时立即停止，不要覆盖或绕过校验。
```

## 预期自动流程

`$skill-installer` 使用 Partner 自己的 GitHub/Git 权限读取私有仓库，不需要
Owner 交出 GitHub 凭证。Skill 中的 `setup_client.py` 会：

1. 要求 Python 3.11 或更新版本以及 OpenSSH 客户端。
2. 检查交付私钥是权限为 0600 的无口令 ED25519 常规文件。
3. 将它导入 Partner 本机的专用路径，并在本机派生公钥；不输出私钥。
4. 固定服务器 ED25519 主机密钥和指纹。
5. 仅在不存在冲突时更新 Codex 的 `claude_research` MCP 配置。
6. 通过公网 SSH 做真实 MCP 握手，确认登录状态与七个工具。

脚本故意保留交付源，直到 `--verify` 成功。成功后应删除源文件和交付压缩包；
导入到 `~/.ssh` 的专用工作密钥需要保留，删除它会让 MCP 失效。

## Fallback

如果 `$skill-installer` 不可用，但 Partner 有仓库读取权限，可以 clone 仓库，
只把 `skills/claude-research` 安装到
`$CODEX_HOME/skills/claude-research`，再运行相同命令。如果没有交付密钥，
`python3 scripts/setup_client.py` 会生成新密钥并只显示 `.pub`；此 fallback
仍需要把公钥交给 Owner 授权后再运行 `--verify`。

不要增加公网自助注册，不要复用 Owner 或通用登录密钥，也不要为了省一步而
关闭 host pin、`IdentitiesOnly` 或 forced-command 限制。
