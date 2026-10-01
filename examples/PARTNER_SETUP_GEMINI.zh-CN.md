# Gemini Research partner 安装

这套接入与 Claude Research 使用同一代码库，但 Antigravity CLI（`agy`）bridge 在 VPS 上独立
部署、独立服务账号、独立 SSH principal 和独立密钥。不要复用 Claude 的密钥，也不要
把私钥提交到仓库。

把以下内容和 owner 预授权的 partner 私钥通过一对一私密渠道交给 partner。仓库
通过仓库安装时，需要先授予 partner Read 权限：

```text
请使用 $skill-installer 从
https://github.com/rapidhere121345/codex2calude-skill/tree/gemini-antigravity/skills/gemini-research
安装 gemini-research Skill。不要读取、展示、上传或转发我附带的私钥内容。
安装后在 Skill 目录运行：

python3 scripts/setup_client.py --principal partner \
  --import-key <我附带的私钥绝对路径> --verify

脚本必须显示 pre-authorized import，并验证恰好七个 gemini 工具。
验证成功后，用系统安全删除机制删除交付私钥和压缩包，并提醒我重启 Codex。
遇到已有密钥、host pin 或 gemini_research 配置冲突时停止，不要覆盖或绕过校验。
```

如果 partner 没有 `$skill-installer`，可以只复制
`skills/gemini-research` 目录到 `$CODEX_HOME/skills/gemini-research`。没有预授权
私钥时，脚本只能生成 `.pub` 并停止等待 owner 授权。

底层已改为官方 Antigravity CLI，命令仍叫 `gemini-research`，MCP 仍是
`gemini_*` 七个工具。真实登录和推理都在 VPS 使用 owner 的 Google OAuth
套餐；partner 只需 MCP 专用密钥，无需 Google 密码、OAuth 文件或 API key。
owner/partner 对话记录隔离，共享账号套餐额度，服务端并发为 1。
两人统一固定 Gemini Pro 的最高可用推理档，模型参数使用 `pro`。
Owner 必须在远端认证后核实具体模型 ID 和支持的最高档，再绑定服务端策略；
`gemini_status.model_selection_ready=false` 时停止，不能用 `auto`、Flash 或
其他模型代替。会话续聊保持创建时的具体模型与档位。额度耗尽直接报告；
Owner 应确认 AI Credit Overages 为 `Never`，仅 OAuth 登录不代表不会消耗超额 credits。

如果 owner 交付的是离线包，解压后将其中的 `gemini-research` 目录复制到
`$CODEX_HOME/skills/gemini-research`（未设置时为 `~/.codex/skills/gemini-research`），
然后运行同一个安装命令，`--import-key` 指向独立交付的私钥。
离线 Skill 包不包含私钥；不要将私钥上传 Git 或发到群聊。

验收要求：公网 TCP 22022 可达、恰好七个 MCP 工具、`principal=partner`、
远端 `logged_in=true`、`default_model=pro` 和 `model_selection_ready=true`。
仅 Skill 安装成功不表示账号认证和模型绑定已完成。公网接入复用已开放的 22022 SSH listener，按 geminimcp 用户路由到独立部署。
遇到连接超时请报告给 owner，不要改用无校验 SSH。
