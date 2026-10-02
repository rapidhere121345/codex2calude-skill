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
请使用 $skill-installer 从 https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/gemini-research 安装 gemini-research Skill，并阅读其中的 SKILL.md；不要等待或索取 Skill ZIP。安全解压随附的 gemini-partner-access.zip，将交付私钥文件权限设为 600，不查看、展示或上传私钥内容。在安装后的 Skill 目录运行 python3 scripts/setup_client.py --principal partner --import-key <交付私钥绝对路径> --verify。要求脚本报告 `pre-authorized import`、恰好七个 gemini 工具、principal=partner、availability.eligible=true、default_model=pro、model_selection_ready=true，并确认策略为 `gemini-3.1-pro-high` / `high`；空 usage metadata 不能证明当前调用的实际计费模型。随后通过宿主原生 `gemini_once` MCP 工具做一次小的离线 Gemini Pro 调用，参数为 model=pro、use_web=false、timeout_seconds=120，问题为“只回复 GEMINI_PRO_ACCEPTANCE_OK。”；若本会话尚未加载新注册的 MCP 工具，先确认注册与握手成功，再等待宿主工具目录刷新，不要重装或改用脚本/SSH 替代。核对实际回答并报告安装与真实调用结果；以后直接按 gemini-research 提问即可。需要 Python 3.11+ 和 OpenSSH，无需本地 Google 登录、Gemini/Antigravity CLI 或 API key。遇到密钥、host pin、配置冲突或远端不可用时停止并报告固定错误，不覆盖或换模型；验证成功后删除交付私钥副本及 access ZIP，保留已安装的专用密钥。
```

`setup_client.py` 只负责导入专用密钥、注册受限 MCP、pin 主机密钥，并在
`--verify` 时做只读 `gemini_status` 握手；它不提供推理调用入口。握手和状态
通过不代表第一次真实推理已被接受。安装后的所有提问、续聊、导出等操作都通过
宿主原生 `gemini_research` MCP 工具，并只使用 bridge 的 opaque conversation_id。

## 已授权 partner 的 Skill 升级

已经完成首次安装的 partner 不需要新的 access ZIP，也不应重打包、删除、输出
或读取既有专用私钥。让 Codex 用 `$skill-installer` 从 main 分支安装到新的
暂存目录（`--dest`），确认内容后将旧 Skill 备份到 skills 目录之外，再用新
目录替换旧目录。安装器不支持覆盖已有目标；直接叠加文件还可能留下旧版
`scripts/call_tool.py`。若此前覆盖安装已经遗留该文件，只清理这个精确路径，
不要泛化删除用户目录。随后先解析自己的 Codex home 真实目录（例如
`realpath "${CODEX_HOME:-$HOME/.codex}"`），再在更新后的 Skill 目录运行：

```sh
python3 scripts/setup_client.py --principal partner \
  --codex-home <解析后的codex-home绝对路径> --verify
```

该命令应识别已就位的专用密钥并复用现有 MCP 注册；除报告冲突外不得改写。
等待宿主加载更新后的七工具目录后，用原生 `gemini_status` 确认
`principal=partner`、`availability.eligible=true`、`default_model=pro`、
`model_selection_ready=true`，再用原生 `gemini_once` 传入
`model="pro"`、`use_web=false`、`timeout_seconds=120` 和提示
`只回复 GEMINI_PRO_ACCEPTANCE_OK。`，核对实际回答。

可直接交给已授权 partner 的 Codex 的升级指令如下；其中不包含也不需要新的
密钥包，私钥保持原位且不可读取或重新打包：

```text
请从 https://github.com/rapidhere121345/codex2calude-skill 的 main 分支安装或更新 skills/gemini-research，先阅读该 Skill 和 examples/PARTNER_SETUP_GEMINI.zh-CN.md。用 $skill-installer 下载到新的暂存目录；若已安装，先将旧 Skill 备份到 skills 目录之外，再用新目录替换，确认没有遗留 scripts/call_tool.py。复用本机已安装的 Gemini partner 专用密钥；如果只有此前收到的本地密钥文件，按 SKILL.md 导入即可，不要生成新密钥或索取新密钥包，不展示、上传或重新打包私钥。解析真实 CODEX_HOME，在安装后的 Skill 目录运行 python3 scripts/setup_client.py --principal partner --codex-home <真实目录> --verify；仅在尚未安装专用密钥时加 --import-key <此前收到的本地文件>。确认宿主加载七个 gemini_research 工具，用原生 gemini_status 检查 principal=partner、availability.eligible=true、default_model=pro、model_selection_ready=true；再用原生 gemini_once（model="pro"、use_web=false、timeout_seconds=120）要求“只回复 GEMINI_PRO_ACCEPTANCE_OK。”，实际收到该回答才算完成。工具暂未加载时等待刷新，必要时说明如何重新加载；不要改用脚本或 SSH 推理。缺少既有密钥、仓库权限或遇到配置冲突时报告具体缺项，保留现有配置。
```

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
