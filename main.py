# -*- coding: utf-8 -*-
"""
贴吧社区（bbs-app）后端服务
================================================================================
技术栈：FastAPI + MySQL 8.0 + 原生 SQL（pymysql），**不使用任何 ORM 框架**

设计目标：直接对接前端 bbs-app-frontend（uni-app + Vue3）
  1. 统一响应体：{"code": 0, "message": "ok", "data": ...}；失败时 code != 0 且带 HTTP 状态码
  2. 字段全部用 camelCase（barId / commentCount / authorAvatar ...），与前端 utils/store.js
     里的字段名一致，前端拿到即可渲染
  3. 已开启 CORS，H5 端跨域直接调用；登录返回 JWT，放在请求头 Authorization: Bearer <token>
  4. 图片走 multipart/form-data 上传（对应前端 uni.chooseImage + uni.uploadFile），
     落盘到 uploads/ 后通过 /uploads/... 静态访问，帖子最多 9 张
  5. 分页统一返回 {list, page, pageSize, total, totalPages, hasMore}，方便下拉加载更多

启动：uvicorn main:app --reload --host 0.0.0.0 --port 8000
文档：http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import ssl
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Optional

import jwt
import pymysql
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# =============================================================================
# 1. 配置（全部走环境变量：本地读 .env，云端读 Render 的 Environment Variables）
#    APP_ENV=development 时用本地友好的默认值；APP_ENV=production 时按线上安全默认值
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # 本地开发用；云端没有 .env 文件也不会报错

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# ---- 服务端口：Render / Railway 等平台会注入 PORT，必须优先使用它 ----
PORT = int(os.getenv("PORT", "8000"))

# ---- MySQL（换成 Aiven 云库只需改这几个环境变量，代码无需改动）----
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "bbs_app")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "8" if IS_PRODUCTION else "5"))
DB_WAIT_TIMEOUT = int(os.getenv("DB_WAIT_TIMEOUT", "10"))  # 池满时等待可用连接的秒数

# ---- 连接重试（远程库首连慢、Aiven 维护重启、网络抖动时非常有用）----
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))             # 单次连接超时（秒）
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "3"))              # 连接失败后的重试次数
DB_CONNECT_RETRY_DELAY = float(os.getenv("DB_CONNECT_RETRY_DELAY", "1.5"))  # 首次重试等待秒数（指数退避）

# ---- TLS：Aiven 强制要求 SSL，生产环境默认开启 ----
DB_SSL = os.getenv("DB_SSL", "true" if IS_PRODUCTION else "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
DB_SSL_CA = os.getenv("DB_SSL_CA", "").strip()          # CA 证书文件路径（有文件时用这个）
DB_SSL_CA_PEM = os.getenv("DB_SSL_CA_PEM", "").strip()  # CA 证书内容（直接把 aiven-ca.pem 文本贴进环境变量）

JWT_SECRET = os.getenv("JWT_SECRET", "bbs-app-dev-secret-change-me-32bytes-minimum")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))  # 默认 7 天

# ---- CORS：逗号分隔的前端域名白名单；留空 = 放开所有（本地开发/首次联调方便）----
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]

MAX_POST_IMAGES = 9  # 与前端「最多 9 张图」保持一致
MAX_IMAGE_MB = int(os.getenv("MAX_IMAGE_MB", "5"))
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# ---- 上传目录：Render 默认磁盘是临时的（重新部署即丢图）。
#      需要长期保存图片时，在 Render 挂一个 Persistent Disk，再把 UPLOAD_DIR 指到挂载点，
#      例如 UPLOAD_DIR=/var/data/uploads ----
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))).expanduser()
POST_IMAGE_DIR = UPLOAD_DIR / "posts"

PBKDF2_ITERATIONS = 200_000  # 密码哈希迭代次数（与 sql 种子数据一致）

# =============================================================================
# 2. MySQL 连接池（pymysql + queue.Queue，全部原生 SQL，无 ORM）
# =============================================================================
# ---- 角色常量（与 users.role 枚举保持一致）----
ROLE_USER = "user"                # 普通用户：只能用 App
ROLE_ADMIN = "admin"              # 管理员：可管理贴吧/帖子/评论
ROLE_SUPER_ADMIN = "super_admin"  # 高级管理员：额外可管理「管理员账号」
ADMIN_ROLES = (ROLE_ADMIN, ROLE_SUPER_ADMIN)
ROLE_TEXT = {
    ROLE_USER: "普通用户",
    ROLE_ADMIN: "管理员",
    ROLE_SUPER_ADMIN: "高级管理员",
}


def db_error_to_http(exc: pymysql.MySQLError) -> HTTPException:
    """把 pymysql 驱动异常转成统一响应体的 HTTPException：
    - 连接类错误（2002/2003/2006/2013/2055）→ 503：提示数据库不可用
    - 其它 SQL 执行错误 → 500：带原始报错，方便本地排查
    这样做的好处：前端拿到的始终是 {code,message,data}，而且响应会经过 CORS 中间件，
    H5 端看到的是真实错误信息，而不是被浏览器报成"跨域失败"。
    """
    code = exc.args[0] if exc.args else 0
    if code in (2002, 2003, 2006, 2013, 2055):
        return HTTPException(status_code=503, detail=f"数据库不可用：{exc}")
    return HTTPException(status_code=500, detail=f"数据库执行失败：{exc}")


_CA_TEMP_FILE: Optional[Path] = None


def write_ca_temp_file() -> Path:
    """把环境变量 DB_SSL_CA_PEM 里的证书内容落成临时文件。

    为什么需要它：Render 只提供环境变量、没有现成文件；而 pymysql 校验 CA 需要文件路径。
    直接把 Aiven 控制台下载的 ca.pem 全文粘到环境变量里即可（支持带 \\n 的单行写法）。
    """
    global _CA_TEMP_FILE
    if _CA_TEMP_FILE and _CA_TEMP_FILE.exists():
        return _CA_TEMP_FILE
    pem = DB_SSL_CA_PEM.replace("\\n", "\n")
    target = Path(tempfile.gettempdir()) / "bbs-app-aiven-ca.pem"
    target.write_text(pem.strip() + "\n", encoding="utf-8")
    _CA_TEMP_FILE = target
    print(f"[bbs-app] 已把 DB_SSL_CA_PEM 写入临时 CA 文件：{target}")
    return target


def build_ssl_context() -> Optional[ssl.SSLContext]:
    """按环境变量构造 pymysql 的 SSL 上下文（Aiven 强制要求 TLS）：

    - DB_SSL=false                          → 返回 None，不加密（本地开发）
    - DB_SSL=true + DB_SSL_CA / DB_SSL_CA_PEM → 加密并校验证书（推荐）
    - DB_SSL=true 但没有 CA                 → 加密但不校验证书（等价 sslmode=REQUIRED，至少不裸奔）
    """
    if not DB_SSL:
        return None

    ctx = ssl.create_default_context()
    ca_path = DB_SSL_CA or (str(write_ca_temp_file()) if DB_SSL_CA_PEM else "")
    if ca_path:
        ctx.load_verify_locations(cafile=ca_path)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MySQLPool:
    """极简连接池：
    - 连接按需创建，最多 DB_POOL_SIZE 条，用完归还
    - 每次取出前 ping(reconnect=True)，避免 MySQL 长时间空闲后断连
    - 归还前 rollback，保证不把未结束的事务留给下一个请求
    """

    def __init__(self, size: int = 5) -> None:
        self._size = max(1, size)
        self._idle: "Queue[pymysql.connections.Connection]" = Queue(maxsize=self._size)
        self._created = 0
        self._lock = threading.Lock()

    def _connect(self) -> pymysql.connections.Connection:
        """建立一个新连接（带重试）：DictCursor 让查询结果直接是 dict，省去手工映射

        - Aiven 等云数据库要求 TLS：DB_SSL=true 时自动加密连接（配了 CA 则校验证书）
        - 远程库首连慢 / 网络抖动 / 云库维护重启：按 DB_CONNECT_RETRIES 指数退避重试
        """
        ssl_ctx = build_ssl_context()
        attempts = max(1, DB_CONNECT_RETRIES)
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                return pymysql.connect(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                    connect_timeout=DB_CONNECT_TIMEOUT,
                    read_timeout=DB_CONNECT_TIMEOUT * 3,
                    write_timeout=DB_CONNECT_TIMEOUT * 3,
                    ssl=ssl_ctx,
                )
            except pymysql.MySQLError as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                wait = DB_CONNECT_RETRY_DELAY * (2 ** (attempt - 1))  # 1.5s → 3s → 6s …
                print(
                    f"[bbs-app] 数据库连接失败（第 {attempt}/{attempts} 次）：{exc}；"
                    f"{wait:.1f}s 后重试"
                )
                time.sleep(wait)

        raise last_exc if last_exc else RuntimeError("数据库连接失败")

    @contextmanager
    def acquire(self):
        conn = None
        try:
            conn = self._idle.get_nowait()
        except Empty:
            with self._lock:  # 还没到上限就新建，否则等待其他请求归还
                if self._created < self._size:
                    try:
                        conn = self._connect()
                    except pymysql.MySQLError as exc:
                        # 连不上数据库：转 503，交给统一异常处理器给出可读提示
                        raise db_error_to_http(exc)
                    self._created += 1
            if conn is None:
                try:
                    conn = self._idle.get(timeout=DB_WAIT_TIMEOUT)
                except Empty:
                    raise HTTPException(status_code=503, detail="数据库连接繁忙，请稍后重试")
        try:
            conn.ping(reconnect=True)
        except Exception:
            self._discard(conn)
            raise HTTPException(
                status_code=500,
                detail="数据库连接失败：请确认 MySQL 已启动，且 .env 中的 DB_USER / DB_PASSWORD 正确",
            )
        try:
            yield conn
        finally:
            self._release(conn)

    def _release(self, conn: pymysql.connections.Connection) -> None:
        """归还连接：先回滚未提交事务；连接已断开则丢弃"""
        try:
            if conn.open:
                conn.rollback()
        except Exception:
            self._discard(conn)
            return
        try:
            self._idle.put_nowait(conn)
        except Full:
            self._discard(conn)

    def _discard(self, conn: Optional[pymysql.connections.Connection]) -> None:
        """丢弃一条不可用连接：关闭连接并把计数扣回去（conn 为空时什么都不做，避免误减）"""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._created = max(0, self._created - 1)


pool = MySQLPool(DB_POOL_SIZE)

# =============================================================================
# 3. 原生 SQL 执行助手（所有接口都通过这几个函数访问数据库）
# =============================================================================
def query_all(sql: str, params: tuple | list | None = None) -> list[dict]:
    """查询多行，返回 list[dict]"""
    with pool.acquire() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchall()
        except pymysql.MySQLError as exc:
            raise db_error_to_http(exc)


def query_one(sql: str, params: tuple | list | None = None) -> Optional[dict]:
    """查询单行，无结果返回 None"""
    rows = query_all(sql, params)
    return rows[0] if rows else None


def query_value(sql: str, params: tuple | list | None = None, default: Any = None) -> Any:
    """查询单个标量值（COUNT(*) 之类）"""
    row = query_one(sql, params)
    if not row:
        return default
    return list(row.values())[0]


@contextmanager
def transaction():
    """写操作统一走事务：正常结束 commit，异常 rollback 后向上抛"""
    with pool.acquire() as conn:
        try:
            yield conn
            conn.commit()
        except pymysql.MySQLError as exc:
            conn.rollback()
            raise db_error_to_http(exc)
        except Exception:
            conn.rollback()
            raise


def execute(sql: str, params: tuple | list | None = None) -> int:
    """执行写 SQL，返回受影响行数"""
    with transaction() as conn:
        with conn.cursor() as cur:
            return cur.execute(sql, params or ())


def execute_many(sql: str, seq_params: list[tuple]) -> int:
    """批量写入（如帖子多图一次性插入）"""
    if not seq_params:
        return 0
    with transaction() as conn:
        with conn.cursor() as cur:
            return cur.executemany(sql, seq_params)


def insert_returning_id(sql: str, params: tuple | list | None = None) -> int:
    """执行 INSERT 并返回自增主键"""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return int(cur.lastrowid)


# =============================================================================
# 4. 密码哈希 + JWT（登录态）
# =============================================================================
def hash_password(password: str) -> str:
    """PBKDF2-SHA256 加盐哈希：pbkdf2_sha256$迭代次数$盐(hex)$哈希(hex)"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码，使用 compare_digest 做定时安全比较"""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError, AttributeError):
        return False


