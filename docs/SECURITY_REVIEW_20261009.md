# Bridge 与只读审计页安全审查

检查日期：2026-10-09。范围：SSH/MCP 连接回收、只读 usage/health 页面、数据采集及部署边界。这里记录可检查的技术边界，不承诺 IP 声誉或账号零封禁风险。

## 已发现的问题

- VPS 约 4 GB 内存、无 swap，同时运行约 82 个 bridge worker，发生反复 OOM，连 Tailscale 也被内核杀死。SSH 公钥认证成功不代表 MCP 初始化能完成。
- 旧客户端断线后主动重连，无空闲生命周期管理。仅在服务端设置断开会被旧代理重新连回。
- Gemini partner 在 VPS 服务账号内具有普通 shell/文件工具能力。因此仅绑定 localhost 并相信身份 HTTP header 的页面，仍可能被本机 partner 进程伪造访问。

## 本次实现边界

- 默认空闲 1800 秒释放连接；请求进行中不强杀；请求结果未知不重放；ping 不维持远端 worker。
- 4 GB swap 只提供故障缓冲，空闲回收才是连接数量治理措施。
- 审计网页生产入口使用权限保护的 Unix socket，由 Tailscale Serve 代理；不开放公共网页端口，不使用 Funnel。
- 每个页面/API/静态资源请求都核对配置的 owner Tailscale login。Unix socket 路径和权限阻止 claudemcp/geminimcp 直接伪造 header。
- 页面无推理、命令执行、账号写入、任意 URL 请求或代理转发接口；不加载第三方脚本、CDN 或分析代码。
- UI 只读元数据，原始 prompt/result、原生 CLI session ID、OAuth 文件、SSH key 不进入网页/API。
- 健康探测与网页服务分离，页面刷新只读缓存。采集调用已有 status 流程，不生成模型推理；官方 CLI 自行处理登录，采集器不读取或复制凭证内容。生产采集周期 15 分钟。
- 调用次数是 bridge 审计次数，需区分状态/管理请求和推理请求。token 仅采用 CLI 实际返回的字段；历史缺失不回填、不当成零；不是供应商账单或防篡改会计凭证。

## 仍然存在的账号风险

- Claude 官方认证规则不允许替第三方用户转接个人 Free/Pro/Max 凭证，要求终端用户使用自己的凭证。owner/partner 共用同一个个人账号的风险不能由 SSH principal 隔离或审计页消除。
- Antigravity FAQ 明确警告第三方软件使用其登录可能导致账号停用，并有地区/账号资格要求。本 bridge 使用官方 CLI，但“官方二进制 + 第三方封装 + 共享个人账号”不自动证明这种用法获准。
- 固定美国 VPS 出口本身不能证明用户/账号符合支持地区规则。没有对风险系统作规避、伪装或降低检测的修改。
- 不自动改用付费 API、开启超额付费、换账号、切模型或改变出口。需要消除共享订阅风险时，应采用各自独立且被允许的登录/席位，或服务方支持的集成计费方式。
- VPS 管理员/root 仍是信任边界。服务账号可写的业务审计库不是不可篡改的安全审计日志。

## 官方依据

- [Claude Code Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)：Authentication and credential use / end-user authentication。
- [Anthropic Consumer Terms](https://www.anthropic.com/legal/consumer-terms)：账号访问和适用条款；具体地区/计划适用版本需另核对。
- [Antigravity FAQ](https://antigravity.google/docs/faq)：第三方登录使用、地区及账号资格。
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)：身份 header 由代理注入且清除外部同名 header；必须保护 backend 直达路径。

运行时验收结果、部署 URL 和修复提交见交付记录；安全边界必须由实际部署测试确认，不能仅依赖这份设计说明。

## 已完成的运行时检查

- 两个 Docker 部署的 Claude/Gemini 共四条连接通过：初始化、七工具目录、加速空闲回收、ping 不重连、真实 tools/list 请求按需重连。
- 服务器实际权限检查：claudemcp、geminimcp 不能读取审计快照，mcp-audit 可以；快照目录 root:mcp-audit 0750，数据库 0640。
- 原始审计库中的 prompt/token 字段和 conversation_history 不会复制到网页快照；测试保持缺失 token 为 NULL。
- 修复了“队列巡检误重置空闲起点”的回归，测试的空闲周期特意跨越后台巡检周期。

- 生产入口已部署至 `https://claude-oregon-01.tail99c4e8.ts.net/`，仅 Tailnet 可达，没有启用 Funnel。
- 实测 root 无身份/错误身份均为 401，精确 owner 身份为 200；claudemcp/geminimcp 伪造身份头均无法连接 socket。运行目录 0700、socket 0600。
- 独立复核发现并修复了 TOML 尾注释导致迁移丢节、子进程组残留、嵌套健康字段泄露、过期队列触发无用重连及 Gemini 嵌套登录状态遗漏。
- 健康快照按 15 分钟采集、20 分钟内视为新鲜，并分别显示 probe_ok / account_ready。两边生产状态探测均通过。
- 快照部署发现 WAL 读取需要锁目录可写；采集器仅以服务账号创建锁，源 SQL 保持只读，网页仍隔离在脱敏快照之外。

- Tailnet HTTPS 页面/API 均实测 200，网页在浏览器成功渲染。外部伪造身份头被 Serve 清除并替换为实际 owner 身份；Unix 后端 Host 为 localhost（[官方实现](https://github.com/tailscale/tailscale/blob/main/ipn/ipnlocal/serve.go)）。没有另一位实际 Tailnet 用户参与测试；非 owner 拒绝由后台身份测试覆盖。
- 360 项本地测试通过。两个容器最后一轮四条传输生命周期测试均通过，Docker healthy。
- 确认无 running 审计记录和 worker 子进程后，回收了 55 个未加保护的遗留 worker；后续新连接均通过服务端 guard。
