# MercariJapanMonitor

一个基于 **Python + mercapi + SQLite + NiceGUI** 的 Mercari Japan 商品监控工具。

支持按关键词、价格范围和排除词进行本地硬过滤，并使用 **DeepSeek** 判定设有语义要求的任务。配置 **SMTP** 后，匹配商品会进入邮件队列。

程序支持后台常驻运行，监控任务由 APScheduler 独立调度，GUI 仅作为管理和查看界面。

> **状态：开发中 / 个人使用项目**

---

## ✨ Features

* 🔎 Mercari Japan 商品搜索
* 🧩 Level 1 本地确定性过滤

  * AND / OR 关键词匹配
  * 最低 / 最高价格
  * 排除词
* 🤖 DeepSeek 语义筛选

  * 例如：`必须是 CD，不要 LP、DVD 或数字版`
  * 严格输出 `true / false`
* 💾 SQLite 持久化

  * Monitor 独立状态
  * 新商品检测
  * 匹配和拒绝记录
  * 忽略商品
* ⏱️ APScheduler 定时扫描

  * 每个 Monitor 独立配置扫描间隔
  * 防止扫描任务重叠
* 📧 SMTP 邮件通知

  * 每轮扫描每个 Monitor 最多发送一封汇总邮件
* 🖥️ NiceGUI Web 管理界面

  * 创建 / 编辑 / 删除 Monitor
  * 启用 / 禁用 Monitor
  * 立即扫描
  * 查看和筛选判定历史
  * 管理忽略商品
  * 配置 DeepSeek / SMTP
* 🌙 Windows 后台常驻

  * 登录自动启动
  * 单实例保护
  * 日志轮转
  * 不自动打开浏览器

---

## 🔄 工作流程

```text
Mercari 在售商品搜索 → Level 1 本地过滤
  ↓
按任务检查忽略状态和既有判定 → SQLite 待处理候选
  ├─ AI 要求为空 → 保存匹配 (matched=1)
  └─ 需要 AI 判定 → 获取或复用商品详情 → DeepSeek
       ├─ true  → 保存匹配 (matched=1)
       ├─ false → 保存拒绝 (matched=0)
       └─ 请求或判定失败 → 保留候选，等待重试

本轮新增匹配 → 已配置 SMTP 时加入邮件队列 → 每任务一封汇总邮件
```

### 两级筛选

项目将确定性条件和自然语言条件分开处理：

**Level 1**

负责：

* 关键词
* 最低价格
* 最高价格
* 排除词

这些条件完全在本地判断，不消耗 AI Token。

**Level 2**

DeepSeek 只负责自然语言语义判断，例如：

> 必须是 CD，最好带 OBI，不要 LP、DVD 或数字版。

通过 Level 1、未被忽略且需要判定的候选才会送给 AI。候选已有详情时，扫描会直接复用。

---

# 🚀 Quick Start

## 1. 创建虚拟环境

```powershell
python -m venv .venv
```

激活：

```powershell
.venv\Scripts\activate
```

如果 PowerShell 阻止脚本执行：

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

安装依赖：

```powershell
pip install -r requirements.txt
```

也可以不激活虚拟环境，直接使用：

```powershell
.venv\Scripts\python.exe
```

---

## 2. 启动程序

```powershell
python main.py
```

程序默认运行在：

```text
http://127.0.0.1:8081
```

程序不会自动打开浏览器。

手动访问：

```text
http://127.0.0.1:8081
```

可以通过 `MJM_PORT` 修改端口：

```powershell
$env:MJM_PORT = "9000"
python main.py
```

---

# ⚙️ 配置

项目采用环境变量保存外部服务配置。

**API Key、SMTP 授权码不会写入 SQLite 或代码。**

---

## DeepSeek

有 AI 语义要求的任务需要配置以下变量。

```powershell
$env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."
```

未设置 API Key 时，有 AI 要求的任务会继续搜索并保存候选，暂停详情获取和 AI 判定。通过设置页补上 Key 后，下一轮扫描可继续处理待判定商品。

当前使用：

```text
deepseek-flash
```

提示词包含任务的语义要求，以及以下商品字段。

