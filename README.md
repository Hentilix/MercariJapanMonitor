# MercariJapanMonitor

个人使用的 Mercari Japan 商品追踪器（开发中）。

## 当前进度

- **Phase 1**：mercapi 搜索 + Level 1 硬过滤
- **Phase 2**：SQLite 持久化 + 新商品检测
- **Phase 3**：APScheduler 每 30 分钟自动扫描
- **Phase 4**：`full_item()` 详情获取 + DeepSeek true/false 语义筛选
- **Phase 5**：SMTP 邮件通知（Phase 5.1 起：每任务每轮扫描一封汇总邮件）
- **Phase 6**：NiceGUI 管理界面（任务 CRUD / 立即扫描 / 设置 / 最近匹配）

当前数据流：

```text
Mercari Japan → mercapi → 搜索 → Level 1 硬过滤 → SQLite 新商品检测
                                                        ↓
                                              只处理新商品: full_item()
                                                        ↓
                                              提取 title/description/
                                              condition/category
                                                        ↓
                                              DeepSeek → true / false
                                                        ↓
                                                  true → SMTP 邮件
```

管理界面（NiceGUI）通过浏览器管理上面的监控任务、查看扫描结果与最近匹配。

## 启动方式

```powershell
# 先设置环境变量（可选：不设置则对应功能降级）
$env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."
$env:SMTP_HOST = "smtp.qq.com"
$env:SMTP_PORT = "465"
$env:SMTP_USERNAME = "hentilix@qq.com"
$env:SMTP_PASSWORD = "你的QQ邮箱SMTP授权码"
$env:SMTP_FROM = "hentilix@qq.com"
$env:SMTP_TO = "chccrimson@gmail.com"

python main.py
# 浏览器打开 http://127.0.0.1:8081
# 端口可用环境变量覆盖：$env:MJM_PORT = "9000"（默认 8081；8080 常被 Steam 等占用）
```

GUI 功能：

- 监控任务列表：名称 / 关键词 / 匹配模式 / 状态 / 间隔 / 上次扫描与结果
- 创建 / 编辑 / 删除 / 启用禁用监控任务（保存即写入 SQLite 并同步 APScheduler）
- 任务支持 **AND / OR 关键词匹配模式**；关键词与排除词均用**中文逗号 `，`** 分割
- 「立即扫描」按钮：复用 `MonitorScheduler.run_monitor_now()`，扫描中不阻塞页面
- 设置区：DeepSeek Key 与 SMTP 配置写入当前进程环境变量（password 输入框，不落盘、不回显已有值）
- 匹配商品历史：按 Monitor 查看（标题 / 价格 / 发布时间 / Mercari 链接），支持四种排序、分页、删除与忽略
- 忽略商品列表：按 Monitor 查看（标题 / 价格 / 忽略时间 / 链接），支持分页与取消忽略

## 项目结构

```text
main.py              NiceGUI 管理界面入口（python main.py）
app/
├── mercari.py       通过 mercapi 0.5.0 搜索商品、获取 full_item() 详情并提取字段
├── filter.py        Level 1 确定性规则过滤（keyword / min_price / max_price / exclude）
├── database.py      SQLite：monitors CRUD + monitor_products 历史 + ignored_products 忽略列表
├── scanner.py       scan_monitor()：以 Monitor 为单位的完整扫描（搜索→过滤→忽略/去重→详情→AI→批量邮件）
├── scheduler.py     APScheduler：MonitorScheduler（每任务一个 job，独立间隔，防重叠）
├── deepseek.py      DeepSeek 调用：必要字段进，严格 true/false 出
└── email.py         SMTP 邮件通知（标准库 smtplib，线程中发送，每任务每轮一封汇总邮件）
tests/
├── test_filter.py            Level 1 离线单元测试
├── test_monitors.py          monitors 表 CRUD / 迁移幂等 / 旧表淘汰测试
├── test_monitor_products.py  monitor_products / ignored_products 全量测试
├── test_monitor_scanner.py   scan_monitor() 全场景离线测试
├── test_monitor_scheduler.py MonitorScheduler 调度测试（间隔/防重叠/并行/启停/幂等）
├── test_deepseek.py          DeepSeek 解析与调用测试（假 HTTP，不花真钱）
├── test_email.py             SMTP 批量邮件测试（mock smtplib，不真发邮件）
└── test_gui_validation.py    GUI 表单校验 / 分页纯函数测试
scripts/
├── test_phase1.py     Phase 1 集成测试（真实搜索 + 过滤）
├── test_phase3.py     Monitor 模型长期运行监控入口（--once 单次扫描模式，临时库）
├── test_phase4.py     Monitor 模型 DeepSeek 集成测试（临时库 + --limit 控制 AI 成本）
└── test_email.py      发送一封含 3 个示例商品的汇总测试邮件
data/
├── mercari_monitor.db    正式 SQLite 数据库（首次运行时自动创建）
└── phase4_test.db        Phase 4 测试库（可随时删除；均被 .gitignore 忽略）
requirements.txt     依赖清单
pytest.ini           限定 pytest 只收集 tests/ 目录
```

