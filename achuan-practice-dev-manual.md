# 阿川 AI 超级助手 · 实战区开发者手册

> 最后更新：2026-05-05
> 项目根目录：`~/Projects/ai-agent-platform/`

---

## 一、项目架构

### 整体入口流程
```
login.html → choose.html → index.html（功能区）
                         → practice.html（实战区）
                         → admin-login.html → admin-practice.html（管理后台）
```

### 技术栈
- **前端**：单文件 HTML（无框架）· CSS 变量体系 · 原生 JS
- **后端**：FastAPI · SQLAlchemy Session · PostgreSQL
- **数据库**：`postgresql://xunyinchuan@localhost:5432/ai_agent_platform`
- **认证**：JWT（jose 库）· `/auth/me` 获取用户信息

### 文件清单

| 文件 | 路径 | 说明 |
|------|------|------|
| practice.html | frontend/ | 实战区主页 |
| article.html | frontend/ | 文章阅读页 |
| admin-login.html | frontend/ | 管理员登录页 |
| admin-practice.html | frontend/ | 管理后台 |
| practice.py | routers/ | 后端 API（当前 v10） |
| practice_md/ | 根目录 | Markdown 文章文件存储 |
| static/uploads/practice/ | 根目录 | 上传图片存储 |

---

## 二、数据库表结构

### practice_projects（实战项目/训练营）
```
id, slug, title, description, project_type(experience/camp), category,
status(published/open/ongoing/ended), cover_emoji, cover_color, cover_image(TEXT),
md_filename, sort_order, start_date, is_published, is_featured, view_count,
author_id, author_name, created_at, updated_at
```

### practice_tags / practice_project_tags
```
tags: id, name, tag_type, sort_order, created_at
project_tags: project_id, tag_id
```

### practice_courses（视频课程）
```
id, slug, title, description, cover_url, cover_emoji, category,
instructor, price, status, sort_order, lesson_count, total_duration,
view_count, is_published, created_at, updated_at
```

### practice_lessons（课时）
```
id, course_id, title, description, lesson_type(video/article),
video_url, video_duration, article_content, sort_order,
is_free, is_published, view_count, created_at, updated_at
```

### practice_subscribes（订阅/报名）
```
id, user_id, phone, project_slug, source_page, created_at, notified_at
```

### practice_likes（点赞）
```
id, user_id, project_slug, created_at
UNIQUE(user_id, project_slug)
```

### practice_comments（评论）
```
id, user_id, username, project_slug, content, created_at
```

### practice_views（浏览记录·去重）
```
id, user_id, project_slug, created_at
UNIQUE(user_id, project_slug)
```

### practice_settings（系统设置）
```
key(PK), value, updated_at
预置: slogan, hero_desc, author_name, monthly_price, yearly_price,
      private_price, announcement, footer_text
```

### users 表新增字段
```
is_admin BOOLEAN DEFAULT false
```

### practice_projects 新增字段
```
is_featured BOOLEAN DEFAULT false
cover_image TEXT
author_id BIGINT
author_name VARCHAR(64) DEFAULT '阿川'
```

---

## 三、后端 API 清单（routers/practice.py）

### 公开接口
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /practice/projects | 项目列表（支持 project_type/category/is_featured/keyword/分页） |
| GET | /practice/projects/{slug} | 项目详情 |
| GET | /practice/projects/{slug}/md | Markdown 内容 |
| GET | /practice/tags | 标签列表 |
| GET | /practice/courses | 课程列表 |
| GET | /practice/courses/{slug} | 课程详情+课时 |
| GET | /practice/courses/{slug}/lessons/{id} | 课时详情 |
| GET | /practice/categories | 分类列表 |
| GET | /practice/course-categories | 课程分类列表 |
| GET | /practice/settings | 系统设置（公开） |

