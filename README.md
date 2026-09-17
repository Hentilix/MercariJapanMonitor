# MercariJapanMonitor

一个基于 **Python + mercapi + SQLite + NiceGUI** 的 Mercari Japan 商品监控工具。

支持按关键词、价格范围和排除词进行本地硬过滤，并可选使用 **DeepSeek** 对商品详情进行语义判断；匹配成功后通过 **SMTP 邮件**发送通知。

程序支持后台常驻运行，监控任务由 APScheduler 独立调度，GUI 仅作为管理和查看界面。

> **状态：开发中 / 个人使用项目**

---

## ✨ Features

* 🔎 Mercari Japan 商品搜索
* 🧩 Level 1 本地确定性过滤

  * AND / OR 关键词匹配
  * 最低 / 最高价格
  * 排除词
* 🤖 可选 DeepSeek 语义筛选

  * 例如：`必须是 CD，不要 LP、DVD 或数字版`
  * 严格输出 `true / false`
* 💾 SQLite 持久化

  * Monitor 独立状态
  * 新商品检测
  * 已匹配商品历史
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
  * 查看匹配历史
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
                         Mercari Japan
                              │
                              ▼
                           mercapi
                              │
                              ▼
                           商品搜索
                              │
                              ▼
                    Level 1 硬过滤
               ┌──────────────┼──────────────┐
               │              │              │
             关键词          价格           排除词
               └──────────────┼──────────────┘
                              │
                              ▼
                       SQLite 新商品检测
                              │
                     只处理真正的新商品
                              │
                              ▼
                       full_item() 详情
                              │
                              ▼
                    title / description
                    condition / category
                              │
                              ▼
                    DeepSeek（可选）
                              │
                         true / false
                              │
                         ┌────┴────┐
                         │         │
                       true      false
                         │
                         ▼
                    SMTP 汇总邮件
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

为了降低成本，只有：

```text
Level 1 通过
      ↓
SQLite 判断为新商品
      ↓
full_item()
      ↓
DeepSeek
```

之后才会调用 AI。

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

如果需要启用 AI 语义筛选：

```powershell
$env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."
```

未设置 API Key 时，项目会自动降级，不调用 DeepSeek。

当前使用：

```text
deepseek-flash
```

DeepSeek 接收到的字段只有：

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

如果没有配置 SMTP：

* 扫描仍然正常运行
* AI 筛选仍然可以执行
* 不发送邮件
* 程序仅记录警告

---

# 🖥️ Web 管理界面

NiceGUI 提供 Monitor 管理界面。

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

保存 Monitor 后会：

```text
SQLite
  +
APScheduler
```

同步更新。

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

数据库不会因为程序重新启动而删除已有数据。

---

## 数据模型

### `monitors`

每个监控任务一行。

主要字段：

```text
id
name
keywords
keyword_mode
min_price
max_price
filter_words
ai_requirement
interval_minutes
notification_email
enabled
last_scan_at
last_result
created_at
updated_at
```

---

### `monitor_products`

保存某个 Monitor 已经成功处理并匹配的商品。

```text
monitor_id
mercari_id
title
price
published_at
url
found_at
```

唯一约束：

```text
(monitor_id, mercari_id)
```

因此同一个商品可以分别属于多个 Monitor。

---

### `ignored_products`

保存用户主动忽略的商品：

```text
monitor_id
mercari_id
ignored_at
```

同样以：

```text
(monitor_id, mercari_id)
```

作为唯一约束。

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

# 📧 邮件通知

每个 Monitor 每轮扫描：

> **最多发送一封汇总邮件。**

如果本轮发现多个 AI 命中的商品：

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
Mercari 新商品提醒｜{关键词}｜{N} 件
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
* 到期但上一轮尚未结束时，会合并下一次执行
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

项目对瞬时网络错误提供有限重试：

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

## 单实例保护

程序启动时会创建：

```text
%TEMP%\marketplace_monitor.lock
```

作为 OS 文件锁。

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

进程退出后 Windows 会自动释放文件锁。

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

因此，如果使用计划任务运行，需要将这些变量设置为**用户级环境变量**：

```text
SMTP_PASSWORD
DEEPSEEK_API_KEY_FOR_MJM
MJM_PORT
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

数据库测试使用 pytest `tmp_path` 创建临时数据库，不会修改：

```text
data/mercari_monitor.db
```

当前测试覆盖：

```text
Filter
Database
Scanner
Scheduler
DeepSeek
SMTP
GUI validation
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

执行一次完整扫描后退出，并使用临时数据库。

---

### Phase 3：长期运行

```powershell
python scripts\test_phase3.py
```

启动后立即扫描，然后按照配置间隔继续运行。

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

`--limit` 用于限制 AI 调用数量，避免测试过程中产生不必要的 API 成本。

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
├── main.py
│
├── app/
│   ├── mercari.py       Mercari 搜索 / full_item() 详情
│   ├── filter.py        Level 1 硬过滤
│   ├── database.py      SQLite 数据访问
│   ├── scanner.py       Monitor 完整扫描流程
│   ├── scheduler.py     APScheduler 调度
│   ├── deepseek.py      DeepSeek 语义筛选
│   └── email.py         SMTP 邮件通知
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

项目目前保持比较简单的分层：

```text
NiceGUI
   │
   ▼
Monitor / GUI Layer
   │
   ▼
Scheduler
   │
   ▼
Scanner
   │
   ├──────────────► Filter
   │
   ├──────────────► SQLite
   │
   ├──────────────► mercapi
   │
   ├──────────────► DeepSeek
   │
   └──────────────► SMTP
```

核心原则：

### 本地能确定的事情，不交给 AI

关键词、价格和排除词使用本地规则完成。

### AI 只处理真正需要语义理解的商品

只有通过 Level 1 且尚未处理过的商品才进入：

```text
full_item()
    ↓
DeepSeek
```

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
* AI 判定 `false` 商品的重新判断策略
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