## 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\activate        # 若被策略阻止：Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
pip install -r requirements.txt
```

也可以不激活虚拟环境，直接用 `.venv\Scripts\python.exe` 运行。

## 运行单元测试（离线）

```powershell
python -m pytest -q
```

不访问网络。数据库测试全部使用 pytest `tmp_path` 临时库，不会修改真实的 `data/mercari_monitor.db`。

## 运行集成测试（真实网络）

```powershell
# Phase 1：搜索 + Level 1 过滤
python scripts\test_phase1.py

# Monitor 模型：单次扫描（立即执行一遍完整流程并退出，临时库）
python scripts\test_phase3.py --once

# Monitor 模型：长期运行监控（启动即扫描每个启用任务一次，之后按各自间隔；Ctrl+C 停止）
python scripts\test_phase3.py

# Monitor 模型 + DeepSeek：独立验证入口（使用独立临时库 data/phase4_test.db，不碰正式库）
# 需要先设置 DeepSeek Key：
#   PowerShell:  $env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."
python scripts\test_phase4.py --limit 3
```

## Phase 4 语义筛选

- **两级筛选职责分离**：价格/关键词/排除词由 Level 1 本地确定性过滤；自然语言要求（如"必须是 CD，不要 LP、DVD 或数字版"）只交给 DeepSeek
- **成本控制顺序**：Level 1 → SQLite 新商品检测 → **只对真正的新商品**调用 `full_item()` 和 DeepSeek；已见过的商品零成本
- **发送给 DeepSeek 的字段只有 4 个**：标题、描述、成色、分类（价格已在 Level 1 判断过，URL/ID 不发送）
- **输出严格限制**：Prompt 要求只输出 `true`/`false`，程序侧严格解析（只认小写 `true`/`false`，其余一律视为无效）；关闭 thinking 模式、`max_tokens=8`、`temperature=0`
- **安全默认**：API 错误、网络错误、非法输出一律不算匹配（记为 `ai_errors`），绝不误判为 true
- **模型**：`deepseek-flash`（按当前官方文档；旧的 `deepseek-chat` 已淘汰）；API Key 通过环境变量 `DEEPSEEK_API_KEY_FOR_MJM` 提供，不写死在代码中
- **SQLite schema 未改变**：AI 结果本阶段不持久化；DeepSeek 判 false 的商品当前不进入最终匹配结果（是否重新判断留待后续阶段）

## Phase 3 监控程序行为

- 启动后**立即**执行一次扫描（不必等第一个 30 分钟）
- 之后每 30 分钟自动扫描一次（`scripts/test_phase3.py` 中的 `INTERVAL_MINUTES`）
- **扫描不重叠**：上一轮未结束时新到期的轮次会被合并（`max_instances=1` + `coalesce=True`），不会同时发起两个扫描
- **单次失败不退出**：某次扫描异常只记录日志，定时任务继续，下一轮照常执行
- **Ctrl+C 优雅退出**：停止调度新任务 → 等待正在进行的扫描自然结束 → 关闭数据库和 HTTP 连接 → 退出
- 测试参数（关键词 `Built to Spill`、价格 1000–5000、排除 LP/DVD/Blu-ray）为临时硬编码，多任务/配置系统由后续 NiceGUI 阶段解决

## 数据库

- 位置：`data/mercari_monitor.db`（自动创建目录和文件；已存在时不删除数据，结构通过幂等迁移自动升级）
- **Phase 5.2 核心三表（Monitor 独立的处理状态）**：

```sql
-- monitors：每个监控任务一行（name 唯一）
CREATE TABLE monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    keywords TEXT NOT NULL,          -- 原始关键词串（, 或 ，分隔）
    keyword_mode TEXT NOT NULL,      -- 'AND' | 'OR'
    min_price INTEGER,               -- NULL = 不限制（JPY）
    max_price INTEGER,
    filter_words TEXT,               -- 排除词串，任一命中即拒绝
    ai_requirement TEXT NOT NULL,    -- 交给 DeepSeek 的语义要求
    interval_minutes INTEGER NOT NULL,
    notification_email TEXT,         -- NULL/空 = 用 SMTP_TO
    enabled INTEGER NOT NULL DEFAULT 1,
    last_scan_at TEXT, last_result TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- monitor_products：某 Monitor 已成功处理（Level 1 + DeepSeek true）的商品