* title
* description
* condition
* category

价格、URL、Mercari ID 等信息不会发送给 DeepSeek。

程序要求模型严格返回：

```text
true
```

或：

```text
false
```

任何其他输出都视为无效，并按照 AI 错误处理。

---

## SMTP

以 QQ 邮箱为例：

```powershell
$env:SMTP_HOST = "smtp.qq.com"
$env:SMTP_PORT = "465"
$env:SMTP_USERNAME = "your@qq.com"
$env:SMTP_PASSWORD = "你的SMTP授权码"
$env:SMTP_FROM = "your@qq.com"
$env:SMTP_TO = "receiver@example.com"
```

其中：

> `SMTP_PASSWORD` 必须填写邮箱的 **SMTP 授权码**，不是邮箱登录密码。

SMTP 配置缺失或不完整时，扫描和判定仍会运行。缺少配置期间新增的匹配会写入历史，补全配置后从新匹配开始发送邮件。设置区会显示配置状态。

---

# 🖥️ Web 管理界面

NiceGUI 提供 Monitor 管理界面。每个标签页独立保存表单、筛选、排序和页码，F5 恢复默认视图。任务和商品记录保存在共享数据库中，各页在刷新相应列表时读取已保存的变更。

## Monitor

每个 Monitor 可以独立配置：

* 名称
* 关键词
* AND / OR 匹配模式
* 最低价格
* 最高价格
* 排除词
* DeepSeek 语义要求
* 扫描间隔
* 通知邮箱
* 启用 / 禁用状态

界面要求填写任务名称和关键词。AI 语义要求可留空，通过本地筛选的商品会直接记为匹配，配置了 SMTP 时仍会收到邮件。

保存任务会同时更新 SQLite 和 APScheduler。启用状态的任务会尝试立即扫描一次。若同一任务的配置已在另一页保存，提交旧表单时会提示冲突并保留草稿，可选择载入最新配置。

---

## 关键词

关键词使用中文逗号 `，` 分隔，英文逗号 `,` 也兼容。

例如：

```text
John Coltrane，A Love Supreme，CD
```

会被拆成：

```text
John Coltrane
A Love Supreme
CD
```

### AND

标题必须包含所有关键词。

```text
John Coltrane
A Love Supreme
CD
```

三个条件全部满足才通过。

### OR

标题包含任意一个关键词即可。

---

## 排除词

排除词始终采用：

```text
任意一个命中 → 排除
```

例如：

```text
LP，DVD，Blu-ray
```

只要标题命中其中一个，就不会进入后续处理。

排除词优先于关键词匹配模式。

---

## 立即扫描

点击 **「立即扫描」** 可以手动触发指定 Monitor 的扫描。

扫描在后台执行，不会阻塞整个 GUI。

---

# 🔎 Level 1 Filtering

所有 Level 1 条件必须同时通过：

```text
关键词
  AND
价格范围
  AND
排除词
```

### 关键词匹配

采用大小写不敏感的子串匹配。

例如：

```text
CD
```

可以匹配：

```text
My CD Collection
```

但子串匹配也存在误伤可能，例如：

```text
LP
```

可能命中：

```text
help
```

这是当前已知限制。

---

### 价格

价格范围包含边界：

```text
min_price <= price <= max_price
```

如果没有设置价格限制，则不进行价格过滤。

如果商品没有价格：

```text
price = None
```

则：

* 未设置价格条件 → 通过
* 设置任意价格条件 → 不通过

价格单位为：

```text
JPY
```

---

# 💾 SQLite

正式数据库：

```text
data/mercari_monitor.db
```

首次运行时自动创建。

## 数据模型

