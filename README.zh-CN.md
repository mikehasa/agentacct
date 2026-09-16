# agentacct

[English](README.md) · 简体中文

[![tests](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml/badge.svg)](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agentacct.svg)](https://pypi.org/project/agentacct/)
[![Python](https://img.shields.io/pypi/pyversions/agentacct.svg)](https://pypi.org/project/agentacct/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**agentacct 只回答一个问题：我的 agent 到底他妈在干什么？**

你的 coding agent 说"做完了"。agentacct 给你看它到底做了什么、花了多少、其中有多少是被证明的：每个任务一张 Work Receipt（工作收据），从 Claude Code、Codex、OpenCode、Hermes 等 agent 本来就写在你机器上的会话日志里生成。不用账号，不上云，什么都不离开你的电脑。

![Sessions 视图：左侧是任务列表，每行一个判定（Verified、In Progress、Reported、Observed）；右侧是打开的收据 "Add a token-bucket rate limiter to the login API"，标为 Verified，上方两条汇总条（5 个步骤：4 个 self-checked、1 个 claimed；5 次检查：5 次通过），中间是带编号的步骤列表，最新一步展开显示它的 agent 上报检查、退出码和改动文件，下方是活动时间线，检查卡片从 12 failed 到 12 passed、12 passed、38 passed。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-work-receipt.png)

<sub>截图来自一个合成的演示工作区；你自己装上之后看到的是本机的真实数据。</sub>

## 安装

```bash
pipx install agentacct
agentacct onboard   # 每台机器一次：找到你的 agent，建好本地存储，启动记录器
agentacct tui       # 终端版
```

需要 Python >= 3.11，macOS 或 Linux；Windows 只支持 WSL。想要原生窗口、不装 Python，可以从 [最新 release](https://github.com/mikehasa/agentacct/releases/latest) 下载签名并公证过的 **macOS app**（macOS 14+）。

不管哪种方式，装完之后重新开一个 agent 会话：hook 和 MCP server 在会话启动时才会接上。

按 agent 分别设置、`--scope project` 的项目级安装、以及 `uv`/`venv` 的替代方式，见 [INSTALL.md](INSTALL.md)（英文）。

## 做完了，谁说的？

上面那张收据按顺序回答三个问题：做完了没有，谁说的，证据还算数吗。

agent 自己说的"做完了"归在 **Reported**。只有当每一项现行检查都通过、而且是在最后一次记录的改动之后跑的，任务才会显示 **Verified**。没有检查的步骤只是 **claimed**（一句声明），而每一项检查都带着它的证据等级：agent 自己上报的、hook 观测到的退出码、或者 CI。上面的收据里，第 1 到 4 步带的是 agent 上报的检查；第 5 步只是一句声明，收据就这么写。

步骤下面的时间线把那次红色的运行留了下来。第一次测试 12 个失败；后面几次分别通过 12、12 和 38 个，什么都没有被平均掉，所以你能看到证据是什么时候追上代码的，以及什么时候没追上。

## 一天的工作，横跨你用的每一个 agent

![Work 标签页：一个 "billing-svc" 分组，8 个会话、4 个 agent、约 9 小时（≈$72.71，各收据之和），在 09:05 到 18:20 的同一条时间轴上。两段长的 Claude Code 运行撑起上午和下午；更短的 Codex 和 Hermes 运行嵌在其中；OpenCode 的运行跨在边缘上；最后一条需要处理的运行带着红点。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-work.png)

把一个 work group 指向项目文件夹，在那里跑过的每个会话都会落到同一条时间轴上，不管是哪个 agent 跑的。每个会话保留自己的收据和证据；分组的合计是 8 张收据的求和，永远不是对这项工作的合并判定。

## 从需要你处理的事开始一天

![Dashboard：Shift Brief 置顶 "Fix the flaky payment test"（billing-svc，claude-code，1 失败 7 通过，记录的原因是 Failed check，来源是 MCP record），带 Review evidence 和 Copy review brief 两个按钮；右侧 Signal rail 有 Working now、Capacity、Usage change、Evidence trust；下方是 Recent work 表格和七天用量图。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-dashboard.png)

Shift Brief 点名最需要人来处理的那一个任务、记录下来的原因、以及这个说法从哪来：这里是 "Fix the flaky payment test" 的一次失败检查，通过 MCP 记录。**Copy review brief** 只复制记录在案的事实，绝不重跑任何东西。

## 在 agent 撞上额度之前知道余量

![Usage & limits：按客户端显示供应商的额度窗口（codex 5 小时窗口已用 12%、每周已用 63%，带重置时间；claude-code 34% 和 59%；opencode 和 hermes 没有供应商额度）以及每个客户端最近七天记录的用量，全部标注为 pricing estimate。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/app-usage.png)

供应商上报的额度窗口和每个 agent 实际用了多少并排放着。token 数是客户端上报的，费用是按价目表估算的、标着 `≈`，永远不是账单。

## 终端里也有

![agentacct tui：Shift Brief 置顶一个被 blocked 的 claude-code 任务，带记录的原因、下一步和 MCP record 来源；Signal rail 有 Working now、Capacity、Usage change、Evidence trust；Recent work 表格有结果、证据和估算费用；底部是用量历史迷你图。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/tui-dashboard.png)

`agentacct tui` 在 shell 里显示同样的 Shift Brief、收据和额度；按 `?` 看快捷键。终端版目前没有按文件夹分组的标签页。

## 诚实是设计出来的

agentacct 还在 early alpha，它宁可给你看一个空缺，也不给你一个猜测。

- **Reported 不等于 Verified。** agent 的声明永远不能伪装成验证。
- **估算就标成估算。** `≈$` 是按价目表的估算，`~$` 是已知不完整的小计；没有账单访问权限。[docs/usage-truth-table.md](docs/usage-truth-table.md) 说明每条路径能证明什么。
- **缺失好过错误。** 用量和记录的工作之间的每一次关联都带置信度标签（`exact`、`high`、`medium`、`low`）；证明不了的关联显示为空缺，不是零。
- **支持按能力算，不按 logo 算。** 今天 Claude Code 和 Codex 的收据最完整；[coverage matrix](docs/coverage-matrix.md) 对 OpenCode、Hermes、DeepSeek Harness、OpenClaw、Cursor 的每条能力分别评级，`agentacct capabilities agents` 打印你所装版本的同一张表。
- **记录的是工作，不是对话。** 工具名和类别、改动的文件、去掉凭证的命令、退出码、token、记录的步骤。绝不存你的 prompt、模型的回复或完整对话；见 [privacy threat model](docs/multi-source-privacy-threat-model.md)。
- **不往外发任何东西。** 只读取已检测到的客户端的本地会话文件，只监听 `127.0.0.1`，绝不存储或索取供应商 API key。

## 它怎么工作

agentacct 是本地优先的 Agent Work Intelligence：两条证据流分开保存，用真实的客户端 id 关联，而不是猜。

- **用了什么** 来自每个客户端自己的会话文件：token 按客户端上报的算，费用按价目表估。
- **做了什么** 来自 agent 工作时通过 MCP 记录的步骤和检查，加上测试运行这类机器检查。一项检查的证据等级由它是怎么被观测到的决定，和 agent 的措辞无关。
- **关联** 通过会话和 transcript id 把两者连起来，每一次归因都带置信度。Claude Code 通过安装的 hook bridge 绑定真实 id；Codex 和 OpenCode 在导入时从它们自己的会话日志里匹配；Hermes 只在 agent 显式传入 id 时关联记录的工作。收据会写明用的是哪条路径。

关联机制、置信度词汇表和 MCP 工具列表见 [docs/reference.md](docs/reference.md)；各 agent 的集成细节见 [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md)。文档目前是英文。

## 让你的 coding agent 帮你装

把这段贴给你的 coding agent：

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

agent 接下来会按 [INSTALL.md](INSTALL.md) 操作。`agentacct setup prompt --agent <client>` 打印的是同一段提示。

## 卸载

```bash
agentacct stop                 # 停掉托管的同步进程和本地 API（只停它自己起的进程）
agentacct uninstall-autostart  # 只有装过自启动才需要
pipx uninstall agentacct
```

然后删掉 `~/.local/state/agentacct/state` 里的存储（想保留历史就留着），并移除 `agentacct onboard` 写进各客户端配置的条目；按客户端列在 [docs/coding-agent-integrations.md](docs/coding-agent-integrations.md)。`--scope project` 安装的所有东西都在那个仓库的 `.agent-sentinel/` 目录里。

## 更多

- 示例：[当 agent 说"做完了"](docs/examples/when-an-agent-says-done.md) · [同一个任务，Claude Code 对比 Codex](docs/examples/compare-claude-code-and-codex.md)
- [Reference](docs/reference.md) · [Coverage matrix](docs/coverage-matrix.md) · [Architecture](docs/architecture.md) · [Safety boundaries](docs/safety-boundaries.md) · [Full flow demo](docs/full-demo.md)
- 参与贡献：[CONTRIBUTING.md](CONTRIBUTING.md) 说明怎么跑测试、怎么从合成演示数据重新生成上面的截图。
- 反馈：开一个 issue，写明你用的 agent，以及哪次关联、归因或收据看起来不对，或者一张收据要显示什么你才会信任一次运行。发日志前先抹掉 API key 和私有路径。