### 用户接口
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /practice/subscribe | 报名/订阅 |
| GET | /practice/subscribe/check | 检查订阅状态 |
| POST | /practice/like | 点赞/取消（toggle） |
| GET | /practice/like/status | 查询点赞状态 |
| POST | /practice/comments | 发评论 |
| GET | /practice/comments | 评论列表 |
| POST | /practice/projects/{slug}/view | 浏览量+1（每用户计一次） |
| GET | /practice/access-check | 权限检查（年卡/私教/管理员） |
| POST | /practice/publish | 发布内容 |
| PUT | /practice/projects/{slug} | 编辑内容（仅作者/管理员） |
| DELETE | /practice/projects/{slug} | 删除内容（软删除） |
| POST | /practice/upload-image | 上传图片 |
| POST | /practice/proxy-image | 代理下载外部图片 |

### 管理员接口
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /practice/admin/login | 管理员登录验证 |
| GET | /practice/admin/stats | 数据统计 |
| GET | /practice/admin/projects | 内容列表（含未发布） |
| PUT | /practice/admin/projects/{slug}/feature | 推荐/取消 |
| PUT | /practice/admin/projects/{slug}/status | 改状态 |
| PUT | /practice/admin/projects/{slug}/publish | 上架/下架 |
| GET | /practice/admin/users | 用户列表 |
| PUT | /practice/admin/users/toggle-admin | 设置管理员 |
| GET | /practice/admin/subscribes | 订阅列表 |
| DELETE | /practice/admin/comments/{id} | 删除评论 |
| POST | /practice/admin/batch | 批量操作 |
| PUT | /practice/admin/settings | 更新系统设置 |

---

## 四、权限体系

| 用户等级 | 浏览实战区 | 发表内容 | 评论/点赞 | 管理后台 |
|---------|---------|---------|---------|---------|
| 免费/月卡 | ❌ 提示升级 | ❌ | ❌ | ❌ |
| 年卡/私教 | ✅ | ✅ | ✅ | ❌ |
| 管理员 | ✅ | ✅ | ✅ | ✅ |

**权限检查逻辑**：
1. 前端从 `/auth/me` 获取 user_id
2. 调 `/practice/access-check?user_id=X`
3. 后端检查 is_admin → 查 memberships(plan_type + expire_at)
4. 年卡/私教/管理员放行，其他显示升级提示遮罩

**管理后台认证**：
- 独立登录页 `admin-login.html`
- 使用 `admin_token` / `admin_user_id`（和前台 token 隔离）
- 所有管理 API 用 `_check_admin(user_id, db)` 验证

---

## 五、功能模块详情

### 实战区（practice.html）
- **三大 Tab**：实战经验 / 训练营 / 视频课程
- **实战经验**：全部/推荐 + 分类标签 + 卡片/信息流视图切换
- **训练营**：Banner(进行中) + 时间线分组 + 状态badge
- **视频课程**：课程卡片网格 + 分类筛选 + 热门/最新排序
- **搜索**：后端 keyword 搜索 · 350ms 防抖
- **分页**：底部「加载更多」按钮
- **发布按钮**：右下角浮动 ✏ · 富文本编辑器
- **权限遮罩**：未授权用户显示升级提示

### 文章详情页（article.html）
- 顶部导航（和 practice.html 一致）
- 作者栏：头像 + 名字 + 标签 + 日期
- Markdown 渲染（marked.js + Prism 代码高亮）
- 互动区：点赞(toggle) + 评论 + 订阅
- 浏览量（每用户计一次）

### 富文本发布编辑器
- contenteditable div（所见即所得）
- 工具栏：加粗 / 引用 / 列表 / 分隔线 / 图片
- 图片上传：按钮选择 / Cmd+V 粘贴
- 公众号图片：自动代理下载到本地（绕过防盗链）
- 图文排版：粘贴时按原文顺序图文交错插入
- 发布时 editorToMarkdown() 转为 Markdown 存储

### 管理后台（admin-practice.html）
- **左侧导航**：数据看板/实战经验/训练营/视频课程/标签/评论/订阅/用户/设置
- **数据看板**：5个核心指标 + 最新内容 + 数据概况
- **内容管理**：搜索/筛选/分页/推荐/上下架/删除/批量操作/复选框
- **训练营**：改状态(open/ongoing/ended)
- **用户管理**：搜索/会员筛选/设置管理员
- **评论管理**：列表+删除
- **订阅管理**：订阅记录列表
- **系统设置**：Slogan/描述/作者名/公告/价格配置