| 表 | 列 | 用途和关键约束 |
| --- | --- | --- |
| `monitors` | `id`, `name`, `keywords`, `keyword_mode`, `min_price`, `max_price`, `filter_words`, `ai_requirement`, `interval_minutes`, `notification_email`, `enabled`, `last_scan_at`, `last_result`, `created_at`, `updated_at`, `rule_version` | 任务配置与上次扫描状态。任务名唯一，规则版本从 1 开始。 |
| `monitor_products` | `id`, `monitor_id`, `mercari_id`, `title`, `price`, `published_at`, `url`, `found_at`, `matched`, `judged_rule_version` | 每个任务对商品的最近一次判定。`(monitor_id, mercari_id)` 唯一，旧记录的判定版本可以为空。 |
| `ignored_products` | `id`, `monitor_id`, `mercari_id`, `title`, `price`, `url`, `ignored_at` | 任务的忽略列表与忽略时保存的商品信息。`(monitor_id, mercari_id)` 唯一。 |
| `search_progress` | `monitor_id`, `generation`, `state` | 每个任务一行。保存搜索版本标识、首扫开始时间和分页游标等 JSON 状态。 |
| `scan_candidates` | `id`, `monitor_id`, `mercari_id`, `title`, `price`, `url`, `item_type`, `published_at`, `created_epoch`, `retry_at`, `detail_json`, `failure_count` | 待处理商品、最早可处理时间、已取得的详情和单件失败次数。`(monitor_id, mercari_id)` 唯一。 |
| `scan_rechecks` | `monitor_id`, `mercari_id` | 删除历史或取消忽略后留下的复查标记，由任务和商品 ID 共同标识。 |
| `mail_batches` | `message_id`, `monitor_id`, `label`, `recipient`, `next_attempt_at`, `attempts`, `last_error`, `failed_at` | 以 `message_id` 为主键，保存待发汇总和重试状态。`label` 是排队时的任务名称。 |
| `mail_items` | `message_id`, `mercari_id`, `title`, `price`, `url` | 待发邮件的商品明细。`(message_id, mercari_id)` 唯一，随邮件批次删除。 |

同一商品可以在多个任务中分别得到判定。

## 判定记录与规则版本

DeepSeek 返回 `true` 时保存 `matched=1`，返回 `false` 时保存 `matched=0`。历史默认选择「仅显示匹配商品」，也可选择「仅显示被拒绝商品」或「显示全部（匹配 + 被拒绝）」。匹配才会触发邮件。失败或无效响应会保留待处理候选，已存在的拒绝记录会保留至重判完成。

关键词、匹配模式、价格、排除词或 AI 语义要求发生实际变化时，任务规则版本递增。同一版本已经拒绝的商品会跳过。旧版本或版本未知的拒绝记录，在再次搜到并通过当前本地过滤后重判。已有匹配会继续保留。搜索条件变更时会重置搜索进度及待处理队列。

## 升级与旧 `products` 表

程序启动时自动更新 SQLite 结构。旧版全局 `products` 表会保留原名、结构和记录，可通过 SQLite 查询或导出。旧记录缺少可靠的任务 ID。新版扫描去重、历史和邮件使用按任务保存的现行表，新库只创建上表所列的 8 张业务表。

如果此前启动过会删除旧表的版本，已删除的记录需要从升级前的备份提取。

旧版监控任务重名时，升级会按原任务 ID 排序。第一个任务保留原名，后续任务从 ` (2)` 起编号，跳过已占用的名称。每个任务保留原 ID 和关联记录。旧版升级失败留下的空迁移表会在重试时自动清理。

---

## 删除与忽略

两者语义不同：

### 删除历史记录

删除后：

```text
商品未来仍可能再次被处理
```

### 忽略商品

忽略后：

```text
商品不会再次进入该 Monitor 的处理流程
```

取消忽略后，商品可以在后续扫描中重新处理。

---

### 扫描范围与补扫

任务首次扫描最多读取 20 页，OR 关键词轮流取页并共享预算。之后每轮最多读取 6 页，默认各用 3 页检查最新结果和继续补扫。补扫保存翻页位置并走到末页，之后开始下一遍。首扫读到的商品全部参加本地筛选。之后按首扫开始时间追踪新品，并继续处理符合当前条件的复查商品和旧规则拒绝记录。任务卡片显示上一轮读页数、是否到达末页以及待处理候选数。

