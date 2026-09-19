# agentacct

[English](README.md) · 简体中文

[![tests](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml/badge.svg)](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agentacct.svg)](https://pypi.org/project/agentacct/)
[![Python](https://img.shields.io/pypi/pyversions/agentacct.svg)](https://pypi.org/project/agentacct/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**agentacct 帮你搞清楚一件事：你的 agent 到底干了什么。**

很多时候，你的 coding agent 号称完成了一个任务，可你并不知道它具体做了什么、花了多少钱、哪些结果是真正验证过的。agentacct 把这些整理成每个任务一张 Work Receipt（工作收据），数据来自 Claude Code、Codex、OpenCode、Hermes 等 agent 本来就写在你电脑上的会话日志。不需要注册账号，不上传云端，所有数据都留在本机。

![Sessions 视图：左侧是任务列表，每一行都有自己的判定（Verified、In Progress、Reported、Observed）；右侧打开的是"给登录接口加一个令牌桶限流"这张收据，判定为 Verified，顶部两条汇总条显示 5 个步骤（4 个自检、1 个仅声明）和 5 次检查（全部通过），中间是带编号的步骤列表，最新一步展开后能看到 agent 上报的检查、退出码和改动的文件，最下方是活动时间线，检查卡片从 12 个失败一路走到 12、12、38 个通过。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-work-receipt.png)

<sub>截图里是合成的演示数据；装好之后你看到的是自己机器上的真实数据。</sub>

## 安装

```bash
pipx install agentacct
agentacct onboard   # 每台机器只需一次：自动发现你装了哪些 agent，建立本地存储，启动记录器
agentacct tui       # 终端版界面
```

需要 Python 3.11 及以上，macOS 或 Linux；Windows 需要通过 WSL 使用。如果你更喜欢原生窗口、不想折腾 Python，可以直接从 [最新 release](https://github.com/mikehasa/agentacct/releases/latest) 下载已签名、已公证的 **macOS 应用**（需要 macOS 14 及以上）。

装好之后记得新开一个 agent 会话：hook 和 MCP server 是在会话启动时接入的，装之前就开着的会话不会被记录。

各个 agent 的具体配置、项目级安装（`--scope project`），以及用 `uv` 或 `venv` 安装的方式，见 [INSTALL.md](INSTALL.md)（英文）。

## "做完了"，谁说的算？

上面这张收据依次回答三个问题：任务完成了吗？这是谁说的？证据现在还有效吗？

agent 自己说的"做完了"只算 **Reported**（自述完成）。只有当所有现行检查都通过、而且是在最后一次记录的改动之后跑的，任务才会标为 **Verified**（已验证）。没有任何检查的步骤只是 **claimed**（一句声明）；每一项检查都注明它的证据等级：是 agent 自己上报的，是 hook 观测到的退出码，还是 CI 的结果。拿上面这张收据来说，第 1 到 4 步都有 agent 上报的检查，第 5 步只是一句声明，收据就如实这么写。

步骤下方的时间线把那次失败的运行原样留着：第一次测试 12 个用例失败，后来几次分别通过了 12、12 和 38 个。失败不会被平均掉，所以你能清楚看到证据是在哪个时刻追上代码的，以及有没有追上。

## 一整天的工作，不管用的是哪个 agent

![Work 标签页：一个叫 "billing-svc" 的分组，8 个会话、4 个 agent、前后约 9 小时（费用 ≈$72.71，是各张收据相加的结果），全部排在 09:05 到 18:20 的同一条时间轴上。两段较长的 Claude Code 运行撑起了上午和下午；更短的 Codex 和 Hermes 运行嵌在它们中间；OpenCode 的运行跨在边缘；最后一条需要处理的运行带着红点。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-work.png)

在 Work 标签页里把一个 work group 指向某个项目目录，这个目录下跑过的所有会话都会出现在同一条时间轴上，不管是哪个 agent 跑的。每个会话保留自己的收据和证据；分组显示的合计只是 8 张收据的简单相加，不是对整个工作的综合评判。

## 一天从最需要你处理的事开始

![Dashboard：Shift Brief 置顶了"修复不稳定的支付测试"（billing-svc，claude-code，1 个失败 7 个通过，记录的原因是 Failed check，来源是 MCP record），配有 Review evidence 和 Copy review brief 两个按钮；右侧的 Signal rail 显示 Working now、Capacity、Usage change 和 Evidence trust；下方是 Recent work 表格和最近七天的用量图。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-dashboard.png)

Shift Brief 只挑出最需要人介入的那一个任务，告诉你记录在案的原因，以及这个判断的依据从哪来：这里是"修复不稳定的支付测试"的一次失败检查，通过 MCP 记录下来的。**Copy review brief** 只复制记录在案的事实，不会重新执行任何东西。

## 在 agent 撞上额度上限之前，先看清余量

![Usage & limits：按客户端显示供应商的额度窗口（codex 的 5 小时窗口已用 12%、每周额度已用 63%，带重置时间；claude-code 分别是 34% 和 59%；opencode 和 hermes 没有供应商额度信息），旁边是每个客户端最近七天的实际用量，全部标注为 pricing estimate。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-usage.png)

各家供应商上报的额度窗口，和每个 agent 实际的用量并排显示。token 数来自客户端自己的记录，费用是按价目表估算的，带 `≈` 标记，从来不是账单。

## 终端里也能用

![agentacct tui：Shift Brief 置顶一个被阻塞的 claude-code 任务，带记录的原因、下一步和 MCP record 来源；Signal rail 显示 Working now、Capacity、Usage change 和 Evidence trust；Recent work 表格列出结果、证据和估算费用；底部是用量历史的迷你图。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/tui-dashboard.png)

`agentacct tui` 在终端里显示同样的 Shift Brief、收据和额度；按 `?` 查看快捷键。终端版暂时没有按目录分组的标签页。

## 宁可留白，也不瞎猜

agentacct 还处于 early alpha 阶段。它的原则是：宁可给你看一个空缺，也不给你一个猜测。

- **Reported 不等于 Verified。** agent 自己的说法，永远不会被包装成验证结果。
- **估算就明说是估算。** `≈$` 表示按价目表估算，`~$` 表示已知不完整的小计；agentacct 拿不到账单数据。每条路径能证明什么，见 [docs/usage-truth-table.md](docs/usage-truth-table.md)。
- **宁缺毋错。** 用量和记录的工作之间的每一次关联都带置信度（`exact`、`high`、`medium`、`low`）；证明不了的关联就显示为空缺，而不是一个零。
- **按能力算支持，不按 logo 算。** 目前 Claude Code 和 Codex 的收据最完整；[coverage matrix](docs/coverage-matrix.md) 对 OpenCode、Hermes、DeepSeek Harness、OpenClaw、Cursor 的每一项能力单独评级，`agentacct capabilities agents` 会打印你当前版本对应的这张表。
- **记录的是工作，不是对话。** 记录的是工具名称和类别、改动的文件、去掉了凭证的命令、退出码、token 用量和记录下来的步骤；不会存你的 prompt、模型的回复或完整的对话记录。详见 [privacy threat model](docs/multi-source-privacy-threat-model.md)。
- **不往外发任何东西。** 只读取已识别客户端的本地会话文件，只监听 `127.0.0.1`，不会存储或索要任何供应商的 API key。

## 工作原理

agentacct 是本地优先的 Agent Work Intelligence：两条证据流分开保存，靠真实的客户端 id 关联，不靠猜。

- **用了什么**：来自每个客户端自己的会话文件，token 按客户端上报的数字算，费用按价目表估算。
- **做了什么**：来自 agent 工作过程中通过 MCP 记录的步骤和检查，再加上测试运行这类机器检查。一项检查的证据等级取决于它是怎么被观测到的，跟 agent 怎么描述无关。
- **怎么关联**：通过会话 id 和 transcript id 把两者对上，每一次归因都标注置信度。Claude Code 通过安装的 hook bridge 绑定真实 id；Codex 和 OpenCode 在导入时从各自的会话日志里匹配；Hermes 只在 agent 显式传入 id 时才会关联记录的工作。收据会写明用的是哪条路径。

关联机制、置信度说明和 MCP 工具列表见 [docs/reference.md](docs/reference.md)；各 agent 的集成细节见 [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md)。文档目前只有英文。

## 让你的 coding agent 帮你装

把下面这段直接发给你的 coding agent：

```text
Install and set up agentacct — a local-first agent work ledger that reads my
coding-agent logs read-only and shows honest token usage, cost, and recorded work.

Run `pipx install agentacct`
(or `pipx install git+https://github.com/mikehasa/agentacct`),
then `agentacct onboard` (installs once per machine, global by default, zero
files written into the repo), then tell me how to open `agentacct tui` and the
local JSON API at http://127.0.0.1:8765.

Observe-only: never store, request, or echo any API key; all state stays local
on this machine. Don't modify my global client config without showing the exact
command first.
```

agent 会按照 [INSTALL.md](INSTALL.md) 的步骤操作。`agentacct setup prompt --agent <client>` 输出的是同一段提示。

## 卸载

```bash
agentacct stop                 # 停止托管的同步进程和本地 API（只停 agentacct 自己启动的进程）
agentacct uninstall-autostart  # 只有配置过开机自启才需要
pipx uninstall agentacct
```

然后删除 `~/.local/state/agentacct/state` 里的存储（想保留历史记录就留着），并清理 `agentacct onboard` 写进各客户端配置的条目，具体清单按客户端列在 [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md)。如果用的是 `--scope project` 安装，所有内容都在那个仓库的 `.agent-sentinel/` 目录里。

## 更多

- 示例：[当 agent 说"做完了"的时候](docs/examples/when-an-agent-says-done.md) · [同一个任务，Claude Code 和 Codex 各做一遍](docs/examples/compare-claude-code-and-codex.md)
- [Reference](docs/reference.md) · [Coverage matrix](docs/coverage-matrix.md) · [Architecture](docs/architecture.md) · [Safety boundaries](docs/safety-boundaries.md) · [Full flow demo](docs/full-demo.md)
- 参与贡献：[CONTRIBUTING.md](CONTRIBUTING.md) 介绍了怎么跑测试，以及怎么用合成的演示数据重新生成上面的截图。
- 反馈：欢迎开 issue，说明你用的是哪个 agent，哪一次关联、归因或收据看起来不对，或者你觉得收据还需要显示什么才值得信任。发日志之前记得抹掉 API key 和私有路径。
