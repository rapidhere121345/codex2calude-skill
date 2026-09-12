# 交给另一个 Codex 完成安装

把下面整段文字连同本仓库 URL 发给对方的 Codex：

```text
请使用 $skill-installer 从
https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/claude-research
安装 claude-research Skill。安装后遵循 SKILL.md 的 First-time client setup，
运行 setup_client.py 完成本机配置。只能向我显示新生成的 .pub 公钥，绝对不要
读取、显示或上传私钥。输出公钥后等待服务器管理员确认授权，再运行 --verify；
验证成功（服务器必须提供且只提供七个 claude 工具）后提醒我重启 Codex。
```

## 对方需要做什么

对方只需要：

1. 把上述指令交给自己的 Codex。
2. 将 Codex 输出的单行 `.pub` 公钥发给服务器管理员。
3. 管理员确认授权后告诉 Codex继续验证。
4. 验证通过（工具清单恰好为七个）后重启 Codex，再使用
   `$claude-research`。

私钥始终保留在对方电脑。不要通过聊天、网盘、邮件或仓库传递私钥。

如果仓库是私有仓库，对方还必须先拥有该 GitHub 仓库的读取权限。