首扫分页尚未完成时，后续每轮仍最多请求 6 页。只有一条查询的任务先检查一次首页，再从保存的位置续扫。如果续扫位置就是首页，只请求一次，成功后计入首扫进度。多关键词 OR 任务默认各用 3 次轮流检查各关键词的首页和从保存的位置续扫，空余名额可互相补用。某一续扫页持续失败时，后续各轮仍会检查首页以发现新品。首扫累计成功读取 20 页或所有查询均到达末页后，转入常规追新与补扫。

需要 AI 判断的候选先存入 SQLite，每轮最多尝试 50 件。待处理商品可能晚几轮发出提醒，搜索失败或重启后仍可继续处理。上游翻页结果可能变化，已下架或从未出现在搜索结果中的商品可能无法发现。

符合处理条件的候选超过本轮额度时，最多一半名额优先给发布时间最新且未尝试的商品，其余按最早可处理时间排序。单件详情获取或 AI 判定失败后，依次至少等待 5 分钟、15 分钟、1 小时、6 小时再重试，后续每次至少等 6 小时。共享 AI 冷却期间暂停判定，单件失败次数保持原值。

再次搜到待判定商品时，扫描会用最新返回的价格和标题重新执行本地过滤。仍符合条件的候选保留排队位置和重试时间，历史与邮件使用更新后的价格和标题。超出条件的候选会移出队列，之后再次搜到且符合条件时可以重新入队。

本轮再次搜到的在售候选使用搜索返回的标题和价格，标题未变时可复用已保存的语义详情。本轮未搜到的候选在处理前按商品 ID 查询最新标题、价格和在售状态，并重新执行本地过滤。这次详情请求也会刷新语义详情缓存。确认售罄、下架或超出筛选条件时，候选会移出队列，之后再次搜到且符合条件时可以重新入队。详情查询失败、在售状态不明确，或设置了价格限制却无法核实价格时，候选会按退避间隔重试。

DeepSeek 出现鉴权、余额、限流、服务端或网络故障时，所有任务共享至少 5 分钟的 AI 冷却期。服务给出更长的 `Retry-After` 时按其等待，冷却到期后先用一件候选试判。

启动、定时和手动扫描共用请求额度。任意 60 秒内，Mercari 默认最多 60 次（含搜索、详情、重试及 Shops 回退），DeepSeek 默认最多 30 次。达到额度后，请求按顺序在本轮等待。可在启动前通过环境变量 `MJM_MERCARI_REQUESTS_PER_MINUTE` 和 `MJM_DEEPSEEK_REQUESTS_PER_MINUTE` 设置正整数额度。

# 📧 邮件通知

每个 Monitor 每轮扫描：

> **最多发送一封汇总邮件。**

如果本轮有多个新增匹配：

```text
商品 A
商品 B
商品 C
```

会合并成：

```text
一封邮件
```

而不是发送三封邮件。

邮件主题格式：

```text
Mercari 新商品提醒｜{任务名称}｜{N} 件
```

每次批量发送只建立一次 SMTP 连接：

```text
connect
  ↓
login
  ↓
send
  ↓
quit
```

这样可以减少 SMTP 服务对短时间重复连接的限制。

扫描开始时若已配置 SMTP，商品匹配记录和待发邮件明细会在同一个 SQLite 事务中保存。独立的邮件任务在扫描结束后发送汇总，失败后依次等待 1、5、15、60 分钟再试，之后每小时重试。重启会恢复待发邮件，包括上次扫描中断前已经保存的匹配。任务卡片显示待发数量、最近失败原因和下次重试时间。

停用监控后，已排队邮件仍会发送。删除任务会取消尚未开始的投递。升级前的历史和 SMTP 未配置期间的匹配不会补发。如果 SMTP 已接收邮件而程序在记录成功前退出，重启后可能收到重复的汇总，重试会复用原 `Message-ID`。

---

# ⏱️ Scheduler

项目使用 APScheduler 管理 Monitor。

每个 Monitor 拥有独立 Job。

默认测试配置为：

```text
30 分钟
```

Scheduler 的核心行为：

* 启动后立即扫描启用的 Monitor
* 按 Monitor 自己的间隔继续扫描
* 同一个 Monitor 不允许扫描重叠
* 上一轮尚未结束时，跳过同一任务的本次触发
* 单次扫描失败不会导致 Scheduler 退出
* Ctrl+C 时等待正在进行的扫描结束后再退出