def create_token(user_id: int, role: str) -> tuple[str, int]:
    """签发 JWT，返回 (token, 有效期秒数)"""
    expires_in = JWT_EXPIRE_HOURS * 3600
    payload = {
        "sub": str(user_id),   # 用户ID
        "role": role,          # user / admin，便于前端做权限判断
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(seconds=expires_in),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), expires_in


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="登录凭证无效")


# auto_error=False：未带 token 时返回 None，由业务代码决定是 401 还是按游客处理
bearer_scheme = HTTPBearer(auto_error=False, description="登录返回的 token，格式：Bearer <token>")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """必须登录的接口依赖：解析 token → 查库返回当前用户"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="请先登录")
    payload = decode_token(credentials.credentials)
    user = query_one(
        "SELECT id, username, nickname, avatar, role, status FROM users WHERE id = %s",
        (int(payload.get("sub", 0)),),
    )
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    if user["status"] != 1:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[dict]:
    """可选登录的接口依赖：游客返回 None（用于返回 liked / favorited / followed 状态）"""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return get_current_user(credentials)
    except HTTPException:
        return None


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """管理员专属接口依赖（users.role ∈ admin / super_admin）"""
    if user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_super_admin(user: dict = Depends(get_current_user)) -> dict:
    """高级管理员专属接口依赖：只有 super_admin 能管理管理员账号"""
    if user["role"] != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="需要高级管理员权限")
    return user


# =============================================================================
# 5. 图片上传（对应前端 uni.chooseImage / uni.uploadFile，最多 9 张）
# =============================================================================
def ensure_upload_dirs() -> None:
    POST_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


async def save_post_images(files: list[UploadFile]) -> list[str]:
    """保存上传图片到 uploads/posts/YYYYMM/，返回可访问的相对 URL 列表"""
    ensure_upload_dirs()
    month_dir = POST_IMAGE_DIR / datetime.now().strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_IMAGE_EXT:
            raise HTTPException(status_code=400, detail=f"不支持的图片格式：{ext or '未知'}")
        data = await f.read()
        if not data:
            continue
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail=f"单张图片不能超过 {MAX_IMAGE_MB}MB")
        filename = f"{uuid.uuid4().hex}{ext}"
        (month_dir / filename).write_bytes(data)
        urls.append(f"/uploads/posts/{month_dir.name}/{filename}")
    return urls


def parse_image_urls(raw: Optional[str]) -> list[str]:
    """解析前端可能直接传来的图片地址：支持 JSON 数组字符串或逗号分隔"""
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return [str(x).strip() for x in data if str(x).strip()][:MAX_POST_IMAGES]
        except json.JSONDecodeError:
            return []
    return [x.strip() for x in raw.split(",") if x.strip()][:MAX_POST_IMAGES]


# =============================================================================
# 6. 展示格式化（时间 / 数量），让前端拿到就能直接渲染
# =============================================================================
def fmt_time(dt: Optional[datetime]) -> str:
    """相对时间：刚刚 / x分钟前 / x小时前 / 昨天 / x天前 / YYYY-MM-DD"""
    if not dt:
        return ""
    delta = datetime.now() - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}小时前"
    if seconds < 172800:
        return "昨天"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}天前"
    return dt.strftime("%Y-%m-%d")


def fmt_count(num: Optional[int]) -> str:
    """数量展示：12345 → 1.2万（与前端吧头部的“帖子 2.3万”风格一致）"""
    n = int(num or 0)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


# =============================================================================
# 7. 应用实例、中间件（CORS 跨域）、静态图片、统一响应
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动准备：建上传目录 + 打印运行环境 + 数据库连通性自检（失败只告警，不阻断启动，方便先看 /docs）"""
    ensure_upload_dirs()
    cors_desc = ",".join(CORS_ORIGINS) if CORS_ORIGINS else "*（未配置白名单，放开所有）"
    print(
        f"[bbs-app] 环境={APP_ENV} 端口={PORT} "
        f"数据库={DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} "
        f"SSL={'on' if DB_SSL else 'off'} CORS={cors_desc}"
    )
    print(f"[bbs-app] 上传目录={UPLOAD_DIR}")
    try:
        query_value("SELECT 1 AS ok")
        print("[bbs-app] MySQL 连接正常，启动自检通过")
    except Exception as exc:  # noqa: BLE001 —— 启动阶段只需提示，不抛出
        print(f"[bbs-app] 警告：数据库暂不可用（{exc}）")
        if IS_PRODUCTION:
            print(
                "[bbs-app] 请检查 Render 的环境变量：DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME，"
                "以及 Aiven 控制台的白名单是否放通了 Render 的出网 IP"
            )
        else:
            print("[bbs-app] 请先在 MySQL 执行 sql/bbs_schema.sql，再检查 .env 里的 DB_* 配置")
    yield


