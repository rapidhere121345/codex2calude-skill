# Claude MCP 请求追踪与原始错误留存部署记录

核验日期：2026-09-20，Asia/Shanghai；最终线上回归约 12:46–12:47。

## 结论

- 最终发布：`/opt/claude-research-mcp/releases/audit-raw-20260920-3`；`current` 已原子切换至该目录。
- 功能提交：`e6087b0fe288d349577ccd16d9c3c23aeaf614e9`。软件版本仍为 `0.1.0`，未升版本。
- 完整测试：**156 passed in 6.26s**；最终 `git diff --cached --check` 和 Shell 语法检查通过。
- owner、partner 均以全新受限 MCP 连接核验：准确的 7 个工具、登录状态、一次最小 Opus 推理，以及一次空提示词校验错误。两种身份实际推理均成功，响应报告 `actual_models=["claude-opus-5"]`。
- 最终六个请求的 `request_id` 与数据库记录逐一对应。284 条旧审计记录保留；两轮上线回归后共 296 条，`PRAGMA quick_check=ok`，核验时没有 running 记录。
- VPS 上使用最终安装包、claudemcp 身份及隔离临时目录完成了合成失败测试：原始 stdout/stderr 按请求编号保存、审计字段关联正确、错误正文不回传、权限符合要求。未通过破坏认证或制造真实上游故障来测试留存。
- `claude-mcp-sshd.service` 和每日清理 timer 均 active；清理 service 最近执行 `Result=success / ExecMainStatus=0`。

本次完成的是 MCP 可观测性增强与发布，不是此前长研究会话恢复或 ActionFlow 报告评审。未修改白皮书、历史证据册、任何飞书文档、模型白名单或认证配置。

## 留存内容与安全边界

每次进入工具处理器的调用生成独立 UUIDv4 `request_id`，关联客户端结果、审计行与可用的原始错误文件。它不是 Claude 原生会话 ID。新增审计列为 `request_id`、`error_category`、`http_status`、`exit_code`、`raw_log_saved`，迁移只增列，旧记录保持 NULL。

仅失败 CLI 输出可写入：

```text
/var/lib/claude-mcp/research/state/error-diagnostics/<principal>/<request_id>.json
```

- 根目录及身份子目录 0700，文件 0600；按 owner/partner 分目录，不增加 MCP 导出或服务器终端能力。
- 保留失败 stdout/stderr 原文，但各流最多 64 KiB，并标记截断；不能将“保留原始错误”理解为无限长完整进程记录。
- 每个身份最多 100 个文件、20 MiB；容量不足时清理较旧的受管日志。单文件安全上限 1 MiB。
- 超过 7 天的受管日志在写入及每日定时清理时移除；空闲时可能保留至下一次每日清理。不是永久归档。
- 使用受控目录、文件锁及拒绝符号链接等检查；仅合法 UUID 命名的普通诊断文件参与清理。
- 成功响应不落入该目录。不主动复制提示词、环境变量、OAuth 文件或成功答案；但上游原始错误本身可能含私密内容或凭据，因此必须视为敏感文件，不粘贴到聊天或研究文档中。
- timeout、cancel、输出超限尽力保留有界部分输出并停止子进程；尚无 CLI 输出、写入失败、截断或进程异常终止仍可能使证据不完整。
- 协议或参数 schema 在进入处理器前拒绝的请求，不保证有应用级 request_id 和审计行。相同 Unix 身份下的管理员级访问不是 owner/partner 文件夹能隔离的安全边界。

用户明确要求保留原始错误后才启用该私有留存。Git 只包含代码、测试、部署资产和本脱敏记录，不包含数据库、私钥或真实原始错误正文。

## 最终线上请求证据

所有调用均走原有 SSH forced-command MCP；管理 SSH 只用于部署、日志关联和服务验证。

| 身份 | 检查 | request_id | 结果 |
| --- | --- | --- | --- |
| owner | status | `969bacc4-7abc-4bdf-9cec-af3a456c98d8` | success |
| owner | 最小 Opus 推理 | `55d9b13e-cab1-46f3-a940-c8e16e996e38` | success |
| owner | 空提示词 | `be724394-57cd-4450-b582-fad3feeb3f6f` | error / validation |
| partner | status | `18f1d56c-3ac2-410d-bd8f-6a5a119787e7` | success |
| partner | 最小 Opus 推理 | `99e49feb-a7d9-4f7e-bb4b-7a30d7e8fdc4` | success |
| partner | 空提示词 | `40385068-76eb-451f-9b75-e4a644c66b99` | error / validation |

参数为 `model=opus`、`use_web=false`、`max_turns=1`、`timeout_seconds=180`。成功调用和执行 CLI 前的校验错误均没有原始错误文件，`raw_log_saved=NULL`，符合设计。

隔离合成错误使用固定测试标记、HTTP 401 与退出码 1；两种身份均验证 `error_category=auth / http_status=401 / exit_code=1 / raw_log_saved=1`，原始文件含该标记但客户端错误不含。合成文件放在独立 `/dev/shm` 临时目录，测试后自动清除，没有污染生产审计库或伪装为真实上游故障。