---

# 🌐 网络与 SSL

项目使用 `mercapi` 访问 Mercari。

**HTTPS 证书验证始终保持开启。**

项目不会：

* `verify=False`
* 关闭全局 SSL 验证
* 降低 OpenSSL Security Level
* 使用环境变量绕过 TLS 验证

如果出现类似：

```text
CERTIFICATE_VERIFY_FAILED
EE certificate key too weak
```

应首先检查本机网络环境、HTTPS 中间人代理、杀毒软件的加密连接扫描等，而不是关闭 SSL 验证。

Mercari 搜索遇到瞬时网络错误时最多尝试三次。

```text
第 1 次
  ↓ 失败
等待 2s
  ↓
第 2 次
  ↓ 失败
等待 4s
  ↓
第 3 次
  ↓
失败
```

SSL 证书错误不会进入重试。

---

# 🪟 Windows 后台运行

程序支持 Windows 登录自动启动。

后台运行时：

```text
Scheduler
    │
    ├── Monitor A
    ├── Monitor B
    ├── Monitor C
    └── ...
```

与浏览器无关。

关闭浏览器不会停止扫描。

GUI 只是管理界面。

---

## 单实例保护（Windows / Ubuntu）

程序启动时会在系统临时目录（由 `tempfile.gettempdir()` 确定）获取一个真实的 OS 文件锁，通常位于：

```text
Windows：%TEMP%\marketplace_monitor.lock
Ubuntu：/tmp/marketplace_monitor.lock
```

Windows 使用 `msvcrt.locking` 独占字节锁，Ubuntu 等 POSIX 系统使用 `fcntl.flock` 非阻塞独占锁，无需额外依赖，非 PID 文件猜测。

如果已有实例正在运行：

```text
第二个实例
    ↓
检测到锁
    ↓
打印警告
    ↓
退出码 1
```

不会继续创建：

* 数据库连接
* Scheduler
* HTTP 服务

因此不会产生重复调度或重复邮件。

进程退出（包括崩溃）后，操作系统会自动释放文件锁，残留的锁文件不会阻塞下次启动。

---

## 日志

程序同时输出：

```text
stdout
```

以及：

```text
data/logs/monitor.log
```

日志使用：

```text
RotatingFileHandler
```

配置为：

```text
1 MB × 3 backups
```

后台运行时可以直接查看日志文件。

---

## Windows 计划任务

注册：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\manage_task.ps1
```

删除：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\manage_task.ps1 -Uninstall
```

计划任务：

```text
名称：MarketplaceMonitor
触发：当前用户登录
Program：.venv\Scripts\python.exe
Arguments：main.py
Start in：项目根目录
```

程序异常退出后会自动尝试重启。

---

### ⚠️ 环境变量

Windows 计划任务无法读取某个 PowerShell 会话中临时设置的：

```powershell
$env:XXX = "..."
```

使用计划任务时，请将所需的服务配置和自定义端口、请求额度设置为**用户级环境变量**。

```text
DEEPSEEK_API_KEY_FOR_MJM
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
SMTP_FROM
SMTP_TO
MJM_PORT
MJM_MERCARI_REQUESTS_PER_MINUTE
MJM_DEEPSEEK_REQUESTS_PER_MINUTE
```

可以使用：

```powershell
setx VARIABLE_NAME "value"
```

或者通过：

```text
系统属性 → 环境变量
```

进行设置。

> 不要把 API Key、SMTP 授权码提交到 Git。

---

# 🧪 Testing

## 单元测试

```powershell
python -m pytest -q
```

测试完全离线。

数据库测试使用 pytest 临时目录中的 SQLite 文件或内存数据库。

当前测试覆盖：

```text
Filter
Database
Scanner
Scheduler
DeepSeek
SMTP
GUI validation / 多标签页隔离
搜索覆盖与请求预算
规则版本与邮件队列
```

---

## 集成测试

### Phase 1：搜索 + Level 1

```powershell
python scripts\test_phase1.py
```

需要真实访问 Mercari。

---

### Phase 3：单次 Monitor 扫描

