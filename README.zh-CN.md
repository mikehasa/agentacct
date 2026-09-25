# agentacct

[English](README.md) · 简体中文

[![tests](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml/badge.svg)](https://github.com/mikehasa/agentacct/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agentacct.svg)](https://pypi.org/project/agentacct/)
[![Python](https://img.shields.io/pypi/pyversions/agentacct.svg)](https://pypi.org/project/agentacct/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**agentacct 帮你搞清楚一件事：你的 agent 到底干了什么。**

看清不同项目、不同客户端里的 coding agents 最近做了什么：活动、工具调用、记录的步骤、检查、token 和估算费用。打开任务，就能沿着 Work Receipt（工作收据）的时间线，从一次尝试看到后来的结果。数据来自 Claude Code、Codex、Kimi Code、OpenCode、Hermes 等客户端的本地会话日志。不需要注册账号，不上传云端，所有数据都留在本机。

![Sessions 与 Work Receipt：按最新活动排序的任务列表，旁边是记录的结果、会话数、估算费用、声明支持情况、检查结果和活动时间线。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-work-receipt.png)

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

## 沿着活动，看清发生了什么

Sessions 默认按最近活动排序，可以按任务或项目搜索，也可以手动切换其他排序。每张收据汇总参与的会话、记录的结果、估算费用、声明与检查；活动时间线放在前面，步骤和完整检查详情可以一键跳转。

agent 自己说的“做完了”算 **Reported**（自述完成）。**Verified**（已验证）需要当前检查全部通过，并且检查发生在最后一次记录的改动之后。声明和检查分开显示，失败尝试会和后来的结果一起保留。记录到失败不等于需要你亲自介入。

## 一整天的工作，不管用的是哪个 agent

![Work 分组展示参与的客户端、最近会话、token 合计和估算费用，并可进入项目的共享活动详情。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-work.png)

把 work group 指向项目目录，就能看到哪些 agents 在这里工作、最近做了什么。打开分组可以看会话详情和共享时间线。每个会话保留自己的证据；合计来自参与的会话，数据不完整或跨目录重复计入时会明确标注。

## 从最近活动开始

![Dashboard 总览展示近期会话状态、已记录任务数、七天用量和按最新活动排序的任务表；历史问题折叠在列表下方。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-dashboard.png)

Dashboard 展示各个项目最新记录的工作，以及客户端、活动时间、结果、证据和费用。历史失败检查与阻塞仍然可以在 **Recorded issues** 中查看，但不会决定默认排序，也不会被当作你必须处理的待办。右键点击 **Recorded issues** 中的一行，可以复制基于已有记录的摘要。

## 看懂工作背后的用量

![Usage 展示相互独立的日期、Agent、Model、Provider 四个筛选器，按 agent 和模型汇总的区间合计，带 Fresh / All tokens 切换的每日图表，以及所选日期按客户端和模型拆分的用量（分别列出缓存读取与写入）。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/zh-CN/app-usage.png)

日期、agent、模型、provider 四个筛选器各自独立，所以“Codex 上的 Opus，最近 7 天”是一次选择，而不是重置整个页面。**This range** 把同一批记录分别按 agent 和按模型汇总；下方的每日表格仍按日期分组，选中某一天后，它下面的客户端/模型明细随之收窄。图表会记住你的 **Fresh** 或 **All tokens** 选择；表格始终并列展示新增、缓存和总 token，客户端与模型明细还区分缓存读取与写入。将鼠标移到 **Fresh** 数值上，可以查看输入和输出计数。

顶栏始终标出本地数据的新鲜度（**Local data · just now**，或最近一次保存的副本）。日期表示**按活动日期归属的会话总量**；跨天会话不会拆成精确的每日消耗。同一页面也能查看供应商上报的额度窗口和重置时间。token 来自客户端记录，费用是带 `≈` 标记的价目表估算，不是账单。

## 终端里也能用

![agentacct tui：Needs review 区块置顶一个被阻塞的 claude-code 任务，带记录的原因、下一步和 MCP record 来源；Right now 栏显示 Working now、Capacity、Usage change 和 Evidence trust；Recent work 表格列出结果、证据和估算费用；底部是用量历史的迷你图。](https://raw.githubusercontent.com/mikehasa/agentacct/main/docs/assets/tui-dashboard.png)

`agentacct tui` 在终端里提供收据、用量、额度和独立的 review 总览，也支持按目录分组的 Work 时间线。它的顶栏带有和 app 一样的本地数据新鲜度标记。按 `?` 查看快捷键。

## 宁可留白，也不瞎猜

agentacct 还处于 early alpha 阶段。它的原则是：宁可给你看一个空缺，也不给你一个猜测。

- **Reported 不等于 Verified。** agent 自己的说法，永远不会被包装成验证结果。
- **估算就明说是估算。** `≈$` 表示按价目表估算，`~$` 表示已知不完整的小计；agentacct 拿不到账单数据。每条路径能证明什么，见 [docs/usage-truth-table.md](docs/usage-truth-table.md)。
- **宁缺毋错。** 用量和记录的工作之间的每一次关联都带置信度（`exact`、`high`、`medium`、`low`）；证明不了的关联就显示为空缺，而不是一个零。
- **按能力算支持，不按 logo 算。** 目前 Claude Code 和 Codex 的收据最完整；[coverage matrix](docs/coverage-matrix.md) 对 OpenCode、Hermes、DeepSeek Harness、Kimi Code、OpenClaw、Cursor 的每一项能力单独评级，`agentacct capabilities agents` 会打印你当前版本对应的这张表。
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
