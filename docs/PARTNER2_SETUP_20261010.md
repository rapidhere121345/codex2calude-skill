# partner2 接入

本仓库托管 Claude/Gemini MCP Skill、客户端安装脚本和服务端源码。`partner2` 私钥包单独通过受控下载链接交付，不进入 GitHub。

拿到密钥包后，在解压目录执行：

```sh
python3.11 claude-research/scripts/setup_client.py \
  --principal partner2 \
  --import-key keys/claude_mcp_partner2_ed25519 \
  --verify

python3.11 gemini-research/scripts/setup_client.py \
  --principal partner2 \
  --import-key keys/gemini_mcp_partner2_ed25519 \
  --verify
```

然后将 `claude-research` 和 `gemini-research` 安装到 Codex 的 `skills` 目录。两个验证都通过后，安全删除解压目录和私钥源文件。