app = FastAPI(
    title="贴吧社区 bbs-app 后端 API",
    description="FastAPI + MySQL 原生 SQL（无 ORM），已适配 uni-app 前端 bbs-app-frontend",
    version="1.0.0",
    lifespan=lifespan,
)

# ---- CORS：生产环境用 CORS_ORIGINS 白名单（逗号分隔的前端域名）；未配置时放开 ----
# 说明：uni-app 的 App / 小程序端不发 Origin，不受 CORS 限制；真正需要白名单的是
#      「H5 网页版」与「bbs-admin-web」，把它们在 Vercel 上的域名填进 CORS_ORIGINS 即可。
# 注意：token 放在 Authorization 请求头里（不是 Cookie），所以 allow_credentials 保持 False，
#       否则浏览器会拒绝 allow_origins=["*"] 的响应。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# ---- 上传的图片：通过 /uploads/xxx 静态访问，前端直接 <image :src="baseUrl + image" /> ----
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


# ---- 统一响应体：{code, message, data}；成功 code=0，失败 code 取 HTTP 状态码 ----
def ok(data: Any = None, message: str = "ok") -> dict:
    return {"code": 0, "message": message, "data": data}


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    """业务异常（401 未登录 / 403 无权限 / 404 不存在 / 400 参数错误）统一成同一种结构"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """Pydantic 参数校验失败"""
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": "参数校验失败", "data": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """兜底异常：把错误信息带回，方便本地联调排查（生产环境建议只记录日志）

    注意：这个处理器的响应由最外层中间件发出，不经 CORSMiddleware，
    所以这里手动补一个 CORS 头，避免 H5 端把 500 误报成跨域失败。
    """
    print(f"[bbs-app] 未捕获异常 {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": f"服务器内部错误：{exc}", "data": None},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# =============================================================================
# 8. 请求模型（Pydantic）
# =============================================================================
class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=20, description="登录用户名（唯一）")
    password: str = Field(..., min_length=6, max_length=32, description="密码（至少 6 位）")
    nickname: str = Field("", max_length=20, description="昵称，留空则用用户名")
    avatar: str = Field("🙂", max_length=255, description="头像 emoji 或图片地址")


class LoginIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=20)
    password: str = Field(..., min_length=1, max_length=32)


class CommentIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000, description="评论内容（前端字段名 text）")
    parentId: Optional[int] = Field(None, description="父评论ID，二级回复用，可空")


class BarIn(BaseModel):
    """管理员创建贴吧"""
    name: str = Field(..., min_length=1, max_length=32, description="吧名，如 前端吧")
    icon: str = Field("💬", max_length=16)
    image: Optional[str] = Field(None, max_length=255, description="吧图 URL，可空")
    intro: str = Field("", max_length=255, description="吧简介")
    owner: str = Field("官方", max_length=32, description="吧主昵称")
    sort: int = Field(0, description="排序值，越小越靠前")


class AdminCreateIn(BaseModel):
    """高级管理员：新增管理员账号"""
    username: str = Field(..., min_length=2, max_length=20, description="登录用户名（唯一）")
    password: str = Field(..., min_length=6, max_length=32, description="初始密码（至少 6 位）")
    nickname: str = Field("", max_length=20, description="昵称，留空则用用户名")
    avatar: str = Field("🛡️", max_length=255, description="头像 emoji")
    role: str = Field(ROLE_ADMIN, description="角色：admin 管理员 / super_admin 高级管理员")


class AdminStatusIn(BaseModel):
    """启用 / 禁用管理员"""
    status: int = Field(..., ge=0, le=1, description="1 启用 / 0 禁用")


class AdminRoleIn(BaseModel):
    """调整管理员角色"""
    role: str = Field(..., description="admin 管理员 / super_admin 高级管理员")


class AdminPasswordIn(BaseModel):
    """重置管理员密码"""
    password: str = Field(..., min_length=6, max_length=32, description="新密码（至少 6 位）")


# =============================================================================
# 9. 序列化：数据库行 → 前端使用的 camelCase 结构
# =============================================================================
# 帖子查询的公共 SQL（列表与详情共用，保证字段一致）
POST_SELECT_SQL = """
SELECT p.id, p.bar_id, p.user_id, p.tag, p.title, p.content,
       p.like_count, p.comment_count, p.forward_count, p.view_count, p.is_top, p.created_at,
       u.nickname AS author, u.username AS author_username, u.avatar AS author_avatar,
       b.name AS bar_name, b.icon AS bar_icon, b.image AS bar_image
  FROM posts p
  JOIN users u ON u.id = p.user_id
  JOIN bars  b ON b.id = p.bar_id