CREATE TABLE monitor_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    mercari_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price INTEGER,
    published_at TEXT,               -- Mercari 商品发布时间（不可得时为 NULL）
    url TEXT NOT NULL,
    found_at TEXT NOT NULL,          -- 本程序发现时间
    UNIQUE (monitor_id, mercari_id)
);

-- ignored_products：用户要求某 Monitor 永久忽略的商品
CREATE TABLE ignored_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    mercari_id TEXT NOT NULL,
    ignored_at TEXT NOT NULL,
    UNIQUE (monitor_id, mercari_id)
);
```

- **Monitor 独立**：同一个 Mercari 商品可以被多个 Monitor 分别处理/匹配/忽略；删除 Monitor 级联删除其商品历史与忽略列表；禁用只暂停扫描，数据全保留
- **删除 ≠ 忽略**：删除历史记录后商品可被再次处理；忽略会把商品从历史移除并加入忽略表（两者不同时存在）；取消忽略只删忽略记录，商品在下次扫描时重新处理
- 旧 Phase 2 的全局 `products` 表已在 Phase 5.4-D 淘汰（启动时自动 DROP，数据不迁移——旧数据无可靠的 monitor_id）
- API Key / SMTP 授权码**不写入数据库**，仅存于环境变量

## Level 1 规则语义

- 所有条件为 **AND** 关系，全部通过才算候选
- **关键词匹配模式（`match_mode`）**：
  - `AND`（默认）：标题必须包含**每一个**关键词
  - `OR`：标题包含**任意一个**关键词即可
- **关键词用中文逗号 `，` 分割**（英文逗号也兼容）：例如搜索关键词填 `John Coltrane，A Love Supreme，CD` 会拆成 3 个关键词参与 Level 1 匹配；发送给 Mercari 的搜索串为关键词的**空格拼接**（`John Coltrane A Love Supreme CD`，P0-1 修复——原始串中的逗号会严重压低召回）
- `exclude_keywords` 优先于 `keywords`，且始终为"任一命中即排除"（与 `match_mode` 无关），同样用中文逗号分割
- 匹配为大小写不敏感（casefold）的**子串匹配**
- 价格边界含端点（`min <= price <= max`）
- 商品无价格（`price=None`）：未设置价格条件时通过；设置了任一价格条件时不通过
- 已知限制：子串匹配可能误伤（例如 `LP` 会命中标题里的 `help`）；后续如有需要可改为词边界匹配

## SMTP 配置（Phase 5 邮件通知）

发件使用 QQ 邮箱（`smtp.qq.com`，465 端口隐式 SSL），密码必须使用 **SMTP 授权码**（在 QQ 邮箱 设置→账户→开启 SMTP 服务 中生成），**不是 QQ 登录密码**。所有配置走环境变量，绝不写入代码：

| 变量 | 示例值 |
|---|---|
| `SMTP_HOST` | `smtp.qq.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USERNAME` | `hentilix@qq.com` |
| `SMTP_PASSWORD` | QQ 邮箱 SMTP 授权码（不要写进任何文件） |
| `SMTP_FROM` | `hentilix@qq.com` |
| `SMTP_TO` | `chccrimson@gmail.com`（默认收件人；每个监控任务可单独覆盖） |

PowerShell 一次性设置：

```powershell
$env:SMTP_HOST = "smtp.qq.com"
$env:SMTP_PORT = "465"
$env:SMTP_USERNAME = "hentilix@qq.com"
$env:SMTP_PASSWORD = "你的QQ邮箱SMTP授权码"
$env:SMTP_FROM = "hentilix@qq.com"
$env:SMTP_TO = "chccrimson@gmail.com"
```

单独测试 SMTP（发送**一封含 3 个示例商品的汇总邮件**到 Gmail，单次 SMTP 连接）：

```powershell
python scripts\test_email.py
```

**通知模型（Phase 5.1）**：每个监控任务每轮扫描**最多发送一封汇总邮件**——本轮所有 AI 命中商品合并在同一封邮件里（主题：`Mercari 新商品提醒｜{关键词}｜{N} 件`），每次 `send_batch()` 只建立一次 SMTP 连接（connect→login→send→quit）。这样避免 QQ SMTP 对短时连续建立连接的限制。

未配置 SMTP 时监控程序正常降级：不发送邮件，仅输出一条警告日志。AI 命中的商品不会重复发信——同一 `mercari_id` 第二次扫描时已属于"已见过"，不会再次触发 AI 和邮件；汇总邮件发送失败也只记日志，不影响扫描结果和"已处理"状态。

## Windows 后台自启（Phase 5.6）

**运行模型**：调度器在服务器启动钩子（`on_startup`）里启动，与浏览器完全无关——关闭浏览器不影响扫描；`ui.run(show=False)` 保证**绝不自动打开浏览器**。程序可常驻后台持续执行所有 Monitor 的定时扫描，GUI 仅在需要时用浏览器手动访问 `http://127.0.0.1:8081`（可用 `MJM_PORT` 环境变量改端口，默认 8081）。

