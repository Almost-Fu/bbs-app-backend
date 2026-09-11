# 贴吧社区 bbs-app 后端（FastAPI + MySQL 原生 SQL）

配套前端：`d:\项目\bbs-app-frontend`（uni-app + Vue3）。
**不使用任何 ORM 框架**，所有数据库访问都是手写 SQL（pymysql + 自行实现的连接池 + 事务）。

## 目录结构

```
bbs-app-backend/
├── main.py                 # ★ 后端全部代码：配置 / 连接池 / 鉴权 / 分级权限 / 33 个 RESTful 接口
├── sql/
│   ├── bbs_schema.sql      # 建库建表脚本（8 张表）+ 演示数据（含高级管理员账号）
│   └── migrate_add_super_admin.sql   # 增量升级：给已有库补「高级管理员」角色与账号
├── tools/
│   ├── smoke_test.py       # 冒烟测试：临时起服务跑完整业务闭环（含管理员管理，无需 pytest）
│   └── e2e_with_temp_mysql.bat       # 一键 E2E：临时 MySQL(3307) + 导入 SQL + 跑冒烟测试
├── api_test.http           # VS Code REST Client 接口测试用例（注册→发帖→点赞→评论…）
├── requirements.txt        # 依赖（FastAPI / uvicorn / pymysql / PyJWT / python-multipart / python-dotenv）
├── .env.example            # 配置模板（数据库账号、JWT 密钥等），复制为 .env 使用
├── .gitignore
├── uploads/                # 运行时生成：帖子图片按 uploads/posts/YYYYMM/ 存放
└── README.md
```

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.9+（本机 3.13） | 需要 `fastapi`、`uvicorn`、`pymysql`、`PyJWT`、`python-multipart`、`python-dotenv` |
| MySQL | 8.0（本机 8.0.46，服务名 `MySQL`） | 字符集统一 utf8mb4 |

## 本地启动步骤

### 1. 建库建表（导入 SQL）

```bat
:: 方式一：命令行导入（会创建 bbs_app 库，并写入演示数据）
D:\Mysql\mysql-8.0.46-winx64\bin\mysql.exe -u root -p < sql\bbs_schema.sql

:: 方式二：图形化工具（Navicat / DBeaver）打开 sql\bbs_schema.sql 整段执行
```

> MySQL 服务若未启动：`net start MySQL`（需要管理员权限的终端）。
> 导入后应能看到 8 张表：users / bars / posts / post_images / comments / likes / favorites / follows。
> 演示账号（三种角色各一个）：
> - `superadmin / super123456` —— **高级管理员**（super_admin，可管理管理员账号）
> - `admin / admin123456` —— 管理员（admin，管理贴吧 / 帖子 / 评论）
> - `demo / demo123456` —— 普通用户（user，仅 App）

### 2. 配置 .env

```bat
copy .env.example .env
```

按本机情况修改（至少改 `DB_PASSWORD`）：

```ini
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_NAME=bbs_app
DB_POOL_SIZE=5
JWT_SECRET=换成一串随机字符串
JWT_EXPIRE_HOURS=168
MAX_IMAGE_MB=5
```

### 3. 安装依赖

```bat
cd /d d:\项目\bbs-app-backend
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
:: 国内网络慢可加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 启动服务

```bat
:: 推荐写法：直接跑 main.py（文件末尾已内置 uvicorn.run，不依赖 Scripts 目录下的 uvicorn.exe）
.venv\Scripts\python.exe main.py

:: 或者用模块方式启动（效果相同，带热重载）
.venv\Scripts\python.exe -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