"""


def post_to_dict(
    row: dict,
    images: Optional[list[str]] = None,
    liked: bool = False,
    favorited: bool = False,
) -> dict:
    """帖子行 → 前端 post 结构（字段名与前端 utils/store.js 完全对齐）"""
    return {
        "id": row["id"],
        "barId": row["bar_id"],
        "barName": row["bar_name"],
        "barIcon": row["bar_icon"],
        "barImg": row["bar_image"] or "",
        "tag": row["tag"],
        "title": row["title"],
        "content": row["content"],
        "author": row["author"],
        "authorId": row["user_id"],
        "authorAvatar": row["author_avatar"],
        "time": fmt_time(row["created_at"]),
        "createdAt": iso(row["created_at"]),
        "images": images or [],
        "likes": row["like_count"],
        "commentCount": row["comment_count"],
        "forwards": row["forward_count"],
        "views": row["view_count"],
        "isTop": bool(row["is_top"]),
        "liked": liked,
        "favorited": favorited,
    }


def comment_to_dict(row: dict, liked: bool = False) -> dict:
    """评论行 → 前端 comment 结构：{id, author, authorAvatar, text, time, likes, liked}"""
    return {
        "id": row["id"],
        "postId": row["post_id"],
        "author": row["author"],
        "authorId": row["user_id"],
        "authorAvatar": row["author_avatar"],
        "text": row["content"],
        "time": fmt_time(row["created_at"]),
        "createdAt": iso(row["created_at"]),
        "likes": row["like_count"],
        "liked": liked,
        "parentId": row["parent_id"],
    }


def bar_to_dict(row: dict, followed: bool = False, post_count: int = 0, follow_count: int = 0) -> dict:
    """贴吧行 → 前端 bar 结构：{id, name, icon, img, desc, owner, posts, members, followed}"""
    return {
        "id": row["id"],
        "name": row["name"],
        "icon": row["icon"],
        "img": row["image"] or "",
        "desc": row["intro"],
        "owner": row["owner"],
        "postCount": post_count,
        "followCount": follow_count,
        # 前端吧头部展示「帖子 2.3万 / 关注 1.2万」，这里直接给格式化好的字符串
        "posts": fmt_count(post_count),
        "members": fmt_count(follow_count),
        "followed": followed,
    }


# ---- 批量查状态（避免逐条查询的 N+1 问题）----
def fetch_images_map(post_ids: list[int]) -> dict[int, list[str]]:
    """批量取帖子图片：{post_id: [url, ...]}，按 sort_order 排序"""
    if not post_ids:
        return {}
    placeholders = ",".join(["%s"] * len(post_ids))
    rows = query_all(
        f"""SELECT post_id, image_url FROM post_images
             WHERE post_id IN ({placeholders}) ORDER BY post_id, sort_order""",
        tuple(post_ids),
    )
    result: dict[int, list[str]] = {}
    for r in rows:
        result.setdefault(int(r["post_id"]), []).append(r["image_url"])
    return result


def fetch_liked_ids(user: Optional[dict], target_type: str, ids: list[int]) -> set[int]:
    """当前用户在这批 id 中点过赞的集合（游客返回空集）"""
    if not user or not ids:
        return set()
    placeholders = ",".join(["%s"] * len(ids))
    rows = query_all(
        f"""SELECT target_id FROM likes
             WHERE user_id = %s AND target_type = %s AND target_id IN ({placeholders})""",
        (user["id"], target_type, *ids),
    )
    return {int(r["target_id"]) for r in rows}


def fetch_favorited_ids(user: Optional[dict], post_ids: list[int]) -> set[int]:
    """当前用户收藏过的帖子 id 集合"""
    if not user or not post_ids:
        return set()
    placeholders = ",".join(["%s"] * len(post_ids))
    rows = query_all(
        f"SELECT post_id FROM favorites WHERE user_id = %s AND post_id IN ({placeholders})",
        (user["id"], *post_ids),
    )
    return {int(r["post_id"]) for r in rows}


def fetch_followed_bar_ids(user: Optional[dict], bar_ids: Optional[list[int]] = None) -> set[int]:
    """当前用户关注的贴吧 id 集合（bar_ids 为空则取全部）"""
    if not user:
        return set()
    if bar_ids:
        placeholders = ",".join(["%s"] * len(bar_ids))
        rows = query_all(
            f"SELECT bar_id FROM follows WHERE user_id = %s AND bar_id IN ({placeholders})",
            (user["id"], *bar_ids),
        )
    else:
        rows = query_all("SELECT bar_id FROM follows WHERE user_id = %s", (user["id"],))
    return {int(r["bar_id"]) for r in rows}


# ---- 分页 ----
def normalize_page(page: int, page_size: int) -> tuple[int, int, int]:
    """规整分页参数，返回 (page, page_size, offset)"""
    page = max(1, int(page or 1))
    page_size = min(50, max(1, int(page_size or 10)))
    return page, page_size, (page - 1) * page_size


def page_data(items: list, total: int, page: int, page_size: int) -> dict:
    """分页响应：前端下拉加载看 hasMore，也可以直接用 total / totalPages"""
    total = int(total or 0)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return {
        "list": items,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
        "hasMore": page * page_size < total,
    }


# =============================================================================
# 10. 接口：健康检查 + 用户认证
#    说明：所有访问数据库的接口都用同步 def（FastAPI 会放进线程池执行），
#          避免阻塞事件循环；只有需要 await 读文件的接口才用 async def。
# =============================================================================
@app.get("/api/health", tags=["通用"], summary="健康检查（确认服务与数据库连接是否正常）")
def health():
    db_ok, db_msg = True, "ok"
    try:
        query_value("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        db_ok, db_msg = False, str(exc)
    return ok(
        {
            "service": "bbs-app-backend",
            "env": APP_ENV,
            "db": db_ok,
            "dbMessage": db_msg,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


@app.post("/api/auth/register", tags=["用户"], summary="注册（可直接拿到 token，前端注册后免再登录）")
def register(body: RegisterIn):
    username = body.username.strip()
    nickname = (body.nickname or "").strip() or username

    if query_one("SELECT id FROM users WHERE username = %s", (username,)):
        raise HTTPException(status_code=400, detail="该用户名已被注册")

    user_id = insert_returning_id(
        """INSERT INTO users (username, password_hash, nickname, avatar, role)
           VALUES (%s, %s, %s, %s, %s)""",
        (username, hash_password(body.password), nickname, body.avatar or "🙂", "user"),
    )
    user = query_one(
        "SELECT id, username, nickname, avatar, role FROM users WHERE id = %s", (user_id,)
    )
    token, expires_in = create_token(user_id, user["role"])
    return ok({"token": token, "tokenType": "Bearer", "expiresIn": expires_in, "user": user})


@app.post("/api/auth/login", tags=["用户"], summary="登录（返回 token）")
def login(body: LoginIn):
    row = query_one(
        """SELECT id, username, nickname, avatar, role, status, password_hash
             FROM users WHERE username = %s""",
        (body.username.strip(),),
    )
    # 不区分「用户不存在」和「密码错误」，避免暴露账号是否注册
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if row["status"] != 1:
        raise HTTPException(status_code=403, detail="账号已被禁用")

    token, expires_in = create_token(row["id"], row["role"])
    user = {k: row[k] for k in ("id", "username", "nickname", "avatar", "role")}
    return ok({"token": token, "tokenType": "Bearer", "expiresIn": expires_in, "user": user})


@app.get("/api/auth/me", tags=["用户"], summary="获取当前登录用户信息")
def me(user: dict = Depends(get_current_user)):
    return ok(user)


# =============================================================================
# 11. 接口：贴吧（板块）
# =============================================================================
# 贴吧列表/详情公共 SQL：顺带算出「帖子数 / 关注数」，前端吧头部直接用
BAR_SELECT_SQL = """
SELECT b.id, b.name, b.icon, b.image, b.intro, b.owner, b.sort,
       (SELECT COUNT(*) FROM posts   p WHERE p.bar_id = b.id AND p.status = 1) AS post_count,
       (SELECT COUNT(*) FROM follows f WHERE f.bar_id = b.id)                   AS follow_count
  FROM bars b
