# AI Agent Platform — 项目永久记忆

> 每次新开 Claude Code 会话自动加载此文件。修改代码前必须先读本文档。

---

## 1. 项目简介与商业背景

这是一个面向 C 端用户的 **AI Agent SaaS 平台**，提供多模型对话、网页搜索、网页抓取等 Agent 能力。

商业模式：
- 免费用户每日限额使用，引导付费转化
- 付费用户按月/季/年订阅，享受更高配额和更强模型
- 支付宝扫码支付，自动开通会员权限
- 运营团队通过管理后台手动管理用户和会员

---

## 2. 完整技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | Python 3.9 + FastAPI 0.115 |
| AI SDK | Anthropic SDK（claude-sonnet-4-6 / opus-4-6） |
| 搜索工具 | Tavily API |
| 网页抓取 | Playwright（Chromium） |
| 数据库 | PostgreSQL（SQLAlchemy 2.0 ORM） |
| 数据库迁移 | Alembic（已安装，尚未初始化迁移文件） |
| 身份认证 | JWT（python-jose）+ 手机号验证码登录 |
| 密码工具 | passlib[bcrypt]（备用） |
| 支付 | 支付宝 alipay-sdk-python（新版 DefaultAlipayClient） |
| 前端（用户） | 纯 HTML + JS（`frontend/index.html`） |
| 前端（管理） | 纯 HTML + JS（`admin/index.html`，单文件无依赖） |
| 运行时 | Uvicorn（reload 模式） |
| 本机 Python | `/Library/Developer/CommandLineTools/usr/bin/python3`，pip 用 `pip3` |

---

## 3. 项目目录结构

```
ai-agent-platform/
├── main.py                  # FastAPI 入口，注册所有路由，lifespan 自动建表
├── database.py              # SQLAlchemy engine + SessionLocal + Base + get_db
├── models.py                # 5 张数据库表的 ORM 定义
├── auth.py                  # JWT 生成/验证 + get_current_user 依赖
├── permissions.py           # 套餐权限规则 + 配额检查 + 用量记录
├── requirements.txt         # Python 依赖
├── .env                     # 环境变量（不提交 git）
│
├── agent/
│   ├── core.py              # Agent 主循环（流式，async generator）
│   └── __init__.py
│
├── tools/
│   ├── search.py            # Tavily 搜索工具
│   ├── scraper.py           # Playwright 网页抓取工具
│   └── __init__.py
│
├── routers/
│   ├── auth.py              # /auth/* 登录注册接口
│   ├── member.py            # /member/* 会员信息接口
│   ├── payment.py           # /pay/* 支付宝支付接口
│   ├── admin.py             # /admin/* 管理员接口
│   └── __init__.py
│
├── frontend/
│   ├── login.html           # 登录页（左右分栏，验证码登录，含鉴权跳转）
│   └── index.html           # 用户端主界面（深色 Notion 风格，需登录）
│
├── admin/
│   └── index.html           # 管理后台前端（深蓝侧边栏，单文件）
│
└── wechat-monitor/          # 独立子项目（Next.js），微信公众号监控，与主后端无关
```

---

## 4. 已完成功能模块

### 4.1 数据库表（`models.py`）

| 表名 | 字段 | 说明 |
|------|------|------|
| `users` | id, phone, nickname, avatar, created_at, is_active | 用户主表 |
| `memberships` | id, user_id, plan_type, start_at, expire_at, status | 会员记录，status: active/expired/cancelled |
| `credits` | id, user_id, balance, total_bought, total_used | 积分余额（预留，暂未使用） |
| `orders` | id, user_id, plan, amount, pay_status, trade_no, created_at, paid_at | 支付订单 |
| `usage_logs` | id, user_id, model, message_count, created_at | 每次 /chat 调用记录 |

### 4.2 认证系统（`routers/auth.py`）

- `POST /auth/send-code`：发送验证码（打印到控制台，生产环境替换为短信 API）
  - 验证码存 `_sms_codes` 内存 dict，重启清空，**生产环境须换 Redis**
- `POST /auth/login`：验证码登录，首次自动注册，返回 JWT Token
- `GET /auth/me`：获取当前用户信息（需 Bearer Token）