**单实例保护**：启动时在用户临时目录获取一个真实的 OS 文件锁（`%TEMP%\marketplace_monitor.lock`，`msvcrt.locking` 独占字节锁，非 PID 文件猜测）。第二个实例会打印警告并以退出码 1 直接退出，**不会**创建数据库连接/调度器/HTTP 服务，因此不可能出现重复调度、端口冲突或重复邮件。进程退出（包括崩溃）后 Windows 自动释放锁，残留的锁文件不会阻塞下次启动。

**日志文件**：除 stdout 外同时写入 `data\logs\monitor.log`（`RotatingFileHandler`，1 MB × 3 个备份），后台运行时从这里查看扫描/错误记录。

**计划任务（登录时自动启动）**：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\manage_task.ps1            # 注册任务 MarketplaceMonitor
powershell -ExecutionPolicy Bypass -File scripts\manage_task.ps1 -Uninstall # 删除任务
```

注册后的任务配置：任务名 `MarketplaceMonitor`；触发器＝**当前用户登录时**；Program＝`<项目>\.venv\Scripts\python.exe`；Arguments＝`main.py`；Start in＝项目根目录；意外退出后 1 分钟自动重启（最多 3 次）；运行时限 365 天。任务进程在后台运行，无控制台窗口。

**环境变量要求（重要）**：登录时启动的任务读不到仅在某个 PowerShell 会话里临时 `$env:` 设置的变量。`SMTP_PASSWORD`、`DEEPSEEK_API_KEY_FOR_MJM`、`MJM_PORT`（如需改端口）必须设为**用户级**环境变量（`setx 变量名 值` 或 系统属性 → 环境变量），才能被计划任务读取。`scripts\manage_task.ps1` 注册时会检查并提示缺失项；脚本本身绝不读取或写入任何密钥。

注意：若旧的命令行实例仍占用 8081，请先按 Ctrl+C 正常退出（或结束该进程），否则计划任务的新实例会因端口占用退出——旧实例来自 Phase 5.6 之前的代码，不持有单实例锁。

## 尚未实现（Phase 边界）

- Discogs
- AI 判 false 商品的重新判断策略
- 价格变化检测 / 历史价格记录
- 邮件模板系统 / 多通道通知（Telegram、Webhook 等）
- 多用户 / 登录 / 权限系统
- Marketplace 抽象层 / LLM 抽象层 / 多平台支持
- ORM / 数据库迁移框架
- Docker / Redis / Celery