partner 验收仅把既有私钥交付包临时解到内存文件系统，测试后自动清除临时副本；原始交付包哈希复核不变。没有生成、轮换或打印密钥。

## 发布完整性与保留项

最终发布源码、安装后的全部 Python 模块和系统内安装的两份清理 unit 均核对一致；发布包 38 个源码/配置/测试文件与功能提交中的内容一致。部署记录作为后续文档提交，不混入运行包。

| 资产 | SHA-256 |
| --- | --- |
| `release-final.tar.gz` | `bedcb83a842e3e94f02d1a8d410e3f480c6c3db244bb9b802cf3725da66fe5a0` |
| `claude_research_mcp-0.1.0-py3-none-any.whl` | `bbeffd12b11991e4679d0049087afbfceacaf24bea93bf69260355a2437999d9` |
| `executor.py` | `2c84ff2c2f70af3934025cd76a5f926b710a0c11974a4077b5f696ec2338aad9` |
| `server.py` | `9d04e0fa5ad45a10cef0a50803145bc7cbcccd8bdaad2345c02a401ebdb2e746` |
| `persistence.py` | `64a6b53c6558345447e98015ad2e406663a53cf2b370f010857765bf76716403` |
| `raw_error_logs.py` | `1be348011a7506232301e63b0c2063029783d8d7b49c3b1d59a60a7379441428` |
| `diagnostics.py` | `f5d8795b1f5939ff7c6f1301b2c349ef0e9f6eac3b8cbd913e86181b8b869732` |

旧 release 未删除；`diagnostics-20260919-3` 的 executor/server 哈希再次与 9 月 19 日部署记录核对一致。两个 root 私有、0600 的 SQLite 备份保存在：

```text
/var/lib/claude-mcp/maintenance/backups/audit-raw-20260920-2-pre-migration.sqlite3
/var/lib/claude-mcp/maintenance/backups/audit-raw-20260920-3-pre-migration.sqlite3
```

第一个对应真正增列前的 284 条记录，第二个对应最终格式修正版切换前的 290 条记录。数据库备份与原始错误日志是不同资产，7 天错误日志清理策略不会删除数据库备份。

未重启 MCP SSH 服务、杀掉旧 stdio 服务或中断其他研究连接。既有进程可能继续使用旧模块，客户端应在当前任务结束后重连 MCP；新连接已验证使用最终 release。旧连接产生的记录仍可能没有新增诊断字段。

## 失败历史与解释边界

1. 早期测试通过不代表覆盖完整；审阅进一步补齐了部分输出捕获、取消/超限边界及 status 错误漏带 request_id 的回归测试。最终以 156 项完整回归为本轮门槛。
2. `audit-raw-20260920-1` 在切换前的合成测试失败：sudo 降权后继承了不可访问的工作目录，FastMCP 对相对 `.env` 的路径检查产生 PermissionError。该次部署记为失败，没有切换现网。修正部署脚本的执行目录为 `/` 后，以独立 release `-2` 完成测试并上线；未放宽生产权限。
3. Git 暂存后对新增文件的完整格式检查发现清理 service 末尾多余空行。修正后重新打包为 `-3`，完整测试、VPS 合成测试、真实双身份推理和审计关联均重新通过。该检查失败不追认通过；`-2` 的功能验证保留为历史。
4. 过去只有 exit 1 / unknown、且没有捕获原始输出的错误，无法靠本次升级恢复原文。本次没有证明 9 月 19 日 23:52/23:54 的具体根因。
5. 最小 Opus 调用成功不证明旧长会话已恢复、联网研究已通过、账户额度充足或错误永久消失。该补丁增强追踪，不修复上游额度、网络或认证故障。没有读取 OAuth 凭据或非公开 usage API。

## 运维与回滚

再次报错时，让 partner 提供 request_id 和脱敏错误即可；管理员先按该编号查审计表，必要时检查对应私有原始文件，不要求对方发送私钥或全文研究会话。

优先回滚目标为本轮前稳定发布 `diagnostics-20260919-3`。回滚前重新确认当前指针、旧目录、无同名临时链接，并在需要时协调正在运行的任务：

1. 停用 `claude-mcp-error-retention.timer`。旧版没有清理脚本，不能只换代码而遗留启用的 timer。
2. 用独占临时软链接及原子 rename 切回经核验的旧 release；不要删除任何旧发布或现有错误证据。
3. 保留增列后的生产数据库，不把备份直接覆盖回去，以免丢失上线后的会话及审计记录；增列设计兼容旧实现。
4. 用全新受限 MCP 连接验证实际工具与最小推理；旧进程不会自动卸载已加载的模块。

本轮未执行回滚。数据库备份恢复属于单独的数据恢复操作，不能作为普通代码回滚的自动步骤。

## 本地复验命令

```bash
.venv/bin/python -B -m pytest -q -p no:cacheprovider
git diff --cached --check
sh -n scripts/install.sh scripts/stdio-forced-command.sh
```

线上另行核验了发布指针、发布包完整性、安装模块、unit 内容、服务状态、私有目录/备份权限、两种身份的隔离合成错误及真实 MCP 调用。保留原 9 月 19 日部署记录，不覆盖历史结论。