### 4.3 权限与配额系统（`permissions.py`）

见第 9 节套餐权限规则。核心函数：
- `check_permission(user, model, db)` → 抛 403 或返回 plan_type
- `record_usage(user, model, db)` → 写 usage_logs
- `get_quota_status(user, db)` → 返回完整配额信息

### 4.4 会员信息（`routers/member.py`）

- `GET /member/info`：返回 plan_type、到期时间、已用次数、剩余次数、可用模型

### 4.5 支付系统（`routers/payment.py`）

- `POST /pay/create`：创建订单，未配置支付宝 Key 时返回 mock 支付链接
- `POST /pay/callback`：支付宝异步回调，验签后更新订单 + 激活会员
- `GET /pay/mock?trade_no=`：历史兼容接口，当前已禁用，不会触发支付成功
- `GET /pay/status/{order_id}`：前端轮询支付结果

支付宝 SDK：使用 `alipay-sdk-python` 新版 API（`DefaultAlipayClient`），**不是**老版 `AliPay` 类。

套餐价格（在 `routers/payment.py` 的 `PLANS` 字典）：
- monthly：99 元/月，30 天
- quarterly：249 元/季，90 天（membership_type = monthly 级别）
- yearly：799 元/年，365 天

### 4.6 管理员系统（`routers/admin.py`）

所有接口需 `Authorization: Bearer <ADMIN_TOKEN>` 或 JWT role=admin。

| 接口 | 说明 |
|------|------|
| `POST /admin/login` | 验证 Token，前端登录用 |
| `GET /admin/stats` | 总用户数、付费会员数、今日新增、今日对话数 |
| `GET /admin/users` | 分页用户列表，支持 phone 模糊搜索，含会员状态 |
| `GET /admin/orders` | 分页订单列表 |
| `POST /admin/grant` | 手动开通会员（phone + plan + 可选 duration_days） |
| `POST /admin/revoke` | 关闭用户活跃会员 |

### 4.7 核心 Chat 接口（`main.py`）

- `POST /chat`：需登录，检查权限配额，调用 Agent，成功后写 usage_logs
- `ChatRequest` 包含 `message`、`history`、`model`（默认 `deepseek-chat`）

### 4.8 Agent 能力（`agent/core.py` + `tools/`）

- 流式 async generator，工具调用循环
- 工具：Tavily 搜索（`tools/search.py`）+ Playwright 抓取（`tools/scraper.py`）
- 模型由 `AGENT_MODEL` 环境变量控制（当前 claude-sonnet-4-6）

### 4.9 管理后台前端（`admin/index.html`）

单文件，无外部 JS 依赖，通过 `http://localhost:8000/admin/` 访问。功能：
- Token 登录，登录态保存 localStorage，刷新自动恢复
- 数据总览：4 格统计卡片 + 最近订单预览
- 用户管理：搜索、分页、弹窗开通/关闭会员
- 订单列表：分页、状态标签
- 系统配置：价格/模型/额度调整（保存 localStorage，提示需同步后端）

---

## 5. 待开发功能（按优先级）

### P0（核心商业闭环）
- [ ] 真实短信验证码（接入阿里云 / 腾讯云 SMS，替换 `_sms_codes` 内存存储为 Redis）
- [ ] 微信支付接入（补充 `routers/payment.py`）
- [ ] 用户前端完善：会员购买页、支付结果页、个人中心

### P1（稳定性）
- [ ] Alembic 迁移管理（`alembic init`，后续表结构变更走 migration）
- [ ] Redis 替换内存验证码存储
- [ ] 接口限流（防暴力发码、防刷接口）
- [ ] 日志系统（结构化日志，接入 Sentry 或自建）
- [ ] `/chat` 接口流式响应（SSE）

### P2（增长）
- [ ] 邀请码/推荐系统
- [ ] 积分体系（credits 表已预留）
- [ ] 用量统计图表（管理后台）
- [ ] 多 Agent 支持（不同 Agent 对应不同能力组合）