"""


@app.get("/api/bars", tags=["贴吧"], summary="获取所有贴吧列表（登录后附带是否已关注）")
def list_bars(
    keyword: Optional[str] = Query(None, description="按吧名模糊搜索，可空"),
    user: Optional[dict] = Depends(get_optional_user),
):
    sql = BAR_SELECT_SQL
    params: list = []
    if keyword and keyword.strip():
        sql += " WHERE b.name LIKE %s"
        params.append(f"%{keyword.strip()}%")
    sql += " ORDER BY b.sort ASC, b.id ASC"

    rows = query_all(sql, tuple(params))
    followed_ids = fetch_followed_bar_ids(user, [r["id"] for r in rows])
    data = [
        bar_to_dict(r, r["id"] in followed_ids, r["post_count"], r["follow_count"]) for r in rows
    ]
    return ok(data)


@app.get("/api/bars/{bar_id}", tags=["贴吧"], summary="获取单个贴吧详情（前端吧页头部）")
def get_bar(bar_id: int, user: Optional[dict] = Depends(get_optional_user)):
    row = query_one(BAR_SELECT_SQL + " WHERE b.id = %s", (bar_id,))
    if not row:
        raise HTTPException(status_code=404, detail="贴吧不存在")
    followed = bar_id in fetch_followed_bar_ids(user, [bar_id])
    return ok(bar_to_dict(row, followed, row["post_count"], row["follow_count"]))


@app.get("/api/bars/{bar_id}/posts", tags=["贴吧"], summary="进入指定贴吧，分页获取吧内帖子")
def list_bar_posts(
    bar_id: int,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(10, ge=1, le=50, description="每页条数"),
    user: Optional[dict] = Depends(get_optional_user),
):
    bar = query_one(BAR_SELECT_SQL + " WHERE b.id = %s", (bar_id,))
    if not bar:
        raise HTTPException(status_code=404, detail="贴吧不存在")

    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(
        "SELECT COUNT(*) AS c FROM posts WHERE bar_id = %s AND status = 1", (bar_id,), 0
    )
    rows = query_all(
        POST_SELECT_SQL
        + """ WHERE p.bar_id = %s AND p.status = 1
              ORDER BY p.is_top DESC, p.id DESC LIMIT %s OFFSET %s""",
        (bar_id, page_size, offset),
    )

    post_ids = [r["id"] for r in rows]
    images_map = fetch_images_map(post_ids)
    liked_ids = fetch_liked_ids(user, "post", post_ids)
    fav_ids = fetch_favorited_ids(user, post_ids)

    items = [
        post_to_dict(
            r,
            images_map.get(r["id"], []),
            r["id"] in liked_ids,
            r["id"] in fav_ids,
        )
        for r in rows
    ]
    followed = bar_id in fetch_followed_bar_ids(user, [bar_id])
    return ok(
        {
            # 前端吧页一次请求即可渲染头部 + 列表
            "bar": bar_to_dict(bar, followed, bar["post_count"], bar["follow_count"]),
            **page_data(items, total, page, page_size),
        }
    )


@app.post("/api/bars/{bar_id}/follow", tags=["贴吧"], summary="关注贴吧")
def follow_bar(bar_id: int, user: dict = Depends(get_current_user)):
    if not query_one("SELECT id FROM bars WHERE id = %s", (bar_id,)):
        raise HTTPException(status_code=404, detail="贴吧不存在")
    # INSERT IGNORE + 唯一键：重复关注不会报错，也不会插重复数据
    execute("INSERT IGNORE INTO follows (user_id, bar_id) VALUES (%s, %s)", (user["id"], bar_id))
    count = query_value("SELECT COUNT(*) AS c FROM follows WHERE bar_id = %s", (bar_id,), 0)
    return ok({"followed": True, "followCount": count, "members": fmt_count(count)})


@app.delete("/api/bars/{bar_id}/follow", tags=["贴吧"], summary="取消关注贴吧")
def unfollow_bar(bar_id: int, user: dict = Depends(get_current_user)):
    execute("DELETE FROM follows WHERE user_id = %s AND bar_id = %s", (user["id"], bar_id))
    count = query_value("SELECT COUNT(*) AS c FROM follows WHERE bar_id = %s", (bar_id,), 0)
    return ok({"followed": False, "followCount": count, "members": fmt_count(count)})


@app.get("/api/users/me/followed-bars", tags=["贴吧"], summary="我关注的吧（前端进吧页 / 侧边抽屉）")
def my_followed_bars(user: dict = Depends(get_current_user)):
    rows = query_all(
        BAR_SELECT_SQL
        + """ JOIN follows f2 ON f2.bar_id = b.id
              WHERE f2.user_id = %s ORDER BY f2.id DESC""",
        (user["id"],),
    )
    return ok([bar_to_dict(r, True, r["post_count"], r["follow_count"]) for r in rows])


@app.post("/api/bars", tags=["贴吧"], summary="创建贴吧（仅管理员，演示 users.role 的用法）")
def create_bar(body: BarIn, admin: dict = Depends(require_admin)):
    name = body.name.strip()
    if query_one("SELECT id FROM bars WHERE name = %s", (name,)):
        raise HTTPException(status_code=400, detail="该贴吧已存在")
    bar_id = insert_returning_id(
        "INSERT INTO bars (name, icon, image, intro, owner, sort) VALUES (%s, %s, %s, %s, %s, %s)",
        (name, body.icon, body.image, body.intro, body.owner, body.sort),
    )
    row = query_one(BAR_SELECT_SQL + " WHERE b.id = %s", (bar_id,))
    return ok(bar_to_dict(row, False, 0, 0), message="贴吧创建成功")


# =============================================================================
# 12. 接口：帖子（信息流 / 发布 / 详情 / 点赞 / 收藏 / 搜索）
# =============================================================================
def get_post_row_or_404(post_id: int) -> dict:
    row = query_one(POST_SELECT_SQL + " WHERE p.id = %s AND p.status = 1", (post_id,))
    if not row:
        raise HTTPException(status_code=404, detail="帖子不存在或已删除")
    return row


def post_payload(post_id: int, user: Optional[dict], bump_view: bool = False) -> dict:
    """取单个帖子的完整前端结构（含图片 / 点赞 / 收藏状态），详情与发帖返回复用"""
    row = get_post_row_or_404(post_id)
    if bump_view:
        execute("UPDATE posts SET view_count = view_count + 1 WHERE id = %s", (post_id,))
        row["view_count"] = row["view_count"] + 1
    images_map = fetch_images_map([post_id])
    liked = post_id in fetch_liked_ids(user, "post", [post_id])
    favorited = post_id in fetch_favorited_ids(user, [post_id])
    return post_to_dict(row, images_map.get(post_id, []), liked, favorited)


@app.get("/api/posts", tags=["帖子"], summary="首页信息流 / 按条件筛选帖子（分页）")
def list_posts(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize", description="每页条数"),
    bar_id: Optional[int] = Query(None, alias="barId", description="只看某个吧"),
    author_id: Optional[int] = Query(None, alias="authorId", description="只看某个用户发的"),
    keyword: Optional[str] = Query(None, description="标题/正文/作者/吧名模糊搜索"),
    order: str = Query("latest", description="排序：latest 最新 / hot 最热"),
    user: Optional[dict] = Depends(get_optional_user),
):
    where = ["p.status = 1"]
    params: list = []
    if bar_id:
        where.append("p.bar_id = %s")
        params.append(bar_id)
    if author_id:
        where.append("p.user_id = %s")
        params.append(author_id)
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        where.append("(p.title LIKE %s OR p.content LIKE %s OR u.nickname LIKE %s OR b.name LIKE %s)")
        params.extend([kw, kw, kw, kw])

    where_sql = " WHERE " + " AND ".join(where)
    order_sql = " ORDER BY p.like_count DESC, p.id DESC" if order == "hot" else " ORDER BY p.is_top DESC, p.id DESC"

    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(
        f"""SELECT COUNT(*) AS c FROM posts p
              JOIN users u ON u.id = p.user_id
              JOIN bars  b ON b.id = p.bar_id{where_sql}""",
        tuple(params),
        0,
    )
    rows = query_all(
        POST_SELECT_SQL + where_sql + order_sql + " LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )

    post_ids = [r["id"] for r in rows]
    images_map = fetch_images_map(post_ids)
    liked_ids = fetch_liked_ids(user, "post", post_ids)
    fav_ids = fetch_favorited_ids(user, post_ids)
    items = [
        post_to_dict(r, images_map.get(r["id"], []), r["id"] in liked_ids, r["id"] in fav_ids)
        for r in rows
    ]
    return ok(page_data(items, total, page, page_size))


@app.get("/api/search", tags=["帖子"], summary="搜索帖子（等价于 /api/posts?keyword=）")
def search_posts(
    keyword: str = Query(..., min_length=1, description="搜索关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
    user: Optional[dict] = Depends(get_optional_user),
):
    return list_posts(page=page, page_size=page_size, bar_id=None, author_id=None, keyword=keyword, order="latest", user=user)


@app.post("/api/posts", tags=["帖子"], summary="发布帖子（multipart 上传最多 9 张图片）")
async def create_post(
    bar_id: int = Form(..., alias="barId", description="发布到哪个吧"),
    title: str = Form(..., min_length=1, max_length=100),
    content: str = Form(..., min_length=1, description="正文"),
    tag: str = Form("未分类", max_length=32, description="分类标签"),
    image_urls: Optional[str] = Form(None, alias="imageUrls", description="已有图片地址，JSON 数组或逗号分隔，可空"),
    files: list[UploadFile] = File(default=[], description="本地图片文件，字段名 files，最多 9 张"),
    user: dict = Depends(get_current_user),
):
    if not query_one("SELECT id FROM bars WHERE id = %s", (bar_id,)):
        raise HTTPException(status_code=404, detail="贴吧不存在，无法发布")

    # 图片 = 本地新上传的 + 前端直接传来的地址，总数不超过 9 张
    uploaded = await save_post_images(files or [])
    images = (uploaded + parse_image_urls(image_urls))[:MAX_POST_IMAGES]
    if len(uploaded) + len(parse_image_urls(image_urls)) > MAX_POST_IMAGES:
        raise HTTPException(status_code=400, detail=f"图片最多 {MAX_POST_IMAGES} 张")

    # 帖子 + 图片一次性写入，保证要么都成功要么都回滚
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO posts (bar_id, user_id, tag, title, content, like_count, comment_count)
                   VALUES (%s, %s, %s, %s, %s, 0, 0)""",
                (bar_id, user["id"], (tag or "未分类").strip() or "未分类", title.strip(), content.strip()),
            )
            new_post_id = int(cur.lastrowid)
        if images:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO post_images (post_id, image_url, sort_order) VALUES (%s, %s, %s)",
                    [(new_post_id, url, idx) for idx, url in enumerate(images)],
                )

    return ok(post_payload(new_post_id, user), message="发布成功")


@app.get("/api/posts/{post_id}", tags=["帖子"], summary="获取帖子详情（含图片、点赞与收藏状态，浏览量 +1）")
def get_post_detail(post_id: int, user: Optional[dict] = Depends(get_optional_user)):
    return ok(post_payload(post_id, user, bump_view=True))


@app.delete("/api/posts/{post_id}", tags=["帖子"], summary="删除帖子（仅作者本人或管理员，软删除）")
def delete_post(post_id: int, user: dict = Depends(get_current_user)):
    row = query_one("SELECT id, user_id FROM posts WHERE id = %s AND status = 1", (post_id,))
    if not row:
        raise HTTPException(status_code=404, detail="帖子不存在或已删除")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="只能删除自己发布的帖子")
    execute("UPDATE posts SET status = 0 WHERE id = %s", (post_id,))
    return ok({"id": post_id}, message="已删除")


@app.post("/api/posts/{post_id}/like", tags=["帖子"], summary="帖子点赞（重复点赞不会重复计数）")
def like_post(post_id: int, user: dict = Depends(get_current_user)):
    get_post_row_or_404(post_id)
    with transaction() as conn:
        with conn.cursor() as cur:
            # likes 表有 (user_id, target_type, target_id) 唯一键，INSERT IGNORE 天然防重复
            cur.execute(
                "INSERT IGNORE INTO likes (user_id, target_type, target_id) VALUES (%s, 'post', %s)",
                (user["id"], post_id),
            )
            if cur.rowcount:  # 本次才是新增点赞，才去加计数
                cur.execute("UPDATE posts SET like_count = like_count + 1 WHERE id = %s", (post_id,))
    likes = query_value("SELECT like_count AS c FROM posts WHERE id = %s", (post_id,), 0)
    return ok({"liked": True, "likes": likes})


