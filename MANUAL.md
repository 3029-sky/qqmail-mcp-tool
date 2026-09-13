# 使用手册

从零到发出第一封邮件的完整操作步骤，以及常见故障的排查方法。

> 项目是做什么的、技术怎么实现的，见 `PROJECT.md` 与 `README.md`。
> 本文件只讲**怎么用**。

---

## 目录

- [一、前提条件](#一前提条件)
- [二、首次配置](#二首次配置)
- [三、日常发信](#三日常发信)
- [四、能说什么](#四能说什么)
- [五、附件怎么用](#五附件怎么用)
- [六、四个工具与端点](#六四个工具与端点)
- [七、配置项参考](#七配置项参考)
- [八、常见问题排查](#八常见问题排查)
- [九、可选高级功能](#九可选高级功能)
- [十、其他用法](#十其他用法)
- [十一、局限](#十一局限)

---

## 一、前提条件

| 需要什么 | 说明 |
|---|---|
| Python 3.11+ | 项目虚拟环境已建好（`venv/`） |
| Ollama | 本地大模型运行时，**必须先启动** |
| QQ 邮箱 | 一个 QQ 邮箱账号，并开启 SMTP 服务 |
| 网络 | 能访问 `smtp.qq.com` / `imap.qq.com` |

检查 Ollama 是否可用：

```powershell
ollama list
```

应能看到 `qwen2.5:3b`。若没有：

```powershell
ollama serve
ollama pull qwen2.5:3b
```

> **MCP 服务器不需要你启动** —— 管家会自己拉起它。

---

## 二、首次配置

### 第 1 步：确认凭据

项目根目录应有 `.env` 文件。若没有，从模板创建：

```powershell
cd E:\rise\qqmail\qqmail-mcp-tool
copy .env.example .env
```

用记事本打开 `.env`，**至少填两项**：

```ini
SMTP_EMAIL=你的QQ邮箱@qq.com
SMTP_PASSWORD=16位授权码
```

> ⚠️ **`SMTP_PASSWORD` 填的是「授权码」，不是 QQ 登录密码。**
>
> 获取方式：登录 QQ 邮箱网页版 → 设置 → 账户 →
> 开启「IMAP/SMTP 服务」→ 生成 16 位授权码。
> 授权码**只显示一次**，请立即保存进 `.env`。

### 第 2 步：验证配置

```powershell
.\venv\Scripts\python.exe -c "from config import settings; print('邮箱:', settings.smtp_email); print('授权码长度:', len(settings.smtp_password))"
```

授权码长度应为 **16**。若报 `ValidationError`，说明 `.env` 里缺项。

### 第 3 步：跑一次测试（可选但推荐）

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

看到 `253 passed` 说明环境完好。**这一步不需要网络、不碰邮箱**，
是最快的环境自检方式。

---

## 三、日常发信

### 启动

```powershell
cd E:\rise\qqmail\qqmail-mcp-tool
.\venv\Scripts\python.exe email_butler.py
```

看到这些就可以说话了：

```
· 正在启动 MCP 服务器…
· MCP 服务器就绪
· 已加载 5 个工具：发送邮件、发送 HTML 邮件、发送带附件的邮件、检查邮箱配置、保存配置
· 附件目录可用文件：周报.txt、测试数据.csv、…

╭──────────────────────────────────────────────╮
│  📬  邮件管家                                 │
╰──────────────────────────────────────────────╯
你好，我是小邮。发件邮箱是 你的邮箱@qq.com。

你 ▸
```

### 发第一封

```
你 ▸ 给我自己发封邮件，主题是周末安排，正文说周六上午十点碰面
```

它会显示自己做了什么：

```
  ⚙  发送邮件
       收件人: 你的邮箱@qq.com
       主题: 周末安排
  ↩  ✅ 邮件发送成功
  ⏱  2.5 秒

小邮 ▸ 邮件已经发送给自己了，主题是"周末安排"。
```

### 接着改（多轮对话）

不用重复说收件人和正文，它记得上下文：

```
你 ▸ 主题改成周日早上九点

  ⚙  发送邮件
       主题: 周日早上九点
```

### 退出

输入 `exit`、`quit`、`退出`、`q` 任一即可。
管家会顺手关掉它启动的服务器并清理连接。

---

## 四、能说什么

| 你想做的 | 这样说 |
|---|---|
| 发给自己 | 给我自己发封邮件，主题是X，正文说Y |
| 发给别人 | 给 zhangsan@example.com 发一封会议提醒 |
| 多收件人 | 发给 a@x.com 和 b@y.com |
| 抄送/密送 | ……抄送给 c@x.com |
| 改上一封 | 主题改成… / 正文改成… / 换成… |
| 换收件人 | 还是发给我自己吧 |
| 用 HTML | 发一封 HTML 格式的邮件，内容是… |
| 带附件 | 把 `周报.txt` 作为附件发给我自己 |
| 查配置 | 检查一下我的邮箱配置 |

**理解那三行符号**：

| 符号 | 含义 |
|---|---|
| `⚙` | 它**决定调用**的工具及其参数 |
| `↩` | 工具**真实返回**的结果 |
| `⏱` | 本轮耗时 |

这是刻意显示的——你能据此判断它是真发了还是在敷衍。
**回答文字不能改变已发出的邮件，只有 `⚙` 那几行才是实际执行的内容。**

---

## 五、附件怎么用

### 默认目录

附件默认放在项目的 `attachments\` 目录。目录里的文件会在启动时列出来。

```
你 ▸ 把 周报.txt 作为附件发给我自己
```

### 完整路径

也可以直接给完整路径：

```
你 ▸ 把 D:\报表\7月.xlsx 作为附件发给 zhangsan@example.com
```

### 文件名说错也能救回来

模型有时会猜错扩展名。项目会按「主干一致 → 归一化一致」逐级匹配修正，
**并在结果里说明替换了什么**：

| 你（或模型）说的 | 实际使用 |
|---|---|
| `测试数据.xlsx` | `测试数据.csv` |
| `测试数据表.xlsx` | `测试数据.csv` |
| `周报.doc` | `周报.txt` |
| `完全不相干.pdf` | ❌ 明确报错，**不猜** |

结果会显示：

```
  ↩  ✅ 邮件发送成功 附件: 测试数据.csv
```

### 三条硬性行为

**1. 附件不存在 → 邮件不会发出**

```
❌ 附件不存在，邮件未发送：D:\报表\7月.xlsx
请确认文件路径是否正确，或先确认文件是否仍然存在。
```

这是刻意设计的——早期版本会静默跳过附件照样返回"成功"，
结果是对方只收到一封没附件的空邮件。

**2. 多个候选时宁可报错**

若 `数据.txt` 在目录里同时匹配到 `数据.csv` 和 `数据.xlsx`，
它会报错让你说清楚，**绝不擅自选一个**。

**3. 大小有上限**

| 限制 | 默认值 |
|---|---|
| 单个附件 | 10 MB（原始大小） |
| 全部附件合计 | 18 MB（原始大小，编码后约 24MB） |

判断口径是**编码后体积**（Base64 每 3 字节变 4 字节，约增 1/3），
因为 QQ 限制的是传输字节数。超限会在发送前拒绝：

```
以下附件过大，超出单件上限 10.0 MB：
  · 大文件.bin：10.0 MB（编码后约 13.3 MB，即 10485760 字节）
建议：压缩后重发、拆成多封邮件，或改用网盘链接。
```

---

## 六、四个工具与端点

管家自己决定用哪个工具，你不需要记。以下是参考。

### 邮件工具（5 个）

| 工具 | 必填参数 | 可选参数 |
|---|---|---|
| `send_text_email` | 收件人、主题、正文 | 抄送、密送、幂等键 |
| `send_html_email` | 收件人、主题、HTML 正文 | 抄送、密送、幂等键 |
| `send_email_with_attachment` | 收件人、主题、附件路径 | 正文、抄送、密送、HTML、幂等键 |
| `check_email_config` | — | — |
| `save_environment_config` | 配置数据 | 文件名 |

### HTTP 端点（5 个）

自己启动服务器时可用（`.\venv\Scripts\python.exe run_server.py`）：

| 端点 | 用途 |
|---|---|
| `/health` | 探活，不触发外部连接 |
| `/tools` | 列出全部工具及其 schema（浏览器可直接打开） |
| `/metrics` | 发送统计：成功率、延迟、连接复用情况 |
| `/mcp` | MCP 协议端点，给客户端用 |
| `/` | 服务信息 |

**`/metrics` 值得一看**，能确认连接复用是否生效：

```json
{
  "counters": { "sends_total": 5, "sends_succeeded": 5, "smtp_connects": 1 },
  "connects_per_send": 0.2
}
```

`connects_per_send` 远小于 1 → 连接被复用；接近 1 → 每封信都在重新握手。

---

## 七、配置项参考

全部在 `.env` 中设置。**必填两项**，其余按需。

### 必填

| 配置 | 说明 |
|---|---|
| `SMTP_EMAIL` | 发件邮箱地址 |
| `SMTP_PASSWORD` | QQ 邮箱**授权码**（非登录密码） |

### 常用可选

| 配置 | 默认 | 作用 |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5:3b` | 换更大模型会更聪明（需机器扛得住） |
| `EMAIL_CONFIRM_DELIVERY` | `false` | 开启后用 IMAP 回读确认已归档（每封多 1-3 秒） |
| `SEND_MAX_ATTEMPTS` | `3` | 瞬时故障的重试次数；`1` 表示不重试 |
| `MCP_HOST` | `0.0.0.0` | 只用本机可改 `127.0.0.1` |
| `MCP_AUTH_TOKEN` | 未设置 | `/mcp` 访问令牌，对外暴露时必设 |
| `JSON_LOGS` | 未设置 | 设 `1` 输出单行 JSON 日志 |

### 进阶（一般不用改）

| 配置 | 默认 | 作用 |
|---|---|---|
| `SMTP_SERVER` / `SMTP_PORT` | `smtp.qq.com` / `465` | SMTP 服务器 |
| `MCP_PORT` | `8000` | 服务器端口 |
| `DEBUG` | `true` | 调试日志 |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 地址 |
| `IMAP_SERVER` / `IMAP_PORT` | `imap.qq.com` / `993` | 发送确认用 |
| `IMAP_SENT_FOLDER` | `"Sent Messages"` | 已发送文件夹名（含引号） |
| `CONFIRM_ATTEMPTS` / `CONFIRM_INTERVAL` | `3` / `2.0` | 确认的轮询次数与间隔 |
| `SEND_RETRY_INITIAL_DELAY` / `SEND_RETRY_BACKOFF` | `1.0` / `2.0` | 重试退避 |
| `SEND_RETRY_ON_RECONNECT` | `true` | 连接断裂时是否换连接重试（见 README 的取舍说明） |
| `IDEMPOTENCY_TTL` | `3600.0` | 幂等键保留时长（秒） |
| `IDEMPOTENCY_INFLIGHT_TIMEOUT` | `300.0` | 「处理中」被视为卡死的时长 |
| `ATTACHMENT_ENCODING_RATIO` | `4/3` | Base64 膨胀系数 |
| `MAX_ATTACHMENT_BYTES` | `10485760` | 单件上限（字节） |
| `MAX_TOTAL_ATTACHMENT_BYTES` | `18874368` | 合计上限（字节） |

---

## 八、常见问题排查

### `无法连接到 Ollama`

Ollama 没启动。运行 `ollama serve`，或先打开一次 Ollama 应用。

### `Ollama 中找不到模型 qwen2.5:3b`

```powershell
ollama pull qwen2.5:3b
```

### `邮箱认证失败` / 提示授权码错误

`.env` 里的 `SMTP_PASSWORD` 必须是**授权码**，不是 QQ 登录密码。

修正步骤：

1. 登录 QQ 邮箱网页版 → 设置 → 账户
2. 找到「POP3/IMAP/SMTP 服务」，确认已开启
3. **重新生成授权码**（旧的会立即失效）
4. 更新 `.env` 里的 `SMTP_PASSWORD`
5. 重新运行前面的验证命令

### `550 Too many attempts. Unable to send`

**限流，不是配置问题。** QQ 对单账号有频率限制。

- 等待数小时后再试（通常 2-3 小时）
- **不要连续重试**，会延长限制时间
- 演示时一次只发一封，中间隔开

### `附件不存在，邮件未发送`

路径错了或文件被移动。检查路径；若用模糊说法（如"那份表"），
改用确切文件名。

### 它只反问、不执行

提示词已明确要求「立即调用工具」，正常不该出现。
若仍发生，回一句「直接发」。

### 提示发送成功但对方没收到

先检查**垃圾邮件箱**。若开启了发送确认，看是否显示「已确认归档」。

> 注意：「发送成功」只代表**投递到服务器**，不代表对方收到。
> 发给不存在的地址也会返回成功，退信是之后的事。

### 双击/启动失败

用命令行运行能看到具体错误：

```powershell
cd E:\rise\qqmail\qqmail-mcp-tool
.\venv\Scripts\python.exe email_butler.py
```

若提示缺依赖：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

---

## 九、可选高级功能

### 发送确认（IMAP 回读）

`.env` 中开启：

```ini
EMAIL_CONFIRM_DELIVERY=true
```

发送后会额外用 IMAP 查询 QQ 的「已发送」文件夹，确认这封信被归档。
每次多花 1-3 秒。

**它能证明什么**：QQ 接收了并归档了。
**它不能证明什么**：收件人真的收到了。

> 实测发现：被 SMTP 以 550 拒绝的邮件**同样会出现在「已发送」里**，
> 因此它是"服务端已归档"的证据，**不是投递确认**。

三种结果：

| `checked` | `confirmed` | 含义 |
|---|---|---|
| true | true | 找到了，QQ 已接收并归档 |
| true | false | 未找到，可能延迟、被过滤，或该响应码确实不代表成功 |
| false | — | 无法确认（IMAP 故障）。**不代表发送失败** |

⚠️ **确认失败永远不会把发送结论改成失败**，`success` 只反映 SMTP 结果。

### 访问令牌（对外暴露时必设）

```powershell
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

把结果填进 `.env`：

```ini
MCP_AUTH_TOKEN=刚生成的令牌
```

启用后所有 `/mcp` 请求必须带 `Authorization: Bearer <token>`，
否则返回 401。客户端配置示例见 `README.md` 的「访问控制」一节。

### 换更大的模型

```ini
OLLAMA_MODEL=qwen2.5:7b
```

需要先 `ollama pull qwen2.5:7b`。模型越大越能正确理解意图、
越少乱猜附件名，但需要更多内存/显存。

---

## 十、其他用法

### 用脚本发信（适合定时任务）

**不需要服务器，也不需要 Ollama。**

```python
import asyncio
from quick_send import QuickSender   # 若该模块不存在，直接用 email_tools

# 推荐直接用 email_tools 的异步接口
from email_tools import email_tools

async def main():
    result = await email_tools.send_text_email(
        to_email="someone@example.com",
        subject="日报",
        body="附件是今天的报表",
    )
    print(result["message"])

asyncio.run(main())
```

**发多封时务必带幂等键**，避免重试造成重复邮件：

```python
await email_tools.send_text_email(
    to_email="someone@example.com",
    subject="订单确认",
    body="...",
    idempotency_key="order-12345",   # 同一个键只发一次
)
```

⚠️ 即便如此，**不要批量群发**——QQ 会限流。适合"一天几十封"以内的量。

### 自己启动 MCP 服务器

```powershell
.\venv\Scripts\python.exe run_server.py
```

管家会自动管理服务器；只有在你需要手动控制或排查时才需要这样。

启动后可用浏览器打开 `http://localhost:8000/tools` 查看全部工具及其参数定义。

### 接入自己的程序

MCP 协议层是开放的。若你想在自己的代码里调用，走 `POST /mcp`（JSON-RPC），
需先完成 `initialize` 握手，再发 `tools/call`。工具清单与完整 JSON Schema
可从 `GET /tools` 获取。

### 跑测试

```powershell
.\venv\Scripts\python.exe -m pytest -q              # 全部
.\venv\Scripts\python.exe -m pytest -v              # 详细
.\venv\Scripts\python.exe -m pytest tests/test_email_tools.py -v   # 单个文件
```

---

## 十一、局限

| 局限 | 说明 |
|---|---|
| **QQ 有限流** | 别连续快发，触发后需等数小时 |
| **"发送成功"≠对方收到** | 只代表投递到服务器；开 `EMAIL_CONFIRM_DELIVERY` 可多一层证据 |
| **小模型会自作主张** | 3B 模型在你没给全信息时会自己补全主题正文——看 `⚙` 那几行确认它用了什么 |
| **不支持回复/转发** | 未实现 `In-Reply-To` / `References` |
| **不支持内嵌图片** | 图片只能当附件 |
| **只支持 QQ 邮箱** | SMTP/IMAP 地址与授权码方式都是 QQ 特有的 |
| **幂等键是进程内内存** | 进程重启即失效；跨重启需换共享存储 |
| **`/mcp` 无 TLS** | 令牌明文传输；对公网暴露必须加反向代理 |

---

## 文档导航

| 文档 | 内容 |
|---|---|
| **`MANUAL.md`** | 本文件 —— 使用操作 |
| `PROJECT.md` | 项目简介、技术要点 |
| `README.md` | 设计文档 —— 架构、决策取舍、协议细节 |