### P3（运营）
- [ ] 站内消息 / 到期提醒推送
- [ ] 优惠券系统
- [ ] 管理后台：用户封禁、批量操作

---

## 6. 重要开发原则

1. **权限检查必须在 Agent 执行前** — `check_permission` 失败直接 403，不消耗 Agent 调用
2. **用量记录必须在成功后** — `record_usage` 在 `run_agent` 完成后调用，失败的请求不扣次数
3. **支付宝 SDK 用新版 API** — `from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient`，老版 `from alipay import AliPay` 在当前安装版本中不存在
4. **验证码不要在 send-code 响应里返回** — 只打印到控制台；测试时临时替换，测后还原
5. **`DEBUG_SMS=true` 在 .env 里** — 这是遗留的开发配置，不影响功能（send-code 响应已还原为正常文本）
6. **本机 Python 环境** — 系统 Python 3.9，包安装路径 `/Users/xunyinchuan/Library/Python/3.9/`，用 `pip3` 不是 `pip`
7. **数据库无密码连接** — `postgresql://xunyinchuan@localhost:5432/ai_agent_platform`，本机用户 xunyinchuan
8. **表结构由 `Base.metadata.create_all` 自动创建** — 服务启动时在 lifespan 里执行，无需手动建表
9. **会员续费逻辑** — `_activate_membership` 在已有 active 会员基础上叠加天数，不是覆盖
10. **quarterly 套餐映射为 monthly 权限级别** — 只是时长更长，权限与 monthly 相同
11. **所有任务改完必须先后台自测** — 改代码后先由开发助手自己跑语法检查、接口/数据库/前端逻辑测试；确认通过后，再给用户无痕测试命令让用户操作。不要把第一轮验证交给用户。

---

## 7. 常用命令

```bash
# 启动服务（热重载）
python3 main.py

# 服务运行在 http://0.0.0.0:8000
# API 文档：http://localhost:8000/docs
# 管理后台：http://localhost:8000/admin/
# 用户前端：用浏览器直接打开 frontend/index.html

# 安装依赖
pip3 install -r requirements.txt

# 杀掉占用 8000 端口的进程
lsof -ti tcp:8000 | xargs kill -9

# 测试管理员接口（替换 TOKEN 为 .env 中的 ADMIN_TOKEN）
curl -s http://localhost:8000/admin/stats -H "Authorization: Bearer <ADMIN_TOKEN>"

# 查看数据库（psql）
psql ai_agent_platform
```

---

## 8. .env 配置说明

```env
# Anthropic（通过 ephone 中转）
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_BASE_URL=https://api.ephone.ai
AGENT_MODEL=claude-sonnet-4-6        # Agent 使用的默认模型
AGENT_MAX_TOKENS=4096
AGENT_TOOL_TIMEOUT=30

# Tavily 搜索
TAVILY_API_KEY=tvly-...

# 服务
HOST=0.0.0.0
PORT=8000

# 数据库（本机无密码）
DATABASE_URL=postgresql://xunyinchuan@localhost:5432/ai_agent_platform
DEBUG_SMS=true                        # 遗留配置，不影响功能

# JWT（上线前必须修改 SECRET_KEY）
JWT_SECRET_KEY=change-me-to-a-long-random-string-in-production
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=10080     # 7 天

# 支付宝（默认不开放售卖；PAYMENTS_ENABLED=true 且配置商户信息后才可创建订单）
ALIPAY_APP_ID=
ALIPAY_PRIVATE_KEY=                   # 多行 key 用 \n 分隔
ALIPAY_PUBLIC_KEY=
ALIPAY_NOTIFY_URL=                    # 必须是公网可访问地址
ALIPAY_DEBUG=false                    # true = 沙箱环境
PAYMENTS_ENABLED=false

# 管理员（上线前必须修改）
ADMIN_TOKEN=change-me-admin-token
```

**上线前必改的 3 项：**
1. `JWT_SECRET_KEY` — 改为随机长字符串
2. `ADMIN_TOKEN` — 改为强随机字符串
3. `ALIPAY_APP_ID` + 密钥 — 填入真实支付宝商户信息

---

## 9. 套餐权限规则