:: 若已激活虚拟环境（.venv\Scripts\activate），也可以直接
uvicorn main:app --reload
```

启动日志里出现 `[bbs-app] MySQL 连接正常` 即表示数据库配置正确。
打开 <http://127.0.0.1:8000/docs> 可直接在 Swagger 页面上点选调试所有接口。

### 5. 验证

```bat
curl http://127.0.0.1:8000/api/health
:: {"code":0,"message":"ok","data":{"service":"bbs-app-backend","db":true,...}}
```

## 接口清单

| 方法 | 路径 | 说明 | 登录 |
|---|---|---|---|
| GET | `/api/health` | 健康检查（含数据库连通性） | - |
| POST | `/api/auth/register` | 注册（直接返回 token） | - |
| POST | `/api/auth/login` | 登录，返回 token | - |
| GET | `/api/auth/me` | 当前登录用户 | ✔ |
| GET | `/api/bars` | 所有贴吧列表（含是否关注） | 可选 |
| GET | `/api/bars/{bar_id}` | 贴吧详情（帖数 / 关注数） | 可选 |
| GET | `/api/bars/{bar_id}/posts` | 吧内帖子分页列表 | 可选 |
| POST | `/api/bars/{bar_id}/follow` | 关注贴吧 | ✔ |
| DELETE | `/api/bars/{bar_id}/follow` | 取消关注 | ✔ |
| GET | `/api/users/me/followed-bars` | 我关注的吧 | ✔ |
| POST | `/api/bars` | 创建贴吧（仅管理员） | ✔ admin |
| PUT | `/api/bars/{bar_id}` | 编辑贴吧（仅管理员，吧名唯一） | ✔ admin |
| DELETE | `/api/bars/{bar_id}` | 删除贴吧（仅管理员；吧内帖子/关注/收藏随外键级联删除） | ✔ admin |
| GET | `/api/posts` | 首页信息流 / 条件筛选（分页） | 可选 |
| GET | `/api/search` | 搜索帖子 | 可选 |
| POST | `/api/posts` | 发布帖子（multipart，最多 9 张图） | ✔ |
| GET | `/api/posts/{post_id}` | 帖子详情（浏览量 +1） | 可选 |
| DELETE | `/api/posts/{post_id}` | 删除帖子（作者或管理员，软删除） | ✔ |
| POST | `/api/posts/{post_id}/like` | 帖子点赞 | ✔ |
| DELETE | `/api/posts/{post_id}/like` | 取消帖子点赞 | ✔ |
| POST | `/api/posts/{post_id}/favorite` | 收藏帖子 | ✔ |
| DELETE | `/api/posts/{post_id}/favorite` | 取消收藏 | ✔ |
| GET | `/api/posts/{post_id}/comments` | 评论分页列表 | 可选 |
| POST | `/api/posts/{post_id}/comments` | 发表评论（帖子评论数 +1） | ✔ |
| POST | `/api/comments/{comment_id}/like` | 评论点赞 | ✔ |
| DELETE | `/api/comments/{comment_id}/like` | 取消评论点赞 | ✔ |
| GET | `/api/users/me/posts` | 我的帖子 | ✔ |
| GET | `/api/users/me/favorites` | 我的收藏 | ✔ |
| PATCH | `/api/users/me` | 修改我的昵称 / 头像 | ✔ |
| POST | `/api/users/me/avatar` | 上传自定义头像（multipart，字段名 `file`，上传即写库） | ✔ |
| POST | `/api/posts/{post_id}/forward` | 转发帖子（转发数 +1） | ✔ |
| POST | `/api/bars/{bar_id}/visit` | 记录足迹：我进过这个吧（幂等） | ✔ |
| GET | `/api/users/me/footprints` | 我的足迹（角标 = 上次浏览后该吧新增帖数） | ✔ |
| GET | `/api/users/me/notifications` | 互动消息：点赞 / 回复 / @我（由 likes、comments 聚合） | ✔ |
| GET | `/api/users/me/notifications/unread` | 互动消息未读数（App 底部角标） | ✔ |
| POST | `/api/users/me/notifications/read` | 互动消息标记已读（角标清零） | ✔ |
| GET | `/api/admin/admins` | 管理员列表（分页 + 关键字/角色/状态筛选） | ✔ **高级管理员** |
| GET | `/api/admin/admins/{admin_id}` | 管理员详情 | ✔ **高级管理员** |
| POST | `/api/admin/admins` | 新增管理员（可指定 admin / super_admin） | ✔ **高级管理员** |
| PATCH | `/api/admin/admins/{admin_id}/role` | 调整角色（管理员 ⇄ 高级管理员） | ✔ **高级管理员** |
| PATCH | `/api/admin/admins/{admin_id}/status` | 启用 / 禁用管理员 | ✔ **高级管理员** |
| PATCH | `/api/admin/admins/{admin_id}/password` | 重置管理员密码 | ✔ **高级管理员** |
| DELETE | `/api/admin/admins/{admin_id}` | 撤销管理员权限（降级为普通用户） | ✔ **高级管理员** |
| GET | `/api/admin/users` | 用户列表（分页 + 关键字 / 角色 / 状态筛选） | ✔ admin |
| GET | `/api/admin/users/{user_id}` | 用户详情（含发帖数 / 评论数） | ✔ admin |
| PATCH | `/api/admin/users/{user_id}/status` | 启用 / 禁用用户（禁用后无法登录 App） | ✔ admin |
| GET | `/api/admin/comments` | 全部评论（分页 + 关键字 / 帖子筛选，含所属帖子标题） | ✔ admin |
| DELETE | `/api/comments/{comment_id}` | 删除违规评论（仅管理员，软删除 + 帖子评论数 -1） | ✔ admin |

### 静态资源（图片）

| 路径 | 内容 | 是否持久 |
|---|---|---|
| `/static/avatars/*.png` | 头像图（随代码发布：内置 8 张 + 1 张默认图） | **持久**（在仓库里，重新部署不会丢） |
| `/uploads/avatars/...` | 用户**自己上传**的头像（`POST /api/users/me/avatar`） | 存实例磁盘，**重新部署会丢**（前端会回落到默认头像，要长期保存需挂 Persistent Disk） |
| `/uploads/posts/...` | 用户上传的帖子图片 | 存实例磁盘，**重新部署会丢**（需挂 Persistent Disk） |

> **头像规则**：`users.avatar` 只存图片地址（`/static/avatars/xxx.png` 或 http 图片链接）。
> 传 emoji 之类的纯文本会被接口层拒绝（HTTP 422）；服务启动自检会**自动把历史数据里的 emoji 头像迁移成图片**（幂等，可反复执行）。
> 头像图放后端而不是前端的原因：管理后台（另一个站点）与 App 需要共用同一份头像。

### 角色与权限（users.role 三档）

| 角色 | 值 | 能做什么 |
|---|---|---|
| 普通用户 | `user` | 只能使用 App（发帖、评论、点赞、收藏、关注） |
| 管理员 | `admin` | 后台可管理贴吧板块（新增 / 编辑 / 删除）、删除帖子、用户管理（列表 / 详情 / 禁用启用）、评论管理（全部评论 / 删除违规评论） |
| **高级管理员** | `super_admin` | 在管理员权限之上，额外可**管理管理员账号**：新增、改角色、启用/禁用、重置密码、撤销权限 |

安全护栏（后端强制，前端同步置灰）：
- 不能对自己执行 **禁用 / 降级 / 撤销**（避免把自己锁在门外，也因此系统里始终至少保留一个可用高级管理员）；
- 普通管理员访问 `/api/admin/admins*` 一律返回 `403 需要高级管理员权限`；
- 被禁用的账号登录会返回 `403 账号已被禁用`（App 端与后台同时失效）。

> 已有数据库升级：执行 `sql/migrate_add_super_admin.sql`（ALTER 角色枚举 + 写入 superadmin），无需重建数据。

### 统一响应格式

```json
{ "code": 0, "message": "ok", "data": { } }
```

- 成功：`code = 0`；失败：`code = HTTP 状态码`（400 参数 / 401 未登录 / 403 无权限 / 404 不存在 / 500 服务器错误）
- 分页接口的 `data`：`{ list, page, pageSize, total, totalPages, hasMore }`
- 分页参数同时支持小写下划线与前端习惯：`?page=1&pageSize=10`

### 与 uni-app 前端对接要点

1. **跨域**：已开启 CORS（`allow_origins=["*"]`），H5 端可直接 `uni.request` 调 `http://127.0.0.1:8000`。
2. **登录态**：登录/注册拿到的 `token` 存本地，后续请求带上
   `header: { Authorization: 'Bearer ' + token }`。
3. **字段名**：接口返回的字段名与前端 `utils/store.js` 里的 `posts / comments / bars` 完全一致
   （`barId` / `barName` / `barImg` / `authorAvatar` / `commentCount` / `liked` / `favorited` / `followed` …），
   前端只需把原来的 Storage 读写换成请求接口。
4. **发布带图**：`uni.uploadFile` 的 `formData` 传 `barId / title / content / tag`，
   文件字段名（`name`）用 **files**，可多选；服务端落盘后返回 `/uploads/posts/...`，
   前端展示时拼上后端地址即可（如 `BASE_URL + post.images[0]`）。
5. **时间**：接口直接返回 `time`（刚刚 / 3小时前 / 昨天 …）和 `createdAt`（`YYYY-MM-DD HH:mm:ss`）。

## 接口测试说明

### 方式一：Swagger（最省事）

打开 <http://127.0.0.1:8000/docs> → 先调 `POST /api/auth/login` 拿 token →
点右上角 **Authorize** 填 `Bearer <token>` → 之后所有需要登录的接口都能直接点「Try it out」。

### 方式二：VS Code REST Client

用 VS Code 打开 `api_test.http`（装 REST Client 扩展），逐个请求点 “Send Request” 即可，覆盖：
注册 → 登录 → 贴吧列表 → 吧内帖子 → 发布文字帖 → 发布带图帖 → 帖子详情 → 点赞/取消 → 收藏/取消 → 评论 → 评论点赞 → 关注吧。

### 方式三：curl（Windows cmd）

```bat
:: 1) 登录，拿到 token
curl -X POST http://127.0.0.1:8000/api/auth/login ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"demo\",\"password\":\"demo123456\"}"

:: 2) 贴吧列表（游客可访问）
curl http://127.0.0.1:8000/api/bars

:: 3) 吧内帖子分页
curl "http://127.0.0.1:8000/api/bars/1/posts?page=1&pageSize=10"

:: 4) 发文字帖（把 <TOKEN> 换成上一步的 token）
curl -X POST http://127.0.0.1:8000/api/posts ^
  -H "Authorization: Bearer <TOKEN>" ^
  -F "barId=1" -F "title=测试标题" -F "content=测试内容" -F "tag=技术交流"

:: 5) 发带图帖（-F files=@ 可重复，最多 9 张）
curl -X POST http://127.0.0.1:8000/api/posts ^
  -H "Authorization: Bearer <TOKEN>" ^
  -F "barId=1" -F "title=带图帖" -F "content=看看我的图" ^
  -F "files=@D:\pic1.jpg" -F "files=@D:\pic2.png"

:: 6) 帖子点赞 / 取消点赞
curl -X POST   http://127.0.0.1:8000/api/posts/1/like -H "Authorization: Bearer <TOKEN>"
curl -X DELETE http://127.0.0.1:8000/api/posts/1/like -H "Authorization: Bearer <TOKEN>"

:: 7) 收藏 / 取消收藏
curl -X POST   http://127.0.0.1:8000/api/posts/1/favorite -H "Authorization: Bearer <TOKEN>"
curl -X DELETE http://127.0.0.1:8000/api/posts/1/favorite -H "Authorization: Bearer <TOKEN>"

:: 8) 发表评论
curl -X POST http://127.0.0.1:8000/api/posts/1/comments ^
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" ^
  -d "{\"text\":\"第一次评论\"}"

:: 9) 评论点赞 / 取消
curl -X POST   http://127.0.0.1:8000/api/comments/101/like -H "Authorization: Bearer <TOKEN>"
curl -X DELETE http://127.0.0.1:8000/api/comments/101/like -H "Authorization: Bearer <TOKEN>"

:: 10) 关注贴吧 / 取消关注
curl -X POST   http://127.0.0.1:8000/api/bars/2/follow -H "Authorization: Bearer <TOKEN>"
curl -X DELETE http://127.0.0.1:8000/api/bars/2/follow -H "Authorization: Bearer <TOKEN>"
```

### 数据库侧自查（原生 SQL）

```sql
USE bbs_app;
-- 首页信息流等价 SQL（帖子 + 作者 + 吧）
SELECT p.id, p.title, p.like_count, p.comment_count, u.nickname AS author, b.name AS bar_name
  FROM posts p JOIN users u ON u.id = p.user_id JOIN bars b ON b.id = p.bar_id
 WHERE p.status = 1 ORDER BY p.id DESC LIMIT 0, 10;

-- 点赞判定依据
SELECT * FROM likes WHERE user_id = 2 AND target_type = 'post' AND target_id = 1;

-- 帖子图片（最多 9 张，按 sort_order）
SELECT image_url, sort_order FROM post_images WHERE post_id = 2 ORDER BY sort_order;
```

### 方式四：一键 E2E（命令，最彻底）

```bat
:: 1) 冒烟测试：临时起服务，校验路由 / 统一响应 / CORS / 401 / 422（数据库可用时跑完整业务闭环）
.venv\Scripts\python.exe tools\smoke_test.py

:: 2) 完整 E2E：自动起一个临时 MySQL 实例（3307 端口，独立数据目录，不碰你的 3306），
::    导入 sql/bbs_schema.sql，再跑上面的冒烟测试，最后关库并清理，日志在 _e2e.txt
cmd /c tools\e2e_with_temp_mysql.bat
```

冒烟测试覆盖：注册 → 登录 → 当前用户 → 密码错误 401 → 贴吧列表 → 吧内帖子分页 → 关注/取关 →
发帖(上传 2 张图) → 帖子详情 → 点赞/重复点赞/取消 → 收藏/取消 → 评论 → 评论数同步 → 评论点赞/取消 →
搜索 → 删除帖子 → 已删除返回 404。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `/api/health` 里 `db=false` | MySQL 未启动（管理员终端执行 `net start MySQL`），或 `.env` 的账号密码不对 |
| 启动报 `Access denied for user 'root'@'localhost'` | `DB_PASSWORD` 填错 |
| 报 `Unknown database 'bbs_app'` | 还没导入 `sql/bbs_schema.sql` |
| H5 端报跨域 | 确认请求地址带了 `http://`（CORS 已全开，通常是写成了相对路径） |
| 上传返回 400「不支持的图片格式」 | 只允许 jpg/jpeg/png/gif/webp/bmp |
| 上传返回 400「单张图片不能超过 5MB」 | 前端 `uni.chooseImage` 加 `sizeType: ['compressed']`，或调大 `.env` 的 `MAX_IMAGE_MB` |
| 图片上传成功但前端显示不出 | 用 `BASE_URL + post.images[0]` 拼接访问（静态目录挂在 `/uploads`） |
| 401「请先登录」 | 缺少 `Authorization: Bearer <token>`，或 token 已过期（默认 7 天） |
