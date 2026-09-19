# Claude MCP 脱敏诊断补丁部署记录

核验日期：2026-09-19，Asia/Shanghai；最终线上核验约 16:44。

## 结论

- Tailscale 管理连接验证通过。
- 当前发布：`/opt/claude-research-mcp/releases/diagnostics-20260919-3`，`current` 已指向该目录；`claude-mcp-sshd.service` 为 active。
- 全新受限 stdio MCP 连接返回且仅返回约定的 7 个工具。以 `model=opus`、`use_web=false`、`max_turns=1`、`timeout_seconds=60` 调用 `claude_once` 成功，响应包含预期测试标记 `CLAUDE_MCP_OK`，MCP 未返回工具错误。
- 该测试证明本次最小 Opus 推理可用，不证明长任务、联网研究、partner principal 或全部会话生命周期均已完成线上回归。
- 本地最终回归：**103 passed in 3.36s**；`git diff --check` 通过。

## 修复内容与边界

旧执行器在 CLI 非零退出时未利用 stdout 中的结构化错误；stderr 为空时，仅能报告泛化的 exit 1。登录状态和 CLI 版本兼容性不能代表实际推理权限或可用额度。

新增有界解析与固定错误类别：`auth`、`rate_limit`、`model_unavailable`、`permission`、`network`、`invalid_argument`、`runtime`、`unknown`。只回传固定类别、经过校验的退出码和白名单 HTTP 状态码；不回传原始 stdout/stderr、错误正文、凭据、路径或 Claude 原生会话 ID。对 CLI 退出 0 但 `is_error=true` 的响应，也按错误处理。

识别限额文案时兼容弯引号与 Unicode 空白。数字不会仅因出现在自由文本中被识别为 HTTP 状态码。无法可靠识别的内容保持 `unknown`，不强行归因。

本轮修改文件：

- `src/claude_research_mcp/executor.py`：脱敏错误分类及运行失败处理。
- `src/claude_research_mcp/server.py`：结构化错误结果处理。
- `tests/test_error_diagnostics.py`：新增错误分类、泄漏防护和异常输入测试。
- `tests/test_statusline_capture.py`：修复集成测试的过期日期夹具；生产额度捕获逻辑未修改。
- `README.md`：记录诊断能力与状态接口边界。
- 本部署记录。

未更改模型白名单、认证配置、密钥、权限边界、7 个工具的集合或正常成功响应契约；未提交、未推送、未升版本。没有修改 ActionFlow 白皮书或飞书文档。

## 核验历史与不确定性

1. 初始测试出现 91 passed、1 failed：失败来自固定测试日期已经过期。该次运行仍记为失败，不追认通过。
2. 修正测试时钟夹具并补充边界测试后，最终全量 103 项通过。
3. 早期线上失败为 exit 1，stderr 为空，stdout 含 `is_error=true` 的结构化结果。受限特征检查确认错误正文存在限额提示特征，但未输出正文或重置时间。
4. 一次宽泛特征检查曾把返回结构的权限相关字段误当成错误正文证据；已纠正，不将其作为“权限不足”的结论。
5. 最后一次线上最小推理成功。因此不能将“当前额度耗尽”作为最终状态，也不能断言恢复由诊断补丁造成；补丁增加可观测性，并不增加账户额度或更改认证。
6. 此前限制究竟对应五小时额度、七天额度或其他窗口，以及剩余额度和重置时间，仍未取得可核验值。需要时使用 MCP 官方 statusline 观测或由用户在 Claude 官方 usage 页面检查，不读取 OAuth 凭据，不调用非公开 usage API。
7. Claude 对 ActionFlow 调研报告的独立评论尚未执行；本轮成功测试不等于报告评审完成。

## 发布与完整性

通过独立新 release 安装及原子 `current` 软链接切换发布；未覆盖旧 release。安装后核验 executor/server 模块实际从当前 release 导入，且安装内容与当前 release 的源码一致。

本地源码与线上当前 release 源码 SHA-256 一致：

| 文件 | SHA-256 |
| --- | --- |
| `executor.py` | `edd87d092448dead764a11f87ac4aed762590c59a734b28aa97429675a0b6de4` |
| `server.py` | `daf4da4466d76ea6f1fc71d9677b2bf7a684d2a2c1e2b600f18464262fa524c5` |

最终发布包：

| 包 | SHA-256 |
| --- | --- |
| `source-v3.tar.gz` | `bdba49b26e0716411a23c4b9f1f0207deebe72784ee2d45256c625e1777ea0e1` |
| wheel，版本仍为 `0.1.0` | `a2d7f09058708cdb6542a8a5473b20c2649594da62796f8ed921e312825c9f1d` |

原发布 `quota-open-20260914-1` 完整保留；再次核验其 executor/server 文件哈希与部署前一致。中间诊断 release 也保留，未删除任何旧发布或用户数据。

## 回滚

回滚目标：`/opt/claude-research-mcp/releases/quota-open-20260914-1`。

回滚需重新核验该目录存在、当前发布指针和独占临时软链接名，再原子切换 `current`；本轮未执行回滚。现存 MCP 进程可能继续使用此前加载的模块，因此发布或回滚后须通过新 MCP 连接验证，不能仅依据旧连接的返回判断部署成败。

## 验收命令

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
git diff --check
```

线上另行核验了 current 指针、服务状态、实际安装模块、原发布哈希、准确工具集合及一次受限最小推理。未读取或输出凭据，也未改变远端认证设置。