定义在 `permissions.py` 的 `PLAN_RULES` 字典，**修改规则在此文件**：

| 套餐 | 配额 | 统计周期 | 可用模型 |
|------|------|---------|---------|
| `free` | 10 次 | 每天（UTC） | deepseek-chat |
| `monthly` | 500 次 | 每月（UTC，每月1日重置） | claude-sonnet-4-6, deepseek-chat |
| `yearly` | 1000 次 | 每月（UTC，每月1日重置） | claude-opus-4-6, claude-sonnet-4-6, deepseek-chat |

超限返回 `HTTP 403`，错误结构：
```json
{"detail": {"error_code": "QUOTA_EXCEEDED", "message": "..."}}
{"detail": {"error_code": "MODEL_NOT_ALLOWED", "message": "..."}}
```

用户无 active membership 记录 → 默认 free 套餐。

---

## 10. 管理后台访问

- **URL**：`http://localhost:8000/admin/`（由 FastAPI StaticFiles 托管 `admin/` 目录）
- **登录**：输入 `.env` 中的 `ADMIN_TOKEN` 值
- **会话**：Token 保存在浏览器 `localStorage`，关闭 tab 不会退出

**快捷操作路径：**
- 手动开通会员：系统配置 → 快捷操作 → 手动开通会员，或用户管理 → 对应用户行 → 开通
- 关闭会员：用户管理 → 对应用户行 → 关闭
- 查看统计：数据总览（默认首页）

---

## 11. 🧠 Agent 升级方案 · OpenClaw 式任务执行（2026-04-20 新增）

> 本章节是产品核心方向。后续所有 agent 相关改动必须遵循此章节定义的架构。

### 11.1 产品定位

**"零门槛的 OpenClaw SaaS 版"**

- **OpenClaw 的问题**：开源工具，需要用户自己装 Docker / Node.js / 配 API Key，99% 普通人搞不定
- **我们的解法**：把 OpenClaw 级别的 Agent 能力封装成 Web 服务，用户打开网站扫码登录即可使用
- **对标**：本地跑 Stable Diffusion vs Midjourney 的关系
- **用户画像**：阿川公众号读者（100 万粉丝中的普通人），不是技术极客

### 11.2 Agent 工作循环（4 阶段）

**当前 agent** 只有"对话 + 工具调用"两个阶段，**升级后** 必须走完整 4 阶段：

```
用户发消息
   ↓
【阶段 1 · 规划 plan】
   Claude 分析任务，输出 3-5 步执行计划（JSON 格式）
   前端渲染为"任务规划卡片"
   ↓
【阶段 2 · 执行 execute】
   逐步执行，每步调用相应工具（web_search / web_scrape / ...）
   每步的工具调用和结果流式推送到前端
   前端渲染为"工具调用卡片"（可折叠）
   ↓
【阶段 3 · 推理复盘 reflect】
   Claude 基于执行结果，提炼 3-5 个洞察 + 1-2 个风险（JSON）
   前端渲染为"推理总结卡片"
   ↓
【阶段 4 · 推荐决策 recommend】
   Claude 给出 2-4 个下一步选项，按推荐度排序（JSON）
   每个选项：标题 + 星级 + 理由 + 下一步动作
   前端渲染为"推荐决策卡片"（带按钮）
   用户点按钮 → 作为新消息发给 AI，进入下一轮循环
```

### 11.3 事件流协议（SSE / Async Generator）

`agent/core.py::run_agent` 是 async generator，yield 以下 event 类型：

| type | content | 用途 |
|------|---------|------|
| `plan_start` | `"正在规划..."` | 占位提示 |
| `plan` | `{steps: [{index, title, tool}, ...]}` | 规划卡片 |
| `step_start` | `{index, title}` | 开始执行某步 |
| `tool_call` | `{name, input}` | 调用工具 |
| `tool_result` | `{name, result}` | 工具结果 |
| `step_done` | `{index}` | 完成某步 |
| `text_delta` | `"文字片段"` | Claude 的自然语言输出（流式） |
| `reflection` | `{insights: [...], risks: [...]}` | 推理卡片 |
| `recommend` | `{options: [{id, title, stars, reason, next_action}, ...], question}` | 推荐卡片 |
| `done` | - | 整轮结束 |
| `error` | `{message}` | 出错 |