@app.delete("/api/posts/{post_id}/like", tags=["帖子"], summary="取消帖子点赞")
def unlike_post(post_id: int, user: dict = Depends(get_current_user)):
    get_post_row_or_404(post_id)
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM likes WHERE user_id = %s AND target_type = 'post' AND target_id = %s",
                (user["id"], post_id),
            )
            if cur.rowcount:
                # GREATEST 兜底，避免计数被减成负数
                cur.execute(
                    "UPDATE posts SET like_count = GREATEST(like_count - 1, 0) WHERE id = %s",
                    (post_id,),
                )
    likes = query_value("SELECT like_count AS c FROM posts WHERE id = %s", (post_id,), 0)
    return ok({"liked": False, "likes": likes})


@app.post("/api/posts/{post_id}/favorite", tags=["帖子"], summary="收藏帖子")
def favorite_post(post_id: int, user: dict = Depends(get_current_user)):
    get_post_row_or_404(post_id)
    execute(
        "INSERT IGNORE INTO favorites (user_id, post_id) VALUES (%s, %s)", (user["id"], post_id)
    )
    return ok({"favorited": True, "postId": post_id}, message="已收藏")


@app.delete("/api/posts/{post_id}/favorite", tags=["帖子"], summary="取消收藏帖子")
def unfavorite_post(post_id: int, user: dict = Depends(get_current_user)):
    execute("DELETE FROM favorites WHERE user_id = %s AND post_id = %s", (user["id"], post_id))
    return ok({"favorited": False, "postId": post_id}, message="已取消收藏")


@app.get("/api/users/me/posts", tags=["用户"], summary="我的帖子（前端「我的帖子」页）")
def my_posts(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
    user: dict = Depends(get_current_user),
):
    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(
        "SELECT COUNT(*) AS c FROM posts WHERE user_id = %s AND status = 1", (user["id"],), 0
    )
    rows = query_all(
        POST_SELECT_SQL
        + " WHERE p.user_id = %s AND p.status = 1 ORDER BY p.id DESC LIMIT %s OFFSET %s",
        (user["id"], page_size, offset),
    )
    post_ids = [r["id"] for r in rows]
    images_map = fetch_images_map(post_ids)
    liked_ids = fetch_liked_ids(user, "post", post_ids)
    fav_ids = fetch_favorited_ids(user, post_ids)
    items = [
        post_to_dict(r, images_map.get(r["id"], []), r["id"] in liked_ids, r["id"] in fav_ids)
        for r in rows
    ]
    return ok(page_data(items, total, page, page_size))


@app.get("/api/users/me/favorites", tags=["用户"], summary="我的收藏（前端「我的收藏」页）")
def my_favorites(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
    user: dict = Depends(get_current_user),
):
    page, page_size, offset = normalize_page(page, page_size)
    total = query_value("SELECT COUNT(*) AS c FROM favorites WHERE user_id = %s", (user["id"],), 0)
    rows = query_all(
        POST_SELECT_SQL
        + """ JOIN favorites f2 ON f2.post_id = p.id
              WHERE f2.user_id = %s AND p.status = 1
              ORDER BY f2.id DESC LIMIT %s OFFSET %s""",
        (user["id"], page_size, offset),
    )
    post_ids = [r["id"] for r in rows]
    images_map = fetch_images_map(post_ids)
    liked_ids = fetch_liked_ids(user, "post", post_ids)
    items = [
        post_to_dict(r, images_map.get(r["id"], []), r["id"] in liked_ids, True) for r in rows
    ]
    return ok(page_data(items, total, page, page_size))


# =============================================================================
# 13. 接口：管理员账号管理（仅**高级管理员** super_admin 可用）
#     角色三档设计：
#       user        普通用户（App 端）
#       admin       管理员（管理贴吧 / 帖子 / 评论）
#       super_admin 高级管理员（额外可新增、启用禁用、调整角色、重置密码、撤销管理员）
#     安全护栏：
#       · 不能对自己做「禁用 / 降级 / 撤销」，避免把自己锁在门外
#       · 不能把最后一个启用状态的高级管理员禁用 / 降级 / 撤销，保证后台始终有人能管
# =============================================================================
ADMIN_SELECT_SQL = """
SELECT u.id, u.username, u.nickname, u.avatar, u.role, u.status, u.created_at,
       (SELECT COUNT(*) FROM posts    p WHERE p.user_id = u.id AND p.status = 1) AS post_count,
       (SELECT COUNT(*) FROM comments c WHERE c.user_id = u.id AND c.status = 1) AS comment_count
  FROM users u
"""


def admin_to_dict(row: dict, is_self: bool = False) -> dict:
    """管理员行 → 前端结构（camelCase，与后台管理页字段一致）"""
    return {
        "id": row["id"],
        "username": row["username"],
        "nickname": row["nickname"],
        "avatar": row["avatar"],
        "role": row["role"],
        "roleText": ROLE_TEXT.get(row["role"], row["role"]),
        "status": row["status"],
        "statusText": "正常" if row["status"] == 1 else "已禁用",
        "createdAt": iso(row["created_at"]),
        "postCount": row["post_count"],
        "commentCount": row["comment_count"],
        "isSelf": is_self,  # 前端据此把「对自己」的危险操作置灰
    }


