# QQ 邮箱 MCP 服务器

把「发邮件」能力封装成符合 [MCP（Model Context Protocol）](https://modelcontextprotocol.io)
规范的服务，让 AI 智能体能够通过标准协议代你发送邮件。

服务基于 FastAPI + 官方 MCP Python SDK 的 **Streamable HTTP** 传输层实现，
对外暴露 4 个邮件工具；仓库内附带一个开箱即用的**邮件管家**客户端
（`email_butler.py`：单入口启动、多轮对话记忆、动作可见）。

---

## 文档导航

本仓库有三份文档，按用途分工：

| 文档 | 用途 | 适合谁 |
|---|---|---|
| **[PROJECT.md](PROJECT.md)** | **项目简介** —— 是什么、解决什么问题、技术要点 | 第一次了解这个项目的人 |
| **[MANUAL.md](MANUAL.md)** | **使用手册** —— 从零到发信的操作步骤、故障排查 | 要用它发邮件的人 |
| **README.md**（本文件） | **设计文档** —— 架构、设计决策与取舍、协议细节 | 想读代码、关心实现的人 |

**只想尽快发出第一封邮件** → 直接看 [MANUAL.md](MANUAL.md)。

---

## 目录

- [文档导航](#文档导航)
- [核心特性](#核心特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [配置](#配置)
- [可用工具](#可用工具)
- [HTTP 端点](#http-端点)
- [使用方式](#使用方式)
- [测试](#测试)
- [可观测性](#可观测性)
- [设计决策与取舍](#设计决策与取舍)
- [发送确认（IMAP 回读）](#发送确认imap-回读)
- [重试与幂等](#重试与幂等)
- [项目结构](#项目结构)
- [安全须知](#安全须知)

---

## 核心特性

| 特性 | 说明 |
|---|---|
| **对话式发信** | `email_butler.py` 单入口启动，用中文说"给我自己发封邮件，说早上好"即可；带多轮记忆，能接住"主题改成…"这类追问 |
| **剪贴板直发图片/文件** | 复制图片或压缩包后输入 `/粘贴`，自动落地成附件。终端本身粘不出图片，也粘不出文件路径，因此直接读剪贴板 |
| **标准 MCP 协议** | 使用官方 Streamable HTTP 传输层，支持完整的 `initialize` 握手与会话管理，任意合规 MCP 客户端均可接入 |
| **4 个邮件工具** | 纯文本 / HTML / 附件邮件，以及配置检查 |
| **SMTP 连接复用** | 专用工作线程独占连接，实测 5 封邮件仅建立 1 条 TLS 连接 |
| **结构化日志** | 可选单行 JSON 输出，便于日志系统采集 |
| **发送指标** | 成功率、延迟分位数、失败原因分布，经 `/metrics` 暴露 |
| **零外部依赖的测试** | 替换 SMTP/IMAP 层与注入替身，290 个用例不联网、不碰真实邮箱、约 3.5 秒跑完 |

---

## 架构

```
                        ┌─────────────────────────────────────────┐
                        │              MCP 客户端                 │
                        │  email_butler.py （邮件管家，日常入口）  │
                        │  （任何合规 MCP 客户端都可接入）         │
                        └────────────────────┬────────────────────┘
                                             │  MCP over HTTP (JSON-RPC)
                                             │  POST /mcp
┌────────────────────────────────────────────▼────────────────────────────────┐
│  mcp_server.py  ——  FastAPI 应用                                            │
│                                                                             │
│   lifespan ──► StreamableHTTPSessionManager.run()                           │
│   /mcp     ──► 官方传输层（原生 ASGI 挂载，非 FastAPI 路径函数）              │
│   /health  ──► 健康检查        /metrics ──► 指标快照    /tools ──► 工具清单  │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │  SDK 依据 inputSchema 校验入参
                                     ▼
                        ┌────────────────────────┐
                        │  tool_defs.py          │  ← 工具定义的唯一真相来源
                        │  · TOOL_DEFS           │
                        │  · validate_arguments  │
                        │  · dispatch_tool       │  ← 可注入 email_tools，便于测试
                        └───────────┬────────────┘
                                    ▼
                        ┌────────────────────────┐
                        │  email_tools.py        │
                        │  · build_message       │  ← MIME 组装
                        │  · QQMailSender        │  ← 发送 + 结果归一 + 打点
                        │  · QQMailTools         │  ← 异步封装
                        └───────────┬────────────┘
                                    ▼
                        ┌────────────────────────┐
                        │  smtp_pool.py          │  ← 连接复用
                        │  专用线程 + 队列        │
                        │  单条 SMTP_SSL 连接     │
                        └───────────┬────────────┘
                                    ▼
                              QQ 邮箱 SMTP
                            (smtp.qq.com:465)
```

**分层意图**：`tool_defs.py` 刻意不依赖 FastAPI、也不依赖全局单例，
`email_tools` 以参数注入。因此分发逻辑可以用假的 `email_tools` 做纯单元测试，
无需真实 SMTP 与网络。

---

## 快速开始

### 1. 环境要求

- Python 3.11+
- [Ollama](https://ollama.com)（仅对话管家 `email_butler.py` 需要）

> **`venv/` 不在仓库里**（体积大且与平台相关）。克隆后必须自己建环境，
> 否则下面命令里的 `venv\Scripts\python.exe` 不存在。

### 2. 安装

```bash
cd <你克隆项目的目录>/qqmail-mcp-tool

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
pip install -r requirements-dev.txt   # 仅开发/测试需要
```

### 3. 配置

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

编辑 `.env`，至少填写 `SMTP_EMAIL` 与 `SMTP_PASSWORD`。
**`SMTP_PASSWORD` 填的是 QQ 邮箱授权码，不是 QQ 登录密码。**

获取授权码：登录 QQ 邮箱网页版 → 设置 → 账户 →
开启「IMAP/SMTP 服务」→ 生成 16 位授权码。
授权码只显示一次，请立即保存到 `.env`。

> ⚠️ **只想跑测试、暂时没有 QQ 授权码**时，可以先用占位配置：
>
> ```bash
> copy .env.test .env      # 只有假数据，测试全程不发起真实请求
> python -m pytest -q      # 应看到 290 passed
> ```
>
> `SMTP_EMAIL` 与 `SMTP_PASSWORD` 是**必填**项，两者都缺失时测试会在
> 收集阶段就抛 `ValidationError` —— 这不是环境坏了，只是还没配置。

### 4. 启动 MCP 服务器

```bash
python run_server.py
```

启动后访问 <http://localhost:8000/health> 应返回 `{"status": "healthy", ...}`。

### 5.（可选）使用智能体客户端

先确保 Ollama 已运行并已拉取模型：

```bash
ollama serve
ollama pull qwen2.5:3b
```

然后启动管家，直接跟它说话（它会自己拉起 MCP 服务器）：

```bash
python email_butler.py
```

也可以双击根目录的 `启动管家.bat`。

需要手动控制服务器时（排查问题、或接入自己的客户端）：

```bash
python run_server.py
```

---

## 配置

所有配置经 `.env` 读入，由 `config.py` 中的 `Settings`（pydantic-settings）校验。

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `SMTP_EMAIL` | ✅ | — | 发件邮箱地址 |
| `SMTP_PASSWORD` | ✅ | — | QQ 邮箱授权码（非登录密码） |
| `SMTP_SERVER` | | `smtp.qq.com` | SMTP 服务器 |
| `SMTP_PORT` | | `465` | SSL 端口 |
| `MCP_HOST` | | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | | `8000` | 监听端口 |
| `MCP_AUTH_TOKEN` | | 未设置 | `/mcp` 的 Bearer 令牌；未设置则不启用鉴权（见[访问控制](#访问控制)） |
| `DEBUG` | | `true` | 调试日志 |
| `OLLAMA_BASE_URL` | | `http://localhost:11434` | Ollama 地址 |
| `OLLAMA_MODEL` | | `qwen2.5:3b` | 使用的模型 |
| `JSON_LOGS` | | 未设置 | 设为 `1`/`true`/`yes` 时日志输出为单行 JSON |
| `EMAIL_CONFIRM_DELIVERY` | | `false` | 是否用 IMAP 回读确认发送（见[发送确认](#发送确认imap-回读)） |
| `SEND_MAX_ATTEMPTS` | | `3` | 发送总尝试次数（含首次）；`1` 表示不重试 |
| `MAX_ATTACHMENT_BYTES` | | `10 MB` | 单个附件上限（原始大小，见[附件行为](#附件行为两条容易踩的规则)） |
| `MAX_TOTAL_ATTACHMENT_BYTES` | | `18 MB` | 全部附件合计上限（原始大小，编码后约 24MB） |

`SMTP_EMAIL` 与 `SMTP_PASSWORD` **没有默认值**：缺失时进程启动即报
`ValidationError`，不会静默回退到某个内置凭据。这条约束由测试固化
（见 `tests/test_config.py`）。

---

## 可用工具

工具定义位于 `tool_defs.py`，是 `tools/list`、`/tools` 与入参校验的共同来源。

| 工具 | 参数 | 说明 |
|---|---|---|
| `send_text_email` | `to_email`, `subject`, `body`, `cc?`, `bcc?`, `idempotency_key?` | 纯文本邮件 |
| `send_html_email` | `to_email`, `subject`, `html_body`, `cc?`, `bcc?`, `idempotency_key?` | HTML 邮件（自动附带纯文本回退） |
| `send_email_with_attachment` | `to_email`, `subject`, `attachment_paths`, `body?`, `cc?`, `bcc?`, `is_html?`, `idempotency_key?` | 带附件邮件；省略 `body` 时自动生成正文 |
| `check_email_config` | — | 检查 SMTP 配置与连通性 |

`to_email` 接受单个字符串或字符串数组。

### 附件行为（三条容易踩的规则）

**1. 附件找不到会中止发送，而不是悄悄发出去。**

早期实现是「静默跳过缺失附件、照样返回发送成功」——结果是用户以为附件发出去了，
对方实际只收到一封空邮件。这类静默失败极难察觉，因此改为明确失败：

```
❌ 附件不存在，邮件未发送：D:\报表\7月.xlsx
请确认文件路径是否正确，或先确认文件是否仍然存在。
```

此时**连 SMTP 连接都不会建立**。

**2. 文件名说错时会尝试修正，并告知你替换了什么。**

小模型常把文件名拼错或猜错扩展名（实测：目录里是 `示例报表.csv`，
模型给出 `示例报表.xlsx`），也常只回一个裸文件名或把路径包在引号里。
因此路径解析按下列顺序尝试：

| 顺序 | 规则 | 例子 |
|---|---|---|
| 1 | 原路径存在 → 原样使用 | — |
| 2 | 剥掉两端引号与空白后再试 | `"D:\照片\团建.jpg"` → 成功 |
| 3 | 在**给定目录**中主干一致，仅扩展名不同 | `示例报表.xlsx` → `示例报表.csv` |
| 4 | 在**给定目录**中归一化一致（裁掉「表/文件/文档/附件」等修饰词） | `示例报表表.xlsx` → `示例报表.csv` |
| 5 | 在**附件目录**中重试 3–4 | 裸名 `示例报表.csv` → 附件目录里的同名文件 |
| 6 | **候选不唯一 → 不猜，报错** | `数据.txt`（同时存在 `.csv` 和 `.xlsx`）|

第 2 步和第 5 步都是实测踩出来的：

- **引号**在 Windows 上是合法文件名字符，所以 `"D:\照片\团建.jpg"` 这种写法
  会让 `is_file()` 判定失败，一个完全正确的路径被误报成「附件不存在」。
- **裸文件名**的 parent 是「当前工作目录」而非附件目录。而系统提示词已经把
  附件目录的真实文件名列给了模型，模型很自然地只回 `示例报表.csv` ——
  于是这个**确实存在**的文件反而判为不存在，整封邮件发不出去。
  修复前实测：`success=False`，`reason=missing_attachment`。

发生替换时，结果里会写明，避免「悄悄换了另一个文件」而用户不知情
（只是剥引号或去空格不算替换，不提示）：

```
✅ 邮件发送成功
附件: 示例报表.csv
（附件名已自动修正：示例报表.xlsx → 示例报表.csv）
```

第 4 条是刻意加的保护：**多个候选时宁可报错，也不发出用户没指定的文件。**

**3. 附件大小有上限。**

超限在发送前就拒绝，而不是等 SMTP 回一个难懂的英文错误：

| 配置 | 默认 | 说明 |
|---|---|---|
| `MAX_ATTACHMENT_BYTES` | `10 MB` | 单个附件上限（原始大小） |
| `MAX_TOTAL_ATTACHMENT_BYTES` | `18 MB` | 全部附件合计上限（原始大小，编码后约 24MB） |
| `ATTACHMENT_ENCODING_RATIO` | `4/3` | Base64 膨胀系数，用于把原始大小换算成传输体积 |

**判断口径是编码后的体积，不是文件属性里显示的大小。** 原因：QQ 限制的是
传输中的字节数，而 Base64 编码每 3 字节原文变成 4 字节，体积增加约 1/3。
若按原始大小比较（早期实现就是如此），18MB 的文件实际传输约 24MB，
余量会算错。18MB 的默认值即由此反推：18 × 1.33 ≈ 24MB，低于 QQ 的约 25MB 上限。

单件超限与合计超限会**分开报告**——前者通常是选错了文件，后者是「每件都不大但加起来太多」。
错误提示里会同时给出原始大小、编码后估算与精确字节数，便于判断差距。

---

## HTTP 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/mcp` | **MCP 协议端点**（Streamable HTTP）。需先 `initialize` 握手 |
| `GET` | `/` | 服务信息 |
| `GET` | `/health` | 健康检查（不触发外部连接） |
| `GET` | `/metrics` | 发送指标快照 |
| `GET` | `/tools` | 以纯 JSON 列出工具，便于人工查看 |

REST 端点仅用于人工排查，不参与 MCP 协议。

---

## 使用方式

项目自带一个对话客户端 **`email_butler.py`（邮件管家）**，
它自己拉起 MCP 服务器、维护多轮对话记忆、并把工具调用显示出来。

MCP 协议层本身是开放接口：任何合规 MCP 客户端都能连 `POST /mcp`，
不必使用本项目提供的客户端（见下方[直接调用 HTTP 端点](#直接调用-http-端点)）。

### 邮件管家（日常入口）

```bash
python email_butler.py
```

或者双击根目录的 **`启动管家.bat`**。

它会**自己启动 MCP 服务器**，你只需要跟它说话：

```
你 ▸ 给我自己发封邮件，说早上好
  ⚙  发送邮件
       收件人: you@qq.com
       主题: 早上好
  ↩  ✅ 邮件发送成功
  ⏱  2.3 秒

小邮 ▸ 已经把邮件发给自己了，主题是"早上好"。

你 ▸ 主题改成明天下午三点的会议提醒
  ⚙  发送邮件
       收件人: you@qq.com
       主题: 明天下午三点的会议提醒
  ↩  ✅ 邮件发送成功

小邮 ▸ 已经把邮件主题改成"明天下午三点的会议提醒"，并发送成功了。

你 ▸ /paste                       # 复制图片或文件后用它导入
  剪贴板里是：图片 1920×1080
  ✅ 已放入附件目录：
     剪贴板图片_20260913_211530.png（412.3 KB，已复制进来）

  现在可以说：「把 剪贴板图片_20260913_211530.png 发给我自己」

你 ▸ 把它发给我自己
  ⚙  发送带附件的邮件
       收件人: you@qq.com
       主题: 剪贴板图片_20260913_211530
       附件: 剪贴板图片_20260913_211530.png
  ↩  ✅ 邮件发送成功
```

`/粘贴`（或 `/paste`、`/p`）把剪贴板内容落地成附件：

| 你先做 | 它会 |
|---|---|
| 截图后复制图片，或右键复制网页图片 | 存成 `attachments\剪贴板图片_<时间>.png` |
| 在资源管理器里选中文件/压缩包 Ctrl+C | 复制进 `attachments\`，文件名不变 |

**为什么需要这条指令**：终端只处理文本。实测在资源管理器里复制文件后，
剪贴板里**只有文件格式（`CF_HDROP`）、没有文本格式**，所以 Ctrl+V
什么也粘不出来；图片更是完全粘不出来。因此只能由代码直接读剪贴板。

**为什么做成显式指令而不是自动检测**：自动导入意味着「你没打算发的
东西也可能被带走」，而这个工具是真能往外发邮件的。

相比早期版本，它解决了三件实际影响体验的事：

1. **单入口**：自己管理服务器子进程，不必再开一个窗口跑 `run_server.py`；
   若检测到端口上已有服务器在跑，会直接复用而不是重复启动。
2. **多轮记忆**：每轮把完整对话历史交给智能体。此前每轮只传当前一句，
   导致"主题改成…"这类追问完全接不住——它不知道你在说哪封邮件。
3. **动作可见**：把调用了哪个工具、返回了什么打出来，
   而不是只给一句结论。失败时也能看出卡在哪一步。

前置条件只有 Ollama（服务器由它自己起）：

```bash
ollama serve
ollama pull qwen2.5:3b
```

Ollama 没启动或模型没拉取时，它会直接给出可照抄的命令，而不是抛一个栈。

> **关于提示词的一点实测经验**：对 qwen2.5:3b 这样的小模型，
> 提示词里只要留了"可以先问一下用户"的余地，它就会不停反问
> （"需要我添加正文吗？"），结果一封邮件都发不出去。
> 因此管家的提示词明确要求**立即调用工具真正发出邮件**——
> 换成这样的措辞后，每一轮才都能落实为真实发送。

### 接入自己的客户端

MCP 协议层是开放的，任何合规客户端都能连 `POST /mcp`：

- `/tools` 列出全部工具及其完整 JSON Schema（浏览器可直接打开）
- `/mcp` 接受 JSON-RPC 请求，需先完成 `initialize` 握手（见[访问控制](#访问控制)）

本项目不内置第三方框架的示例客户端，以免与 `email_butler.py` 功能重叠——
一个能对话、另一个不能，反而让人挑错入口。

⚠️ **不要连续快速发多封。** QQ 对单账号有频率限制，
触发后会返回 `550 Too many attempts`，需要等待数小时恢复。

---

## 测试

```bash
python -m pytest            # 全部用例
python -m pytest -q         # 精简输出
python -m pytest tests/test_email_tools.py -v
```

套件共 290 个用例，**全程不发起真实网络请求**：

| 文件 | 关注点 |
|---|---|
| `tests/test_config.py` | 配置加载；**证明凭据无硬编码默认值** |
| `tests/test_auth.py` | **鉴权**：令牌解析、401 响应、凭据剥离、启动期安全告警 |
| `tests/test_delivery.py` | **发送确认**：主题 MIME 解码匹配、收件人校验、重试轮询、失败降级 |
| `tests/test_retry.py` | **重试分类**：4xx 可重试 / 5xx 不可重试、退避上限、次数耗尽的传播 |
| `tests/test_idempotency.py` | **幂等键**：重复请求不再发送、TTL 过期、卡死占用回收 |
| `tests/test_email_butler.py` | **管家**：多轮记忆、只显示本轮动作、Ollama 检查、附件列举、提示词约束、`/粘贴` 指令、GBK 下的输出加固 |
| `tests/test_clipboard.py` | **剪贴板导入**：位图逐像素解析（行对齐、自下而上、BGRA→RGB）、文件复制、超限跳过、PNG 落地 |
| `tests/test_email_tools.py` | MIME 结构、UTF-8 编码、附件、连接复用、指标、错误处理 |
| `tests/test_mcp_server.py` | REST 端点 + **真实 MCP 协议握手与工具调用** |
| `tests/test_tool_defs.py` | 工具注册表一致性、参数校验、分发、超时 |

### 隔离手段

- **SMTP 层替身**：`tests/conftest.py` 用 `FakeSMTPServer` 替换
  `smtplib.SMTP_SSL`。相比启动真实 SMTP 服务器，这种方式能精确模拟
  QQ 邮箱的非标准响应（见下），且不依赖额外依赖包。
- **进程内 MCP 会话**：用 `httpx.ASGITransport` 把官方 MCP 客户端直接接到
  FastAPI 应用上，不占用端口、不启动服务器。需显式进入应用 lifespan，
  因为会话管理器在其中启动。
- **应用工厂**：每个用例调用 `create_app()` 得到全新实例。这不是强迫症——
  `StreamableHTTPSessionManager.run()` 每个实例只允许调用一次，
  复用模块级单例会导致第二个用例直接报错。

CI 见 `.github/workflows/tests.yml`，在 Python 3.11 上执行上述套件。

---

## 可观测性

### 指标

`GET /metrics` 返回发送统计。下面是一次**实测捕获**的输出
（当时 QQ 邮箱恰好触发了频率限制，因此全部失败——这恰好演示了指标如何如实归因）：

```json
{
  "counters": {
    "sends_total": 5,
    "sends_failed": 5,
    "smtp_connects": 1
  },
  "latency_ms": {
    "samples": 5,
    "min": 422.0,
    "p50": 453.0,
    "p95": 828.0,
    "max": 828.0,
    "avg": 531.2
  },
  "failures_by_reason": { "smtp_reject": 5 },
  "connects_per_send": 0.2,
  "success_rate": 0.0
}
```

从这一份输出可以同时读出两件事：

- `smtp_connects: 1` 而 `sends_total: 5` —— **连接复用生效**，
  即便 5 次投递全部被拒，也只建立了一条 TLS 连接。
- `success_rate: 0.0` 与 `failures_by_reason.smtp_reject: 5` ——
  **失败被如实记录**，没有被掩盖为成功。

**`connects_per_send` 是验证连接复用的关键指标**：远小于 1 说明复用生效，
接近 1 说明每封信都在重新握手。

`failures_by_reason` 的取值：`smtp_reject`（服务端拒绝）、`auth_error`、
`transport_error`、`build_error`、`write_error`。

> 触发限流时服务端返回 `550 Too many attempts. Unable to send. Try again later`，
> 属于 `smtp_reject`。服务端同时会记录包含错误码的日志，便于排查：
> `SMTP服务器拒绝 code=550`。

### 日志

默认输出人类可读格式。设置 `JSON_LOGS=1` 后切换为单行 JSON：

```json
{"ts": "2026-09-13T16:28:55", "level": "INFO", "logger": "email_tools", "event": "发送成功 elapsed_ms=515.2 recipients=1", "recipients_count": 3}
```

通过 `extra=` 传入的业务字段会被自动并入 JSON，无需改动 formatter。

---

## 设计决策与取舍

这一节记录几个不明显但有依据的选择。面试时被追问的通常就是这些点。

### 为什么用官方 Streamable HTTP 传输层，而不是手写 JSON-RPC

早期版本自行实现了一个 `/mcp` 端点，只处理 `tools/list` 与 `tools/call`，
**没有 `initialize` 握手，也没有会话管理**。任何合规的 MCP 客户端连上来
第一步就会失败。当时为了绕开这个问题，代码额外暴露了一个直连接口
`/api/send-email-sync`，客户端优先调用它——结果是：项目自称 MCP 服务器，
**核心功能却不经过 MCP 协议**。

现已删除该后门，改用官方传输层，`initialize` → `tools/list` → `tools/call`
全链路经真实协议栈验证。

> 另一处细节：官方传输层是直接读写 ASGI `scope`/`receive`/`send` 的原生应用，
> 不能注册为普通 FastAPI 路径函数——Starlette 会把返回值当成 `Response`
> 再序列化一次，导致 422。因此以原生 ASGI 形式挂载。
> 同时额外注册精确路径 `/mcp`，避免 `Mount` 的路径规范化产生 307 重定向
> （实测一次智能体运行会因此产生 6 次多余往返）。

### 为什么入参校验只做「键是否存在」

MCP SDK 会依据 `inputSchema` 做完整的 jsonschema 校验（含类型检查）。
若分发层再自行判断类型，就会出现两份可能分歧的校验实现。
因此 `validate_arguments()` 只负责键的存在性，并在 `MissingArgumentsError`
里给出可读提示；类型错误交由 SDK 拦截。

### 为什么把 `send_email_with_attachment` 的 `body` 改为非必填

原实现允许省略 `body` 并自动生成正文，但 `inputSchema` 把它列在 `required` 里。
重构时统一走 schema 校验后，这个不一致导致原本可用的调用被拒绝。
修法是让 schema 反映真实契约（`body` 移出 `required`、加 `default: ""`），
而不是放宽校验——**schema 与实现对同一件事必须有相同的说法**。

### 为什么连接复用要串行化发送

`smtplib.SMTP_SSL` 不是线程安全的，连接不能跨线程共享。
而发送经 `asyncio.to_thread` 执行，任务可能落在任意线程上，
因此「线程本地连接」也无法保证复用。

方案是用**一个专用工作线程独占一条连接**，所有发送请求经队列排队。
这天然把发送变成串行。这是有意接受的代价：

- 连接复用省下的 TLS 握手 + 登录成本，实测约 **328ms/封（降低 36%）**；
- 单个 SMTP 账号下，并发发送只会更快触发 QQ 的限流，收益有限；
- 串行化彻底消除了并发使用同一连接的风险。

**实测对比**（均为真实 QQ SMTP，连发 5 封）：

| | 每封新建连接 | 连接复用 |
|---|---|---|
| 建立连接数 | 5 | **1** |
| 总耗时 | 4.53s | 2.89s |
| 平均每封 | 906 ms | **578 ms** |

若连接超过 60 秒未被使用，会在下次使用前主动重建，
避免用到已被服务端回收的连接。

若要提升吞吐，正确方向是**多账号 / 多连接池分片**，而不是给单条连接加锁。

### 两个容易踩的坑

**`smtplib.SMTPException` 继承自 `OSError`。** 连接失效的判定因此不能捕获
`OSError`——那会把 550、451、535 等协议错误全部误判为连接断裂，
导致每封信都重新握手。只有 `SMTPServerDisconnected` 与 `SMTPConnectError`
才表示传输层断裂。这条约束由 `tests/test_email_tools.py` 中的用例固化。

**必须使用 `langchain_ollama` 的 `ChatOllama`。** `langchain_community` 里
有一个同名类，但它**没有实现 `bind_tools`**，会让
`create_agent` 抛出 `NotImplementedError`。

**懒加载连接池必须加锁。** `QQMailSender.worker` 是延迟创建的，
而发送经 `asyncio.to_thread` 执行。裸的 check-then-act 写法在多线程
同时首次访问时，会让每个线程都看到 `_worker is None` 并各自建一个连接池——
实测 8 并发建立了 8 条本应唯一的连接。修法是加锁并做双重检查。

这个缺陷**在串行调用下完全无法暴露**，只有并发首次访问才会触发。
`tests/test_email_tools.py::test_worker_initialization_is_thread_safe`
用 `threading.Barrier` 让 16 个线程同时冲进该属性来稳定复现它
（已确认移除锁后该用例必然失败，因此不是装饰性测试）。

**配置应当在读取时解析，不要在构造时固化。** `QQMailTools.attachment_dir`
早期是构造时从 `settings` 复制的实例属性，于是「先构造对象、后修改配置」
不会生效——这曾导致测试隔离失效，测试写进了真实的 `attachments/` 目录。
现已改为只读属性，每次读取都从配置解析。这类"缓存配置"的写法很常见，
但会让对象的行为依赖构造顺序，尤其在测试里非常容易踩坑。

### 关于 QQ 邮箱的非标准响应

QQ 邮箱在邮件已实际投递的情况下，偶尔会返回 `(-1, b'\x00\x00\x00')`。
单看这个响应码无法区分「真的发出去了」和「出问题了」，因此本项目**不靠它下结论**，
而是引入独立证据：发送后用 IMAP 回读「已发送」文件夹（见下）。

除此之外，其他任何 `smtp_code` 都会被如实报告为失败——
不会为了"让演示通过"而把错误当作成功。

---

## 发送确认（IMAP 回读）

发送之后额外用 IMAP 查询 QQ 的「已发送」文件夹，确认这封信确实被 QQ
接收并归档。默认关闭，启用方式：

```
EMAIL_CONFIRM_DELIVERY=true
```

结果会附在工具返回值的 `confirmation` 字段中：

```json
{
  "success": true,
  "confirmation": {
    "checked": true,
    "confirmed": true,
    "detail": "已在「已发送」中找到该邮件，QQ 已接收并归档",
    "elapsed_ms": 1454.0,
    "attempts": 1,
    "matched_subject": "基线 #1"
  }
}
```

### 它能证明什么、不能证明什么

这一节比代码本身更重要，因为它界定了这个机制的边界。

**能证明**：QQ 接收了这封信并把它归档进了「已发送」。

**不能证明**：收件人真的收到了，也不代表收件地址存在。

实测发现了一个反直觉的事实：**被 SMTP 以 550 拒绝的邮件同样会出现在「已发送」里**。
所以「出现在已发送」**不等于**投递成功——它是发信方的存档行为，不是收件方的确认。

真正的投递确认必须读取收件人的收件箱，而那只有收件人自己有权访问。
在本项目的场景下无法实现。因此这里提供的是**最接近的可用证据**，
并在 `detail` 中如实标注结论强度，而不是含糊地说"已投递"。

### 三种状态如何解读

| `checked` | `confirmed` | 含义 |
|---|---|---|
| `true` | `true` | 找到了对应邮件，QQ 已接收并归档 |
| `true` | `false` | 未找到。可能是归档延迟、被过滤，或该响应码确实不代表成功 |
| `false` | — | 无法完成确认（IMAP 连不上、登录失败等）。**不代表发送失败** |

⚠️ **确认失败永远不会把发送结论改成失败**。`success` 只反映 SMTP 的实际结果；
确认只是附加证据。IMAP 故障时发送依旧报告成功，仅标注"未做确认"。

### 实现要点

- **匹配依据是「主题 + 收件人 + 时间窗口」，不是 Message-ID。**
  实测 QQ 会用自己生成的 Message-ID（`<tencent_...@qq.com>`）覆盖发信方设置的值，
  因此无法用预设的 Message-ID 匹配。
- **主题需先做 MIME 解码再比较**，QQ 存储的是 Q-encoded 形式。
- **带重试轮询**（默认 3 次、间隔 2 秒）：归档有延迟，单次查询容易得到假阴性。
- **文件夹名必须带引号**传给 `imaplib`：`"Sent Messages"` 含空格，
  而 `imaplib` 不会自动加引号，否则 QQ 返回 `BAD EXAMINE parameters!`。
- **连接类错误不重试**：IMAP 连不上时反复重试没有意义，立即返回。
- 每次确认约增加 1-3 秒（实测命中约 1.5 秒、未命中约 3 秒），
  这是默认关闭的原因。

相关的可用配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMAIL_CONFIRM_DELIVERY` | `false` | 是否启用发送确认 |
| `IMAP_SERVER` | `imap.qq.com` | IMAP 服务器（与 SMTP 共用授权码） |
| `IMAP_PORT` | `993` | IMAP SSL 端口 |
| `IMAP_SENT_FOLDER` | `"Sent Messages"` | 已发送文件夹名（含引号） |
| `CONFIRM_ATTEMPTS` | `3` | 轮询次数 |
| `CONFIRM_INTERVAL` | `2.0` | 轮询间隔（秒） |

---

## 重试与幂等

发送失败并不都一样：有些重试就好，有些重试多少次都一样。
本项目对两者区别对待。

### 错误分类

| 类别 | 例子 | 处理 |
|---|---|---|
| **瞬时**（可重试） | `421` 服务不可用、`450/451/452` 稍后重试、连接断裂、超时 | 按策略重试 |
| **永久**（不可重试） | `550` 收件人不存在、`535` 认证失败 | 立即失败，不浪费配额 |

默认最多尝试 3 次（`SEND_MAX_ATTEMPTS`），退避 1s → 2s，单次等待上限 8s。

⚠️ **认证失败永远不重试**，即便它带了 4xx 码。`SMTPAuthenticationError` 是
`SMTPResponseException` 的子类，因此判定的顺序必须让永久错误优先——
这一点由 `tests/test_retry.py` 固化。

### 重试的安全边界（重要）

SMTP 是 **at-least-once** 语义，盲目重试会造成重复邮件：

- 错误发生在**连接、登录、MAIL/RCPT** 阶段 → 能确定服务器**没有**接收，重试安全。
- 断开发生在**正文传输之后** → 服务器可能已经收下，重试**会重复发信**。

因此连接在发送中断裂时是否重连重试，由 `SEND_RETRY_ON_RECONNECT` 控制：

- **默认开启**：更常见的故障是空闲连接在传输正文前被回收，此时重发安全。
- 若你的场景**更不能容忍重复邮件**，请置为 `false`，并改用下面的幂等键。

这是无法完全消除的取舍，README 选择把它写明，而不是假装不存在。

### 幂等键

调用发送类工具时传入 `idempotency_key`，同一键在有效期内只会真正发送一次：

```json
{
  "name": "send_text_email",
  "arguments": {
    "to_email": "someone@example.com",
    "subject": "订单确认",
    "body": "...",
    "idempotency_key": "order-12345-confirm"
  }
}
```

重复提交会返回首次的结果，并带上 `duplicate: true`：

```
✅ 邮件发送成功（重复请求，未再次发送）
```

行为说明：

- 首次请求**仍在处理中**时，重复请求立即返回「处理中」，不会重复发送。
- 首次因**永久错误**失败时会**释放**键，允许调用方修正参数后重用同一键。
- 键默认保留 1 小时（`IDEMPOTENCY_TTL`）。
- **被幂等拦截的请求不计入 `sends_total`**，因此成功率等指标不会被重复请求污染。

实测（真实服务器，三次调用）：

```
sends_total     = 2      # 第一次 + 不同键的第三次
idempotent_hits = 1      # 第二次被拦截
smtp_connects   = 1      # 三次调用共用一条连接
```

**局限**：这是**单进程内存**实现。进程重启后键失效；多副本部署时各副本互不可见。
要跨进程/跨重启生效，需要换成 Redis 之类的共享存储。

---

## 项目结构

```
qqmail-mcp-tool/
├── mcp_server.py            # FastAPI + MCP 服务器（create_app 工厂）
├── run_server.py            # 启动脚本
├── tool_defs.py             # 工具定义与分发的唯一真相来源
├── email_tools.py           # MIME 构建、发送、结果归一、日志与打点
├── smtp_pool.py             # SMTP 连接复用（专用线程 + 队列）
├── metrics.py               # 发送指标
├── config.py                # 配置（pydantic-settings）
├── auth.py                  # /mcp 的 Bearer 令牌鉴权
├── delivery.py              # 发送确认（IMAP 回读「已发送」）
├── retry.py                 # 重试策略与错误分类
├── idempotency.py           # 幂等键（防止重复发信）
├── email_butler.py          # 邮件管家（推荐入口：自动起服务器 + 多轮记忆）
├── clipboard.py             # 剪贴板导入（/粘贴 指令：图片与文件）
├── 启动管家.bat              # 双击启动管家（仅含 ASCII，中文提示由 Python 输出）
├── create_attachments.py    # 生成示例附件（报表/纪要/配置）
├── requirements.txt         # 运行时依赖
├── requirements-dev.txt     # 测试依赖
├── pytest.ini               # pytest 配置
├── PROJECT.md               # 文档：项目简介
├── MANUAL.md                # 文档：使用手册
├── README.md                # 文档：设计说明（本文件）
├── .env.example             # 配置模板（可提交）
├── .env.test                # CI 用占位配置
├── .github/workflows/tests.yml
├── tests/                   # 290 个用例
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_auth.py
│   ├── test_delivery.py
│   ├── test_retry.py
│   ├── test_idempotency.py
│   ├── test_email_butler.py
│   ├── test_clipboard.py
│   ├── test_email_tools.py
│   ├── test_mcp_server.py
│   └── test_tool_defs.py
└── attachments/             # 附件目录（示例附件由 create_attachments.py 生成，/粘贴 也放这里）
```

---

## 安全须知

- **`.env` 绝不入库**。`.gitignore` 已覆盖 `.env`、`.env.*`（并反向放行
  `.env.example`）、`venv/`，以及 `attachments/` 下由程序生成的 JSON。
- **凭据只经 `.env` 流转**。不要把它写进源码、提交信息、issue 或截图。
  本项目所有读取凭据的位置都集中在 `config.py`，源码中不存在硬编码凭据
  （由 `tests/test_config.py` 固化）。
- **授权码泄露后应立即在 QQ 邮箱重新生成**，旧码随即失效。

### 访问控制

`/mcp` 支持 Bearer 令牌鉴权。MCP 工具具备真实发信能力，因此**对外提供服务时
必须启用**：

```bash
# 生成一个随机令牌
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

把它写进 `.env`：

```
MCP_AUTH_TOKEN=<上一步生成的令牌>
```

启用后，对 `/mcp` 的请求必须携带该令牌，否则返回 `401` 并附
`WWW-Authenticate: Bearer realm="qqmail-mcp"`：

```
Authorization: Bearer <你的令牌>
```

MCP 客户端配置自定义请求头的方式各不相同，例如使用官方 SDK 时：

```python
import httpx
from mcp.client.streamable_http import streamablehttp_client

def factory(**_):
    return httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})

async with streamablehttp_client("http://127.0.0.1:8000/mcp",
                                httpx_client_factory=factory) as (read, write, _):
    ...
```

行为说明：

- **未设置 `MCP_AUTH_TOKEN` 时不启用鉴权**，便于本机开发。此时若监听在
  非回环地址，启动日志会打印醒目警告。
- **`/health` 始终不要求凭据**，因为它用于探活；但它会返回配置的发件邮箱，
  介意的话请勿对外暴露该端点。
- 令牌比较使用 `secrets.compare_digest`，避免通过响应时间差逐字节猜测。
- 鉴权通过后 `Authorization` 头会从请求中移除，不传递给下游。
- 未通过鉴权的请求**不会建立任何 MCP 会话**。

推荐的最小安全配置：

```
MCP_HOST=127.0.0.1        # 只监听本机
MCP_AUTH_TOKEN=<随机令牌>  # 即使改了监听地址也有访问控制
```

**尚未实现**（超出当前范围）：TLS/HTTPS、令牌轮换、多用户与细粒度权限、
请求审计日志。若要真正暴露到公网，建议在前面加一层反向代理处理 TLS 与限流。