前端根据 `type` 分发到不同的渲染函数。

### 11.4 Agent 专属 System Prompt

每个 agent 有独立的 system prompt 文件，放在 `agent/prompts/`：

```
agent/prompts/
├── side_hustle.py      # 副业 Agent（首批改造）
├── writing.py          # 写作 Agent
├── search.py           # 搜索 Agent
├── scraper.py          # 抓取 Agent
└── coding.py           # 编程 Agent
```

**副业 Agent 人设核心原则**：
- 背景注入：阿川公众号 / 100 万粉丝 / 2000 项目教程 / 10000 成员
- 风格要求：实战、具体、不空谈、数据优先、阿川风格（真实直接带幽默）
- 关注点：变现路径，不做学术分析

### 11.5 前端卡片系统（`frontend/index.html`）

4 种新卡片，统一米黄系视觉（与落地页、登录页保持一致）：

#### 规划卡片（plan-card）
```html
<div class="card plan-card">
  <div class="card-header">📋 任务规划</div>
  <ol class="plan-steps">
    <li class="step done">✓ 搜索热门话题</li>
    <li class="step running">⏳ 分析爆款结构</li>
    <li class="step pending">○ 生成报告</li>
  </ol>
</div>
```

状态切换由 `step_start` / `step_done` 事件触发。

#### 工具调用卡片（tool-card · 可折叠）
```html
<details class="card tool-card" open>
  <summary>🔍 搜索：{{query}}</summary>
  <div class="tool-output">{{result_preview}}</div>
</details>
```

默认展开最新一个，历史的自动折叠。

#### 推理卡片（reflect-card）
```html
<div class="card reflect-card">
  <div class="card-header">🧠 我的发现</div>
  <ul class="insights">{{#insights}}<li>🔍 {{.}}</li>{{/insights}}</ul>
  <ul class="risks">{{#risks}}<li>⚠️ {{.}}</li>{{/risks}}</ul>
</div>
```

#### 推荐决策卡片（recommend-card）
```html
<div class="card recommend-card">
  <div class="card-header">🎯 下一步建议</div>
  {{#options}}
    <div class="option {{recommended?}}">
      <h4>{{stars}} {{title}}</h4>
      <p>{{reason}}</p>
      <button onclick="chooseOption('{{id}}', '{{title}}')">选这个</button>
    </div>
  {{/options}}
  <textarea placeholder="或者告诉我其他想法..."></textarea>
</div>
```

点按钮 → 作为新消息 `"我选 {{title}}"` 发给 agent。

### 11.6 JSON 模式约束

**规划 / 推理 / 推荐** 3 个阶段强制要求 Claude 输出严格 JSON：
- 使用 Anthropic 的 `response_format` 或 prompt 里明确要求 JSON
- 前端解析失败时降级为纯文本卡片，不崩溃
- 后端在解析前先 `re.search(r'\{[\s\S]*\}', text)` 抽取 JSON 块，容忍前后噪声文本

### 11.7 实施路线图

#### Phase 1 · 副业 Agent 试点（本周 · 已开工 2026-04-20）
- ✅ 更新 CLAUDE.md（本章节）
- ⏳ 改造 `agent/core.py` · 加 4 阶段循环
- ⏳ 新建 `agent/prompts/side_hustle.py`
- ⏳ 改造 `frontend/index.html` · 加 4 种卡片
- ⏳ 端到端测试

#### Phase 2 · 其他 4 个 Agent 套用（下周）
副业 Agent 跑通后，复制框架给写作 / 搜索 / 抓取 / 编程。

#### Phase 3 · 技能库系统（下个月）
每个 agent 挂载多个 skill 包（小红书爆款公式 / 公众号选题库 / 副业起步图谱 / ...）。skill 是 markdown + 脚本，灵感来自 OpenClaw 的 Skills 设计。这一步做完，产品有独家护城河。

### 11.8 Agent 配置系统（后台可配置）