---

## 六、开发操作手册

### 启动后端
```bash
cd ~/Projects/ai-agent-platform && python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 杀掉占用端口
```bash
lsof -ti:8000 | xargs kill -9
```

### 无痕模式测试
```bash
# 实战区
open -na "Google Chrome" --args --incognito "http://localhost:8000/practice.html"

# 文章页
open -na "Google Chrome" --args --incognito "http://localhost:8000/article.html?slug=real-talk-focus"

# 管理后台
open -na "Google Chrome" --args --incognito "http://localhost:8000/admin-login.html"
```

### 测试 API
```bash
# 项目列表
curl -s http://localhost:8000/practice/projects | python3 -m json.tool | head -30

# 权限检查
curl -s "http://localhost:8000/practice/access-check?user_id=2" | python3 -m json.tool

# 管理员统计
curl -s "http://localhost:8000/practice/admin/stats?user_id=2" | python3 -m json.tool
```

### 改完自测流程
```bash
# 语法检查（不写 pyc 缓存，避免权限问题）
python3 -c "import pathlib; files=['main.py','models.py','routers/media.py','routers/conversations.py','agent/core.py','tools/image_gen.py']; [compile(pathlib.Path(p).read_text(), p, 'exec') for p in files if pathlib.Path(p).exists()]; print('python syntax ok')"

# 健康检查
curl -s http://localhost:8000/health
```

规则：所有任务改完后，开发助手必须先自己跑后台自测；涉及前端交互的，还要做本地逻辑/浏览器验证。自测通过后，才给用户无痕测试命令让用户操作。

### 文件替换流程
```bash
# 后端路由
cp ~/Downloads/practice_router_vN.py ~/Projects/ai-agent-platform/routers/practice.py

# 前端页面
cp ~/Downloads/practice.html ~/Projects/ai-agent-platform/frontend/practice.html
cp ~/Downloads/article.html ~/Projects/ai-agent-platform/frontend/article.html
cp ~/Downloads/admin-practice.html ~/Projects/ai-agent-platform/frontend/admin-practice.html
cp ~/Downloads/admin-login.html ~/Projects/ai-agent-platform/frontend/admin-login.html
```

### 数据库连接
```bash
psql -U xunyinchuan -d ai_agent_platform
```

### 测试账号
- 手机号：13260018535
- user_id：2
- is_admin：true
- 会员：yearly（年卡）

---

## 七、沟通铁律

1. 中文沟通
2. 命令逐行给
3. 改前必备份
4. 直接给方案不铺垫
5. 不改 index.html（功能区）
6. 每次改完给终端无痕测试命令
7. practice_* 表结构不能改原有字段（可以加新字段）
8. 所有代码改完先由开发助手自测通过，再让用户测试

---

## 八、版本记录

| 版本 | 说明 |
|------|------|
| v1-v3 | 基础框架 · 三模块 · 卡片布局 |
| v4 | 权限拦截 · 发布编辑器 · 图片上传 |
| v5 | is_featured · 编辑删除 · 信息流模式 |
| v6 | cover_image字段 · 富文本编辑器 · 公众号图片转存 |
| v7 | 后端keyword搜索 · 分页加载 |
| v8 | 管理后台Phase1 · 独立登录 · 数据看板 · 内容管理 |
| v9 | Phase2 · 用户管理 · 订阅管理 · 评论删除 · 批量操作 |
| v10 | Phase3 · 系统设置 · 前端动态配置 |

---

## 九、待开发功能

- [ ] 数据导出（CSV）
- [ ] 管理后台课程CRUD（新建/编辑/删除课程和课时）
- [ ] 管理后台标签CRUD
- [ ] 趋势图表（近7天/30天折线图）
- [ ] 消息通知系统
- [ ] 用户头像上传
- [ ] 文章收藏功能
- [ ] 文章分享功能
