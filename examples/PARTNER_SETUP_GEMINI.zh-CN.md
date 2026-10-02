# Gemini Research partner 安装

与 Claude Research 共用代码库；VPS 上分别部署，客户端都使用 MCP + Skill。
公网传输复用 TCP 22022，partner 私钥仅授权受限 MCP forced command。
Google OAuth、Antigravity CLI 和所有真实推理均留在 VPS。
Google credentials、tokens、keys 不交付给 partner，无需本地 provider 登录。

2026-10-02 自测验收已完成：官方 `agy` 1.2.14 个人 OAuth 已 eligible，Owner 和
干净环境中实际安装的 partner 客户端均通过真实 MCP 调用、续聊、列表、导出、关闭
及跨 principal 拒绝检查；VPS 命令、文件写入和官方网页读取通过。生产策略将逻辑
模型 `pro` 绑定到 `gemini-3.1-pro-high` / `high`，公网仍复用 22022。Owner 私下
交付专用 access ZIP；不要把空 usage metadata 解读为当前调用的实际计费模型。

标准安装流程从 GitHub `main` 获取 `gemini-research` Skill，**不需要 Skill ZIP**。
Owner 只通过一对一受控渠道交付这位 partner 专用的
`gemini-partner-access.zip`；它含私密凭据，不进入代码库。安装地址：
https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/gemini-research
仅 OAuth 或模型目录可读不代表可用：地区资格、模型绑定和真实推理必须分别通过。

将下面这段话与 `gemini-partner-access.zip` 一起交给 partner 的 Codex：

```text
请使用 $skill-installer 从 https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/gemini-research 安装 gemini-research Skill，并阅读其中的 SKILL.md；不要等待或索取 Skill ZIP。安全解压随附的 gemini-partner-access.zip，将交付私钥文件权限设为 600，不查看、展示或上传私钥内容。在安装后的 Skill 目录运行 python3 scripts/setup_client.py --principal partner --import-key <交付私钥绝对路径> --verify。要求脚本报告 `pre-authorized import`、恰好七个 gemini 工具、principal=partner、availability.eligible=true、default_model=pro、model_selection_ready=true，并确认策略为 `gemini-3.1-pro-high` / `high`；空 usage metadata 不能证明当前调用的实际计费模型。随后做一次小的离线 Gemini Pro 调用，问题为“只回复 GEMINI_PRO_ACCEPTANCE_OK。”；本会话若尚未加载 MCP 工具，就用 scripts/call_tool.py 调用同一个受限 MCP：--tool gemini_once --arguments-file <包含 model=pro、use_web=false、timeout_seconds=120 的 JSON 文件绝对路径> --prompt-stdin，并通过 stdin 输入问题。核对实际回答并报告安装与真实调用结果；以后直接按 gemini-research 提问即可。需要 Python 3.11+ 和 OpenSSH，无需本地 Google 登录、Gemini/Antigravity CLI 或 API key。遇到密钥、host pin、配置冲突或远端不可用时停止并报告固定错误，不覆盖或换模型；验证成功后删除交付私钥副本及 access ZIP，保留已安装的专用密钥。
```

本对话可以直接使用打包的 `scripts/call_tool.py`，它只接受安装器写入的完整配置、
固定 endpoint、专用密钥和 host pin；握手后核验七个工具，再转发一次 MCP 调用。
重新启动 Codex 后，优先用原生 `gemini_research` MCP 工具。
helper 支持 `--codex-home`；它不接受 `--principal`，身份由专用 key 决定。
续聊、导出等参数通过 `--arguments-file` 传递，只使用 bridge 的 opaque conversation_id。

GitHub 安装要求 partner 已拥有私有仓库 Read 权限。若主线 GitHub 安装暂不可用，
Owner 可另行安排私下传输 Skill 目录作为非标准备用引导；这不改变 canonical
流程，也不意味着需要把 Skill ZIP 与 access ZIP 一起交付。

MCP 会话映射与查询按 principal 过滤；Owner 与 partner 共享套餐额度和一个服务端并发槽。
可信 Owner/partner 可以让 Gemini 为明确任务使用远端文件、命令等普通工具。
Bridge 只校验 CLI init 格式及必要 web 能力，不检查工具目录 subset；实际工具
执行由 VPS 独立配置 `always-proceed` 授权，不再设普通工具执行白名单。
`use_web=true` 选择 web 研究模式，`false` 请求不做 web 研究，不能当作 web/file/shell
的强技术隔离。初始工具目录不证明有效权限或工具上下文裁剪。
两者使用同一 VPS 服务账户，开放 file/shell 后不具有文件系统强隔离；SSH key 本身
仍只进入七工具 MCP forced command。禁止 `agy` 派生其他 AI 代理/任务，或通过命令
切换 AI CLI/模型绕过固定 Pro；协作由外层 Codex native subagents 处理。

生产只允许逻辑模型 `pro`，具体模型和 effort 由 Owner 在 VPS 核对并绑定。
当前账号目录的最高 Pro 选择为 Gemini 3.1 Pro (High)，CLI ID 为
`gemini-3.1-pro-high`，使用 `--effort high`，Pro 选择器仅提供 Low 与 High。目录和固定参数并不能证明
账号已获服务资格，真实调用结果才是最后验收。
会话续聊保持创建时的具体模型与 effort 快照。

`availability.status=unsupported_location` 时，由 Owner 检查 Google 条款页的账号
关联国家及官方 FAQ；partner 安装器不会把它当成需要再次登录。
`useG1Credits=false` 已由部署设置且为默认值；账号 AI Credit Overages 为 `Never`
尚未确认，不能宣称用量仅限套餐基础额度。资格或额度错误直接停止，不自动改用 Flash、
其他供应商、API-key 或 Vertex 计费。
