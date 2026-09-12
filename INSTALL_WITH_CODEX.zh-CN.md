# 交给另一个 Codex 完成安装

发送前，owner 必须先给 partner 授予本私有仓库的 Read 权限，并等 partner
确认自己能打开仓库 URL；确认后再把下面整段文字连同 URL 发给对方的 Codex：

```text
请使用 $skill-installer 从
https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/claude-research
安装 claude-research Skill。安装后遵循 SKILL.md 的 First-time client setup，
运行 setup_client.py 完成本机配置。只能向我显示新生成的 .pub 公钥，绝对不要
读取、显示或上传私钥。输出公钥后等待服务器管理员确认授权，再运行 --verify；
验证成功（服务器必须提供且只提供七个 claude 工具）后提醒我重启 Codex。
```

## 推荐安装路径：$skill-installer

上面的提示词会使用 `$skill-installer`。它会利用对方已有的 GitHub/Git
权限安装私有仓库中的 Skill，不需要 owner 交出任何凭证。

## 安全 fallback

如果 `$skill-installer` 不可用，但对方有本仓库的 Git 读取权限，可以：

1. 使用自己的 Git 凭证 clone 本仓库。
2. 只把 `skills/claude-research` 安装到
   `$CODEX_HOME/skills/claude-research`，不要安装或复制无关文件。
3. 从安装后的 Skill 目录运行 `python3 scripts/setup_client.py`。

如果对方也没有 GitHub CLI 或 `$skill-installer`，owner 可以通过私密渠道
只发送 `skills/claude-research` 这个目录，例如放入对方已可用的加密/受控
传输方式；不要用公开链接分发。无论采用哪种方式，都绝不发送任何私钥、
token 或仓库其他机密。

## 对方需要做什么

对方只需要：

1. 把上述指令交给自己的 Codex。
2. 将 Codex 输出的单行 `.pub` 公钥发给服务器管理员。
3. 管理员确认授权后告诉 Codex继续验证。
4. 验证通过（工具清单恰好为七个）后重启 Codex，再使用
   `$claude-research`。

私钥始终保留在对方电脑。不要通过聊天、网盘、邮件或仓库传递私钥。

唯一无法自动化的步骤是服务器管理员把对方新生成的 `.pub` 公钥绑定到
partner forced-command。除此之外不要增加公网自助注册，也不要改变服务端
安全边界。