def get_admin_row_or_404(admin_id: int) -> dict:
    """取一个管理员（admin / super_admin）账号，不存在则 404"""
    row = query_one(
        ADMIN_SELECT_SQL + " WHERE u.id = %s AND u.role IN ('admin', 'super_admin')",
        (admin_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="管理员账号不存在")
    return row


def active_super_admin_count(exclude_id: Optional[int] = None) -> int:
    """还有几个「启用状态」的高级管理员（exclude_id：排除即将被操作的那个）"""
    sql = "SELECT COUNT(*) AS c FROM users WHERE role = %s AND status = 1"
    params: list = [ROLE_SUPER_ADMIN]
    if exclude_id:
        sql += " AND id <> %s"
        params.append(exclude_id)
    return int(query_value(sql, tuple(params), 0) or 0)


def assert_not_last_super_admin(target: dict) -> None:
    """禁止把最后一个可用的高级管理员禁用 / 降级 / 撤销，避免后台失去管理能力"""
    if target["role"] == ROLE_SUPER_ADMIN and active_super_admin_count(target["id"]) == 0:
        raise HTTPException(status_code=400, detail="至少需要保留一个启用状态的高级管理员")


def assert_not_self(me: dict, target: dict, action: str) -> None:
    """禁止对自己做危险操作（禁用 / 降级 / 撤销）"""
    if me["id"] == target["id"]:
        raise HTTPException(status_code=400, detail=f"不能{action}自己的账号")


@app.get("/api/admin/admins", tags=["管理员管理"], summary="管理员列表（仅高级管理员）")
def list_admins(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
    keyword: Optional[str] = Query(None, description="用户名 / 昵称模糊搜索"),
    role: Optional[str] = Query(None, description="按角色筛选：admin / super_admin"),
    status: Optional[int] = Query(None, ge=0, le=1, description="按状态筛选：1 正常 / 0 禁用"),
    me: dict = Depends(require_super_admin),
):
    where = ["u.role IN ('admin', 'super_admin')"]
    params: list = []
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        where.append("(u.username LIKE %s OR u.nickname LIKE %s)")
        params.extend([kw, kw])
    if role:
        if role not in ADMIN_ROLES:
            raise HTTPException(status_code=400, detail="角色只能是 admin 或 super_admin")
        where.append("u.role = %s")
        params.append(role)
    if status is not None:
        where.append("u.status = %s")
        params.append(status)

    where_sql = " WHERE " + " AND ".join(where)
    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(f"SELECT COUNT(*) AS c FROM users u{where_sql}", tuple(params), 0)
    rows = query_all(
        # FIELD() 让高级管理员排在前面
        ADMIN_SELECT_SQL
        + where_sql
        + " ORDER BY FIELD(u.role, 'super_admin', 'admin'), u.id ASC LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    items = [admin_to_dict(r, r["id"] == me["id"]) for r in rows]
    return ok(page_data(items, total, page, page_size))


@app.get("/api/admin/admins/{admin_id}", tags=["管理员管理"], summary="管理员详情（仅高级管理员）")
def get_admin(admin_id: int, me: dict = Depends(require_super_admin)):
    row = get_admin_row_or_404(admin_id)
    return ok(admin_to_dict(row, row["id"] == me["id"]))


@app.post("/api/admin/admins", tags=["管理员管理"], summary="新增管理员账号（仅高级管理员）")
def create_admin(body: AdminCreateIn, me: dict = Depends(require_super_admin)):
    username = body.username.strip()
    if body.role not in ADMIN_ROLES:
        raise HTTPException(status_code=400, detail="角色只能是 admin 或 super_admin")
    if query_one("SELECT id FROM users WHERE username = %s", (username,)):
        raise HTTPException(status_code=400, detail="该用户名已被占用")

    admin_id = insert_returning_id(
        """INSERT INTO users (username, password_hash, nickname, avatar, role, status)
           VALUES (%s, %s, %s, %s, %s, 1)""",
        (
            username,
            hash_password(body.password),
            (body.nickname or "").strip() or username,
            body.avatar or "🛡️",
            body.role,
        ),
    )
    row = get_admin_row_or_404(admin_id)
    return ok(admin_to_dict(row, False), message="管理员创建成功")


@app.patch("/api/admin/admins/{admin_id}/status", tags=["管理员管理"], summary="启用 / 禁用管理员（仅高级管理员）")
def update_admin_status(
    admin_id: int, body: AdminStatusIn, me: dict = Depends(require_super_admin)
):
    target = get_admin_row_or_404(admin_id)
    if body.status == 0:  # 禁用才需要护栏：不能禁用自己、不能禁用最后一个高级管理员
        assert_not_self(me, target, "禁用")
        assert_not_last_super_admin(target)
    execute("UPDATE users SET status = %s WHERE id = %s", (body.status, admin_id))
    status_text = "正常" if body.status == 1 else "已禁用"
    return ok(
        {"id": admin_id, "status": body.status, "statusText": status_text},
        message="已启用" if body.status == 1 else "已禁用",
    )


@app.patch("/api/admin/admins/{admin_id}/role", tags=["管理员管理"], summary="调整管理员角色（仅高级管理员）")
def update_admin_role(admin_id: int, body: AdminRoleIn, me: dict = Depends(require_super_admin)):
    if body.role not in ADMIN_ROLES:
        raise HTTPException(status_code=400, detail="角色只能是 admin 或 super_admin")

    target = get_admin_row_or_404(admin_id)
    if target["role"] == body.role:
        return ok(admin_to_dict(target, me["id"] == admin_id), message="角色未发生变化")

    if body.role == ROLE_ADMIN:  # 降级：不允许降自己，也不允许把最后一个高级管理员降掉
        assert_not_self(me, target, "降级")
        assert_not_last_super_admin(target)

    execute("UPDATE users SET role = %s WHERE id = %s", (body.role, admin_id))
    row = get_admin_row_or_404(admin_id)
    return ok(
        admin_to_dict(row, row["id"] == me["id"]), message=f"已调整为{ROLE_TEXT[body.role]}"
    )


@app.patch("/api/admin/admins/{admin_id}/password", tags=["管理员管理"], summary="重置管理员密码（仅高级管理员）")
def reset_admin_password(
    admin_id: int, body: AdminPasswordIn, me: dict = Depends(require_super_admin)
):
    get_admin_row_or_404(admin_id)
    execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (hash_password(body.password), admin_id),
    )
    return ok({"id": admin_id}, message="密码已重置")


@app.delete(
    "/api/admin/admins/{admin_id}",
    tags=["管理员管理"],
    summary="撤销管理员权限（降级为普通用户，仅高级管理员）",
)
def revoke_admin(admin_id: int, me: dict = Depends(require_super_admin)):
    target = get_admin_row_or_404(admin_id)
    assert_not_self(me, target, "撤销")
    assert_not_last_super_admin(target)
    execute("UPDATE users SET role = %s WHERE id = %s", (ROLE_USER, admin_id))
    return ok(
        {"id": admin_id, "role": ROLE_USER},
        message="已撤销管理员权限（该账号降级为普通用户）",
    )


# =============================================================================
# 14. 接口：评论（发表 / 分页列表 / 点赞）
# =============================================================================
COMMENT_SELECT_SQL = """
SELECT c.id, c.post_id, c.user_id, c.parent_id, c.content, c.like_count, c.created_at,
       u.nickname AS author, u.avatar AS author_avatar
  FROM comments c
  JOIN users u ON u.id = c.user_id
"""


@app.get("/api/posts/{post_id}/comments", tags=["评论"], summary="获取帖子评论列表（分页）")
def list_comments(
    post_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50, alias="pageSize"),
    user: Optional[dict] = Depends(get_optional_user),
):
    get_post_row_or_404(post_id)
    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(
        "SELECT COUNT(*) AS c FROM comments WHERE post_id = %s AND status = 1", (post_id,), 0
    )
    rows = query_all(
        COMMENT_SELECT_SQL
        + """ WHERE c.post_id = %s AND c.status = 1
              ORDER BY c.id ASC LIMIT %s OFFSET %s""",
        (post_id, page_size, offset),
    )
    liked_ids = fetch_liked_ids(user, "comment", [r["id"] for r in rows])
    items = [comment_to_dict(r, r["id"] in liked_ids) for r in rows]
    return ok(page_data(items, total, page, page_size))


@app.post("/api/posts/{post_id}/comments", tags=["评论"], summary="发表评论（评论数同步 +1）")
def create_comment(post_id: int, body: CommentIn, user: dict = Depends(get_current_user)):
    get_post_row_or_404(post_id)
    parent_id = body.parentId
    if parent_id:
        parent = query_one(
            "SELECT id FROM comments WHERE id = %s AND post_id = %s AND status = 1",
            (parent_id, post_id),
        )
        if not parent:
            raise HTTPException(status_code=400, detail="要回复的评论不存在")

    content = body.text.strip()
    if not content:
        raise HTTPException(status_code=400, detail="评论内容不能为空")

    # 写评论 + 帖子评论数 +1，放在同一个事务里，保证前端 commentCount 与实际一致
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO comments (post_id, user_id, parent_id, content, like_count)
                   VALUES (%s, %s, %s, %s, 0)""",
                (post_id, user["id"], parent_id, content),
            )
            comment_id = int(cur.lastrowid)
            cur.execute(
                "UPDATE posts SET comment_count = comment_count + 1 WHERE id = %s", (post_id,)
            )

    row = query_one(COMMENT_SELECT_SQL + " WHERE c.id = %s", (comment_id,))
    return ok(comment_to_dict(row, False), message="评论成功")


@app.post("/api/comments/{comment_id}/like", tags=["评论"], summary="评论点赞")
def like_comment(comment_id: int, user: dict = Depends(get_current_user)):
    row = query_one("SELECT id FROM comments WHERE id = %s AND status = 1", (comment_id,))
    if not row:
        raise HTTPException(status_code=404, detail="评论不存在")
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO likes (user_id, target_type, target_id) VALUES (%s, 'comment', %s)",
                (user["id"], comment_id),
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE comments SET like_count = like_count + 1 WHERE id = %s", (comment_id,)
                )
    likes = query_value("SELECT like_count AS c FROM comments WHERE id = %s", (comment_id,), 0)
    return ok({"liked": True, "likes": likes})


@app.delete("/api/comments/{comment_id}/like", tags=["评论"], summary="取消评论点赞")
def unlike_comment(comment_id: int, user: dict = Depends(get_current_user)):
    row = query_one("SELECT id FROM comments WHERE id = %s AND status = 1", (comment_id,))
    if not row:
        raise HTTPException(status_code=404, detail="评论不存在")
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM likes WHERE user_id = %s AND target_type = 'comment' AND target_id = %s",
                (user["id"], comment_id),
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE comments SET like_count = GREATEST(like_count - 1, 0) WHERE id = %s",
                    (comment_id,),
                )
    likes = query_value("SELECT like_count AS c FROM comments WHERE id = %s", (comment_id,), 0)
    return ok({"liked": False, "likes": likes})


# =============================================================================
# 15. 接口：后台管理补全（用户管理 / 贴吧编辑与删除 / 全部评论与删除评论）
#     这是给 bbs-admin-web 补的最后 7 个接口（前端契约早已按约定写好，后端补齐即生效）：
#       · 用户管理页   → GET /api/admin/users、GET /api/admin/users/{id}、PATCH .../status
#       · 贴吧板块页   → PUT /api/bars/{id}、DELETE /api/bars/{id}
#       · 评论管理页   → GET /api/admin/comments、DELETE /api/comments/{id}
#     权限：require_admin（普通管理员与高级管理员都可调用；只有「管理员账号管理」限 super_admin）
# =============================================================================

# ------------------------------- 用户管理 -------------------------------
USER_SELECT_SQL = """
SELECT u.id, u.username, u.nickname, u.avatar, u.role, u.status, u.created_at,
       (SELECT COUNT(*) FROM posts    p WHERE p.user_id = u.id AND p.status = 1) AS post_count,
       (SELECT COUNT(*) FROM comments c WHERE c.user_id = u.id AND c.status = 1) AS comment_count
  FROM users u