```powershell
python scripts\test_phase3.py --once
```

执行一次扫描后退出。脚本使用独立数据库 `data/phase3_test.db`，后续运行会复用其扫描进度和判定记录。

---

### Phase 3：长期运行

```powershell
python scripts\test_phase3.py
```

使用同一个 `data/phase3_test.db`，启动后立即扫描，然后按照配置间隔继续运行。

使用：

```text
Ctrl+C
```

停止。

---

### Phase 4：DeepSeek

```powershell
$env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."

python scripts\test_phase4.py --limit 3
```

脚本使用独立数据库 `data/phase4_test.db`。`--limit` 限制本轮尝试判定的候选数，实际 AI 请求还取决于候选的详情是否可用。

---

### SMTP

```powershell
python scripts\test_email.py
```

发送一封包含 3 个示例商品的汇总邮件，用于验证 SMTP 配置。

---

# 📁 Project Structure

```text
MercariJapanMonitor/
│
├── main.py              启动装配与服务生命周期
│
├── app/
│   ├── ui/              NiceGUI 任务、设置、历史与忽略列表
│   ├── mercari.py       Mercari 搜索与详情请求
│   ├── filter.py        Level 1 本地过滤
│   ├── database.py      SQLite 数据访问与事务
│   ├── migrations.py    SQLite 建表与旧库升级
│   ├── coverage.py      翻页、补扫与候选发现
│   ├── scanner.py       单个任务的扫描流程
│   ├── scheduler.py     APScheduler 调度
│   ├── deepseek.py      DeepSeek 判定与故障冷却
│   ├── request_budget.py  进程共享请求额度
│   ├── outbox.py        邮件持久队列与重试
│   ├── email.py         SMTP 邮件发送
│   ├── exchange_rate.py  界面参考汇率
│   └── single_instance.py  单实例锁
│
├── tests/
│   ├── test_filter.py
│   ├── test_monitors.py
│   ├── test_monitor_products.py
│   ├── test_monitor_scanner.py
│   ├── test_monitor_scheduler.py
│   ├── test_deepseek.py
│   ├── test_email.py
│   └── test_gui_validation.py
│
├── scripts/
│   ├── test_phase1.py
│   ├── test_phase3.py
│   ├── test_phase4.py
│   ├── test_email.py
│   └── manage_task.ps1
│
├── data/
│   ├── mercari_monitor.db
│   └── logs/
│       └── monitor.log
│
├── requirements.txt
├── pytest.ini
└── README.md
```

---

# 🧭 Architecture

```text
main.py 装配共享服务
├─ NiceGUI 任务管理与历史查询 → SQLite
└─ APScheduler → Scanner → Mercari 搜索、筛选与 DeepSeek 判定
                     ↓
                   SQLite → 邮件工作任务 → SMTP
```

核心原则：

### 本地能确定的事情，不交给 AI

关键词、价格和排除词使用本地规则完成。

### AI 只处理真正需要语义理解的商品

需要语义判定的商品从 SQLite 候选队列读取。详情缓存随候选保存，判定完成后清理。

### 外部服务失败不应破坏本地状态

例如：

* Mercari 网络错误
* DeepSeek API 错误
* SMTP 发送失败

都不应该导致整个 Scheduler 停止。

---

# 📌 Current Limitations

以下功能目前尚未实现：

* Discogs 集成
* 价格变化检测
* 历史价格记录
* 邮件模板系统
* Telegram / Webhook 等其他通知渠道
* 多用户 / 登录 / 权限系统
* Marketplace 抽象层
* LLM 抽象层
* 多平台支持
* ORM
* 独立数据库迁移框架
* Docker
* Redis
* Celery

这些并不是当前运行所必需的依赖，因此目前没有为了“扩展性”提前引入。

---

# 🔐 Security Notes

请不要将以下内容提交到 Git：

```text
DEEPSEEK_API_KEY_FOR_MJM
SMTP_PASSWORD
```

推荐使用环境变量：

```powershell
$env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."
$env:SMTP_PASSWORD = "..."
```

数据库也不会保存 API Key 或 SMTP 授权码。

---

# 📄 License

个人项目，License 暂未确定。
