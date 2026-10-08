# Partner 客户端更新：空闲连接回收

本次更新保留原有专用 SSH key、固定服务器 host key 和七个 MCP 工具。
Claude、Gemini 均需使用新版 skill 内的 `scripts/mcp_ssh_reconnect.py`。

## 更新

1. 从已授权的私有仓库取得最新版本：`git pull --ff-only`。
2. 将已安装的 `claude-research`、`gemini-research` skill 备份到 skills 目录之外。
3. 用仓库 `skills/claude-research`、`skills/gemini-research` 的完整目录替换已安装副本。不要覆盖或删除 `.ssh` 中的密钥及 host pin，不需要再次传输私钥。
4. 在**安装后的目录**运行以下命令；不要在临时 checkout 中注册将来会删除的脚本路径：

```sh
python3 scripts/setup_client.py --principal partner --verify
```

两个 skill 分别运行一次。若 Codex home 是符号链接，给命令加 `--codex-home`，值为它解析后的真实目录。不要再次使用 `--import-key`。

安装器只迁移可识别的旧配置，并保存备份。遇到自定义 command、SSH 目标、identity、host pin 或工具清单不匹配时会停止；不要通过重新生成 key 或关闭主机校验来解决。

5. 重启承载 MCP 的客户端/会话，让进程加载新版脚本。只替换文件不会升级已经运行的代理。Docker 部署需同步实际挂载的 skill/代理文件后重启对应容器。
6. 查看 MCP 状态和七个工具是否可用。旧连接失效或调用结果未知时，不要盲目重试带副作用的调用。

## 行为

- 空闲 30 分钟后关闭 SSH 和远端 worker，本地 MCP 端点保留。
- 下一个真实请求才重连；只重放 initialize/initialized，不重放结果未知的 tools/call。
- 正在执行或排队的请求不会被空闲定时器杀掉。
- 协议 ping 在本地回答，不会续命或唤醒远端连接。
- 服务器也有 30 分钟空闲保护，但旧版主动重连客户端仍会重新建立连接，因此 partner 必须更新客户端。

## 账号和隐私

这是连接生命周期修复，不改变模型、订阅或账号权限，也不保证共享订阅符合提供商规则。审计页面只给 owner 访问，不分发登录凭证或原始对话内容。双方共享账号的条款/地区风险需要单独处理，详见安全审计文档。