"""


def user_to_dict(row: dict) -> dict:
    """用户行 → 后台「用户管理」页所需字段（camelCase，与前端 src/api/user.js 契约一致）"""
    return {
        "id": row["id"],
        "username": row["username"],
        "nickname": row["nickname"],
        "avatar": row["avatar"],
        "role": row["role"],
        "roleText": ROLE_TEXT.get(row["role"], row["role"]),
        "status": row["status"],
        "statusText": "正常" if row["status"] == 1 else "已禁用",
        "createdAt": iso(row["created_at"]),
        "postCount": row["post_count"],
        "commentCount": row["comment_count"],
    }


def get_user_row_or_404(user_id: int) -> dict:
    row = query_one(USER_SELECT_SQL + " WHERE u.id = %s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    return row


@app.get("/api/admin/users", tags=["用户管理"], summary="用户列表（分页 + 关键字 / 角色 / 状态筛选）")
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
    keyword: Optional[str] = Query(None, description="用户名 / 昵称模糊搜索"),
    role: Optional[str] = Query(None, description="按角色筛选：user / admin / super_admin"),
    status: Optional[int] = Query(None, ge=0, le=1, description="按状态筛选：1 正常 / 0 禁用"),
    admin: dict = Depends(require_admin),
):
    where = ["1 = 1"]
    params: list = []
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        where.append("(u.username LIKE %s OR u.nickname LIKE %s)")
        params.extend([kw, kw])
    if role:
        if role not in (ROLE_USER, ROLE_ADMIN, ROLE_SUPER_ADMIN):
            raise HTTPException(status_code=400, detail="角色只能是 user / admin / super_admin")
        where.append("u.role = %s")
        params.append(role)
    if status is not None:
        where.append("u.status = %s")
        params.append(status)

    where_sql = " WHERE " + " AND ".join(where)
    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(f"SELECT COUNT(*) AS c FROM users u{where_sql}", tuple(params), 0)
    rows = query_all(
        USER_SELECT_SQL + where_sql + " ORDER BY u.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    return ok(page_data([user_to_dict(r) for r in rows], total, page, page_size))


@app.get("/api/admin/users/{user_id}", tags=["用户管理"], summary="用户详情")
def get_user(user_id: int, admin: dict = Depends(require_admin)):
    return ok(user_to_dict(get_user_row_or_404(user_id)))


@app.patch(
    "/api/admin/users/{user_id}/status",
    tags=["用户管理"],
    summary="启用 / 禁用用户（禁用后该账号无法登录 App）",
)
def update_user_status(user_id: int, body: AdminStatusIn, admin: dict = Depends(require_admin)):
    get_user_row_or_404(user_id)
    if body.status == 0 and admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能禁用自己的账号")
    execute("UPDATE users SET status = %s WHERE id = %s", (body.status, user_id))
    status_text = "正常" if body.status == 1 else "已禁用"
    return ok(
        {"id": user_id, "status": body.status, "statusText": status_text},
        message="已启用" if body.status == 1 else "已禁用（该账号将无法登录 App）",
    )


# --------------------------- 贴吧编辑 / 删除 ---------------------------
@app.put("/api/bars/{bar_id}", tags=["贴吧"], summary="编辑贴吧（管理员）")
def update_bar(bar_id: int, body: BarIn, admin: dict = Depends(require_admin)):
    if not query_one("SELECT id FROM bars WHERE id = %s", (bar_id,)):
        raise HTTPException(status_code=404, detail="贴吧不存在")

    name = body.name.strip()
    # 吧名唯一：排除自己再查重
    if query_one("SELECT id FROM bars WHERE name = %s AND id <> %s", (name, bar_id)):
        raise HTTPException(status_code=400, detail="已存在同名贴吧")

    execute(
        """UPDATE bars SET name = %s, icon = %s, image = %s, intro = %s, owner = %s, sort = %s
            WHERE id = %s""",
        (name, body.icon, body.image, body.intro, body.owner, body.sort, bar_id),
    )
    updated = query_one(BAR_SELECT_SQL + " WHERE b.id = %s", (bar_id,))
    return ok(
        bar_to_dict(updated, False, updated["post_count"], updated["follow_count"]),
        message="保存成功",
    )


@app.delete(
    "/api/bars/{bar_id}",
    tags=["贴吧"],
    summary="删除贴吧（管理员；吧内帖子/关注/收藏随外键级联删除）",
)
def delete_bar(bar_id: int, admin: dict = Depends(require_admin)):
    if not query_one("SELECT id FROM bars WHERE id = %s", (bar_id,)):
        raise HTTPException(status_code=404, detail="贴吧不存在")

    post_count = query_value("SELECT COUNT(*) AS c FROM posts WHERE bar_id = %s", (bar_id,), 0)
    # bars 的外键都是 ON DELETE CASCADE：吧内帖子、帖子图片、评论、点赞、收藏、关注会一并删除
    # （前端「删除贴吧」用的是红色二次确认，已经明确提示过这一点）
    execute("DELETE FROM bars WHERE id = %s", (bar_id,))
    return ok(
        {"id": bar_id, "deletedPosts": post_count},
        message=f"已删除该贴吧（同时删除 {post_count} 篇帖子）",
    )


# --------------------- 全部评论（跨帖） / 删除评论 ---------------------
ADMIN_COMMENT_SELECT_SQL = """
SELECT c.id, c.post_id, c.user_id, c.parent_id, c.content, c.like_count, c.status, c.created_at,
       u.nickname AS author, u.avatar AS author_avatar,
       p.title     AS post_title
  FROM comments c
  JOIN users u ON u.id = c.user_id
  JOIN posts p ON p.id = c.post_id
"""


def admin_comment_to_dict(row: dict) -> dict:
    """评论行 → 后台「评论管理」页所需字段（含所属帖子标题）"""
    return {
        "id": row["id"],
        "postId": row["post_id"],
        "postTitle": row["post_title"],
        "author": row["author"],
        "authorId": row["user_id"],
        "authorAvatar": row["author_avatar"],
        "text": row["content"],
        "likes": row["like_count"],
        "status": row["status"],
        "time": fmt_time(row["created_at"]),
        "createdAt": iso(row["created_at"]),
        "parentId": row["parent_id"],
    }


@app.get("/api/admin/comments", tags=["评论管理"], summary="全部评论（分页 + 关键字 / 帖子筛选）")
def list_all_comments(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50, alias="pageSize"),
    keyword: Optional[str] = Query(None, description="评论内容 / 作者模糊搜索"),
    post_id: Optional[int] = Query(None, alias="postId", description="只看某个帖子的评论"),
    status: Optional[int] = Query(None, ge=0, le=1, description="1 正常 / 0 已删除"),
    admin: dict = Depends(require_admin),
):
    where = ["1 = 1"]
    params: list = []
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        where.append("(c.content LIKE %s OR u.nickname LIKE %s)")
        params.extend([kw, kw])
    if post_id:
        where.append("c.post_id = %s")
        params.append(post_id)
    if status is not None:
        where.append("c.status = %s")
        params.append(status)

    where_sql = " WHERE " + " AND ".join(where)
    page, page_size, offset = normalize_page(page, page_size)
    total = query_value(
        f"""SELECT COUNT(*) AS c FROM comments c
              JOIN users u ON u.id = c.user_id
              JOIN posts p ON p.id = c.post_id{where_sql}""",
        tuple(params),
        0,
    )
    rows = query_all(
        ADMIN_COMMENT_SELECT_SQL + where_sql + " ORDER BY c.id DESC LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    return ok(page_data([admin_comment_to_dict(r) for r in rows], total, page, page_size))


@app.delete(
    "/api/comments/{comment_id}",
    tags=["评论管理"],
    summary="删除违规评论（软删除，同时把该帖评论数 -1）",
)
def delete_comment(comment_id: int, admin: dict = Depends(require_admin)):
    row = query_one("SELECT id, post_id, status FROM comments WHERE id = %s", (comment_id,))
    if not row:
        raise HTTPException(status_code=404, detail="评论不存在")
    if row["status"] == 0:
        return ok({"id": comment_id, "status": 0}, message="该评论已是删除状态")

    # 软删除（保留数据便于追溯）+ 同步帖子评论数，放同一事务
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE comments SET status = 0 WHERE id = %s", (comment_id,))
            cur.execute(
                "UPDATE posts SET comment_count = GREATEST(comment_count - 1, 0) WHERE id = %s",
                (row["post_id"],),
            )
    return ok({"id": comment_id, "status": 0}, message="已删除该评论")


# =============================================================================
# 16. 启动入口
#     本地开发：python main.py（自动热重载，端口取 PORT 或 8000）
#     云端部署（Render）：Start Command 用
#         uvicorn main:app --host 0.0.0.0 --port $PORT
#     平台会注入 PORT 环境变量，代码里也读 PORT，两边保持一致即可。
# =============================================================================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=not IS_PRODUCTION)