用户（阿川）希望在管理后台里给每个 agent 单独配置模型和参数，不用改代码。为此设计以下机制：

#### 数据库表：`agent_configs`

```python
# models.py
class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id = Column(Integer, primary_key=True)
    agent_type = Column(String(32), unique=True, index=True)
    # 5 个 agent_type: side_hustle / writing / search / scraper / coding

    # 4 阶段各自的模型（都可以不同）
    model_plan = Column(String(64), default="claude-opus-4-6")
    model_execute = Column(String(64), default="claude-opus-4-6")
    model_reflect = Column(String(64), default="claude-opus-4-6")
    model_recommend = Column(String(64), default="claude-opus-4-6")

    max_tokens = Column(Integer, default=4096)
    max_plan_steps = Column(Integer, default=5)
    enabled = Column(Boolean, default=True)

    custom_system_prompt = Column(Text, nullable=True)  # 覆盖默认 prompt

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

#### 运行时读取

`agent/core.py` 通过 `get_agent_config(agent_type)` 从数据库读取配置：
- 读不到记录 → 使用 `agent/prompts/*.py` 里的默认值
- 读到 → 覆盖对应字段
- 加 60 秒内存缓存，避免每次请求查数据库

#### 管理后台 UI（Phase 2 结束后实现）

管理后台新增 "Agent 配置" 页面（`admin/index.html`）：

```
┌─────────────────────────────────────────────────────────┐
│  Agent 配置                                              │
│─────────────────────────────────────────────────────────│
│  Agent 类型   规划      执行      反思      推荐    启用│
│  写作        [Opus ▾]  [Opus ▾]  [Sonnet▾] [Opus]  ✓  │
│  搜索        [Sonnet]  [Opus]    [Sonnet]  [Sonnet]✓  │
│  副业        [Opus]    [Opus]    [Opus]    [Opus]  ✓  │
│  编程        [Sonnet]  [Opus]    [Sonnet]  [Sonnet]✓  │
│  抓取        [Haiku]   [Sonnet]  [Haiku]   [Haiku] ✓  │
│                                                         │
│  高级：每个 agent 可展开编辑 System Prompt、max_tokens 等│
│  [保存配置]  [重置为默认]                                │
└─────────────────────────────────────────────────────────┘
```

#### 可选模型列表（管理后台下拉框）

```python
AVAILABLE_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    # DeepSeek 等其他模型后续扩展
]
```

#### 渐进式实施

1. **Phase 1（当前）**：代码按"可配置"写，但暂时只从代码常量读（不读数据库）
2. **Phase 2**：创建 `agent_configs` 表 + 后台 API（`GET/PUT /admin/agent-configs`）
3. **Phase 3**：做管理后台 UI，阿川可以点击切换模型

### 11.9 已否决的方向

| 方向 | 否决理由 |
|------|----------|
| 直接对接 OpenClaw 的 MCP 协议 | 我们是 Web SaaS，OpenClaw 是本地工具，架构不匹配 |
| 从零做 Manus 完整复刻 | 需要浏览器自动化 + 沙箱，工程量 3 个月+，需要团队 |
| 把 5 个 agent 合并成 1 个通用 agent | 弱化现有品牌沉淀，用户心智不清晰 |
| 聊天 UI 做成 Manus / 飞书式双栏 | 小改基于现有 index.html 就够了，不必大改 |
| 登录页花哨插画（旺柴 / 宇航员 / 小动物） | 反复迭代证明极简左右版最合适（CLAUDE.md 11.10 血泪教训） |

### 11.10 开发原则（登录页反复改 6 版的血泪教训）

1. **先画方案，后写代码** —— 方案不对用户确认，代码改 10 版也没用
2. **一次改到位** —— 不要做"试水版"，用户没有耐心看第 7 版
3. **对齐术语** —— 用户说"左右"可能指"整屏两栏"也可能指"卡片内两栏"，必须用草图确认
4. **尊重用户原话** —— 如果用户说"就几个字"，不要擅自加"一个人就是一个团队"这种文案
5. **Subject · 不要擅自加戏** —— 用户让你做 A，不要主动做 A+B


---
