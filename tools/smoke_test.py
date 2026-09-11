# -*- coding: utf-8 -*-
"""
后端冒烟测试（只用标准库，不需要 pytest / requests）
================================================================================
用法：在 bbs-app-backend 目录下执行
    .venv\\Scripts\\python.exe tools\\smoke_test.py

它会做三件事：
  1. import main，校验应用能装配、26 个路由是否齐全、OpenAPI 能否生成
  2. 在 8010 端口用后台线程起一个临时 uvicorn（不影响你的 8000 端口）
  3. 请求接口：
     - 数据库可用：完整跑一遍 注册 → 登录 → 贴吧列表 → 吧内帖子 → 发帖(带图) →
       帖子详情 → 点赞/取消 → 收藏/取消 → 评论 → 评论点赞 → 关注吧/取关
     - 数据库不可用：只校验「服务可启动 + 统一错误响应 + CORS 响应头」，并给出提示

退出码 0 表示通过，1 表示有失败项。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import uvicorn  # noqa: E402

import main as app_module  # noqa: E402

PORT = 8010
BASE_URL = f"http://127.0.0.1:{PORT}"

# 期望存在的路由（与 README 的接口清单一致）
EXPECTED_PATHS = {
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/me",
    "/api/bars",
    "/api/bars/{bar_id}",
    "/api/bars/{bar_id}/posts",
    "/api/bars/{bar_id}/follow",
    "/api/users/me/followed-bars",
    "/api/users/me/posts",
    "/api/users/me/favorites",
    "/api/posts",
    "/api/posts/{post_id}",
    "/api/posts/{post_id}/like",
    "/api/posts/{post_id}/favorite",
    "/api/posts/{post_id}/comments",
    "/api/comments/{comment_id}/like",
    "/api/search",
    # 管理员账号管理（仅高级管理员）
    "/api/admin/admins",
    "/api/admin/admins/{admin_id}",
    "/api/admin/admins/{admin_id}/status",
    "/api/admin/admins/{admin_id}/role",
    "/api/admin/admins/{admin_id}/password",
    # 后台管理补全：用户管理 / 全部评论
    "/api/admin/users",
    "/api/admin/users/{user_id}",
    "/api/admin/users/{user_id}/status",
    "/api/admin/comments",
    "/api/comments/{comment_id}",
    # 数据收口：足迹 / 转发 / 个人资料 / 互动消息
    "/api/bars/{bar_id}/visit",
    "/api/users/me/footprints",
    "/api/posts/{post_id}/forward",
    "/api/users/me",
    "/api/users/me/notifications",
    "/api/users/me/notifications/unread",
    "/api/users/me/notifications/read",
}

FAILED = []
PASSED = []


def check(name: str, condition: bool, extra: str = "") -> None:
    """记录一条断言结果"""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {extra}".strip())
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------------------
# HTTP 小工具（标准库 urllib，避免引入 requests）
# ---------------------------------------------------------------------------
def request(method: str, path: str, body=None, token=None, raw: bytes = None,
            content_type: str = None, extra_headers: dict = None):
    """返回 (status_code, 解析后的 json 或 None, 响应头 dict)"""
    url = BASE_URL + path
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, (json.loads(payload) if payload else None), dict(resp.headers)
    except urllib.error.HTTPError as err:
        payload = err.read().decode("utf-8")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw": payload}
        return err.code, parsed, dict(err.headers)


def build_multipart(fields: dict, files: list) -> tuple[bytes, str]:
    """构造 multipart/form-data 请求体（fields: 普通字段, files: [(字段名, 文件名, 内容)])"""
    boundary = "----BbsSmokeBoundary" + uuid.uuid4().hex[:8]
    buf = bytearray()
    for key, value in fields.items():
        buf += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
    for field, filename, content in files:
        buf += (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


# 1x1 的合法 PNG，用于测试图片上传
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# 业务闭环测试（需要数据库可用）
# ---------------------------------------------------------------------------
def run_api_checks() -> None:
    suffix = uuid.uuid4().hex[:6]
    username = f"smoke_{suffix}"
    password = "smoke123456"

    # --- 注册 / 登录 / 鉴权 ---
    status, res, _ = request(
        "POST",
        "/api/auth/register",
        body={"username": username, "password": password, "nickname": f"冒烟{suffix}"},
    )
    check("注册成功并返回 token", status == 200 and res.get("code") == 0 and res["data"].get("token"), str(res))
    check(
        "注册后头像是图片地址（不再是 emoji）",
        ((res.get("data") or {}).get("user") or {}).get("avatar") == app_module.DEFAULT_AVATAR,
        str(((res.get("data") or {}).get("user")))[:200],
    )

    status, res, _ = request("POST", "/api/auth/login", body={"username": username, "password": password})
    check("登录成功并返回 token", status == 200 and res.get("code") == 0 and res["data"].get("token"), str(res))
    token = (res.get("data") or {}).get("token")

    status, res, _ = request("GET", "/api/auth/me", token=token)
    check("带 token 可以获取当前用户", status == 200 and res["data"].get("username") == username, str(res))

    status, res, _ = request("POST", "/api/auth/login", body={"username": username, "password": "wrong-password"})
    check("密码错误返回 401", status == 401 and res.get("code") == 401, str(res))

    # --- 贴吧列表 / 吧内帖子 ---
    status, res, _ = request("GET", "/api/bars", token=token)
    bars = (res or {}).get("data") or []
    check(
        "贴吧列表返回数组且字段齐全",
        status == 200 and bool(bars) and {"id", "name", "icon", "followed"} <= set(bars[0]),
        str(res)[:200],
    )
    if not bars:
        return
    bar_id = bars[0]["id"]

    status, res, _ = request("GET", f"/api/bars/{bar_id}/posts?page=1&pageSize=5", token=token)
    data = (res or {}).get("data") or {}
    check(
        "吧内帖子分页返回 bar + list + hasMore",
        status == 200 and {"bar", "list", "hasMore"} <= set(data),
        str(res)[:200],
    )

    # --- 关注贴吧 / 取关 ---
    status, res, _ = request("POST", f"/api/bars/{bar_id}/follow", token=token)
    check("关注贴吧成功（followed=true）", status == 200 and res["data"].get("followed") is True, str(res))
    status, res, _ = request("GET", "/api/users/me/followed-bars", token=token)
    check("我关注的吧包含刚关注的吧", status == 200 and any(b["id"] == bar_id for b in res["data"]), str(res)[:200])
    status, res, _ = request("DELETE", f"/api/bars/{bar_id}/follow", token=token)
    check("取消关注成功（followed=false）", status == 200 and res["data"].get("followed") is False, str(res))

    # --- 发布带 2 张图的帖子（multipart 上传）---
    raw, ctype = build_multipart(
        {"barId": bar_id, "title": f"冒烟测试帖 {suffix}", "content": "由 smoke_test.py 自动发布", "tag": "技术交流"},
        [("files", "a.png", TINY_PNG), ("files", "b.png", TINY_PNG)],
    )
    status, res, _ = request("POST", "/api/posts", raw=raw, content_type=ctype, token=token)
    post = (res or {}).get("data") or {}
    check(
        "发帖成功且返回 2 张图片",
        status == 200 and res.get("code") == 0 and len(post.get("images", [])) == 2,
        str(res)[:300],
    )
    check(
        "图片返回 /uploads/ 地址",
        bool(post.get("images")) and all(str(u).startswith("/uploads/posts/") for u in post["images"]),
        str(post.get("images")),
    )
    post_id = post.get("id")
    if not post_id:
        return

    status, res, _ = request("GET", f"/api/posts/{post_id}", token=token)
    check(
        "帖子详情可读且 liked / favorited 为 false",
        status == 200 and res["data"]["liked"] is False and res["data"]["favorited"] is False,
        str(res)[:200],
    )
    base_likes = res["data"]["likes"]

    # --- 帖子点赞 / 取消（含重复点赞幂等）---
    status, res, _ = request("POST", f"/api/posts/{post_id}/like", token=token)
    check(
        "帖子点赞成功且计数 +1",
        status == 200 and res["data"]["liked"] is True and res["data"]["likes"] == base_likes + 1,
        str(res),
    )
    status, res, _ = request("POST", f"/api/posts/{post_id}/like", token=token)
    check("重复点赞不会重复计数", status == 200 and res["data"]["likes"] == base_likes + 1, str(res))
    status, res, _ = request("DELETE", f"/api/posts/{post_id}/like", token=token)
    check(
        "取消点赞后计数还原",
        status == 200 and res["data"]["liked"] is False and res["data"]["likes"] == base_likes,
        str(res),
    )

    # --- 收藏 / 取消收藏 ---
    status, res, _ = request("POST", f"/api/posts/{post_id}/favorite", token=token)
    check("收藏成功", status == 200 and res["data"]["favorited"] is True, str(res))
    status, res, _ = request("GET", "/api/users/me/favorites", token=token)
    check(
        "我的收藏包含刚收藏的帖子",
        status == 200 and any(p["id"] == post_id for p in res["data"]["list"]),
        str(res)[:200],
    )
    status, res, _ = request("DELETE", f"/api/posts/{post_id}/favorite", token=token)
    check("取消收藏成功", status == 200 and res["data"]["favorited"] is False, str(res))

    # --- 评论 / 评论点赞 ---
    status, res, _ = request("POST", f"/api/posts/{post_id}/comments", body={"text": "冒烟测试评论"}, token=token)
    check("发表评论成功", status == 200 and res["data"]["text"] == "冒烟测试评论", str(res)[:200])
    comment_id = (res.get("data") or {}).get("id")

    status, res, _ = request("GET", f"/api/posts/{post_id}/comments", token=token)
    check("评论列表可分页查询", status == 200 and res["data"]["total"] >= 1, str(res)[:200])

    status, res, _ = request("GET", f"/api/posts/{post_id}")
    check("评论后帖子 commentCount 已同步", status == 200 and res["data"]["commentCount"] >= 1, str(res)[:200])

    if comment_id:
        status, res, _ = request("POST", f"/api/comments/{comment_id}/like", token=token)
        check("评论点赞成功", status == 200 and res["data"]["likes"] == 1, str(res))
        status, res, _ = request("DELETE", f"/api/comments/{comment_id}/like", token=token)
        check("取消评论点赞成功", status == 200 and res["data"]["likes"] == 0, str(res))

    # --- 搜索 ---
    status, res, _ = request("GET", f"/api/search?keyword={suffix}")
    check("搜索能命中刚发的帖子", status == 200 and res["data"]["total"] >= 1, str(res)[:200])

    # --- 删除自己发的帖子（软删除）---
    status, res, _ = request("DELETE", f"/api/posts/{post_id}", token=token)
    check("作者可以删除自己的帖子", status == 200 and res.get("code") == 0, str(res))
    status, res, _ = request("GET", f"/api/posts/{post_id}")
    check("已删除帖子返回 404", status == 404 and res.get("code") == 404, str(res))


# ---------------------------------------------------------------------------
# 高级管理员（super_admin）与「管理员账号管理」闭环测试
#   覆盖：角色权限隔离、新增管理员、改角色、启用/禁用、重置密码、撤销管理员，
#         以及「不能对自己做危险操作」的安全护栏
# ---------------------------------------------------------------------------
SUPER_ADMIN_USERNAME = "superadmin"
SUPER_ADMIN_PASSWORD = "super123456"


def login_token(username: str, password: str):
    """登录并返回 (状态码, 响应体, token)"""
    status, res, _ = request(
        "POST", "/api/auth/login", body={"username": username, "password": password}
    )
    token = ((res or {}).get("data") or {}).get("token")
    return status, res, token


def run_super_admin_checks() -> None:
    # ---------- 1) 高级管理员登录 ----------
    status, res, super_token = login_token(SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)
    check("高级管理员 superadmin 登录成功", status == 200 and bool(super_token), str(res)[:200])
    check(
        "superadmin 的 role 为 super_admin",
        ((res or {}).get("data") or {}).get("user", {}).get("role") == "super_admin",
        str(res)[:200],
    )
    if not super_token:
        return

    # ---------- 2) 权限隔离：普通管理员无权重管理员账号 ----------
    status, res, admin_token = login_token("admin", "admin123456")
    check("普通管理员 admin 登录成功", status == 200 and bool(admin_token), str(res)[:200])
    status, res, _ = request("GET", "/api/admin/admins", token=admin_token)
    check("普通管理员查管理员列表被拒（403）", status == 403 and res.get("code") == 403, str(res)[:200])
    status, res, _ = request(
        "POST",
        "/api/admin/admins",
        body={"username": "should_fail_admin", "password": "abc123456", "role": "admin"},
        token=admin_token,
    )
    check("普通管理员新增管理员被拒（403）", status == 403, str(res)[:200])

    # ---------- 3) 管理员列表 ----------
    status, res, _ = request("GET", "/api/admin/admins?page=1&pageSize=50", token=super_token)
    data = (res or {}).get("data") or {}
    names = {item["username"] for item in data.get("list", [])}
    first = (data.get("list") or [{}])[0]
    check(
        "管理员列表包含 superadmin 与 admin",
        status == 200 and {"superadmin", "admin"} <= names,
        str(res)[:200],
    )
    check("列表项含 roleText / isSelf 字段", "roleText" in first and "isSelf" in first, str(first)[:200])
    check("高级管理员排在列表最前面", first.get("role") == "super_admin", str(first)[:200])

    # ---------- 4) 新增管理员 ----------
    new_username = f"smoke_admin_{uuid.uuid4().hex[:6]}"
    status, res, _ = request(
        "POST",
        "/api/admin/admins",
        body={
            "username": new_username,
            "password": "smoke123456",
            "nickname": "冒烟管理员",
            "avatar": app_module.DEFAULT_AVATAR,
            "role": "admin",
        },
        token=super_token,
    )
    new_id = ((res or {}).get("data") or {}).get("id")
    check("新增管理员成功", status == 200 and res.get("code") == 0 and bool(new_id), str(res)[:200])

    status, res, _ = request(
        "POST",
        "/api/admin/admins",
        body={"username": new_username, "password": "smoke123456", "role": "admin"},
        token=super_token,
    )
    check("用户名重复时新增失败（400）", status == 400, str(res)[:200])

    status, res, _ = request(
        "POST",
        "/api/admin/admins",
        body={"username": f"bad_role_{uuid.uuid4().hex[:4]}", "password": "abc123456", "role": "root"},
        token=super_token,
    )
    check("非法角色被拒（400）", status == 400, str(res)[:200])

    if not new_id:
        return

    # ---------- 5) 新管理员是普通管理员：能登录，但无权管理管理员 ----------
    status, res, new_token = login_token(new_username, "smoke123456")
    check("新管理员可以登录后台", status == 200 and bool(new_token), str(res)[:200])
    status, res, _ = request("GET", "/api/admin/admins", token=new_token)
    check("新管理员（普通）访问管理员列表被拒（403）", status == 403, str(res)[:200])

    # ---------- 6) 提升为高级管理员后即可管理管理员 ----------
    status, res, _ = request(
        "PATCH", f"/api/admin/admins/{new_id}/role", body={"role": "super_admin"}, token=super_token
    )
    check("提升为高级管理员成功", status == 200 and res["data"].get("role") == "super_admin", str(res)[:200])
    status, res, _ = request("GET", "/api/admin/admins", token=new_token)
    check("提升后可以访问管理员列表", status == 200, str(res)[:200])

    # ---------- 7) 降级回普通管理员 ----------
    status, res, _ = request(
        "PATCH", f"/api/admin/admins/{new_id}/role", body={"role": "admin"}, token=super_token
    )
    check("降级为管理员成功", status == 200 and res["data"].get("role") == "admin", str(res)[:200])

    # ---------- 8) 安全护栏：不能对自己做危险操作 ----------
    status, res, _ = request("GET", "/api/auth/me", token=super_token)
    my_id = (res or {}).get("data", {}).get("id")
    status, res, _ = request(
        "PATCH", f"/api/admin/admins/{my_id}/status", body={"status": 0}, token=super_token
    )
    check("不能禁用自己（400）", status == 400, str(res)[:200])
    status, res, _ = request(
        "PATCH", f"/api/admin/admins/{my_id}/role", body={"role": "admin"}, token=super_token
    )
    check("不能降级自己（400）", status == 400, str(res)[:200])
    status, res, _ = request("DELETE", f"/api/admin/admins/{my_id}", token=super_token)
    check("不能撤销自己的管理员权限（400）", status == 400, str(res)[:200])

    # ---------- 9) 启用 / 禁用 ----------
    status, res, _ = request(
        "PATCH", f"/api/admin/admins/{new_id}/status", body={"status": 0}, token=super_token
    )
    check("禁用管理员成功", status == 200 and res["data"].get("status") == 0, str(res)[:200])
    status, res, _ = request(
        "POST", "/api/auth/login", body={"username": new_username, "password": "smoke123456"}
    )
    check("被禁用的管理员无法登录（403）", status == 403, str(res)[:200])
    status, res, _ = request(
        "PATCH", f"/api/admin/admins/{new_id}/status", body={"status": 1}, token=super_token
    )
    check("重新启用管理员成功", status == 200 and res["data"].get("status") == 1, str(res)[:200])

    # ---------- 10) 重置密码 ----------
    status, res, _ = request(
        "PATCH",
        f"/api/admin/admins/{new_id}/password",
        body={"password": "reset123456"},
        token=super_token,
    )
    check("重置管理员密码成功", status == 200 and res.get("code") == 0, str(res)[:200])
    status, res, _ = request(
        "POST", "/api/auth/login", body={"username": new_username, "password": "reset123456"}
    )
    check(
        "用新密码可以登录",
        status == 200 and bool(((res or {}).get("data") or {}).get("token")),
        str(res)[:200],
    )
    status, res, _ = request(
        "POST", "/api/auth/login", body={"username": new_username, "password": "smoke123456"}
    )
    check("旧密码已失效（401）", status == 401, str(res)[:200])

    # ---------- 11) 撤销管理员权限 ----------
    status, res, _ = request("DELETE", f"/api/admin/admins/{new_id}", token=super_token)
    check(
        "撤销管理员权限成功（降级为普通用户）",
        status == 200 and res["data"].get("role") == "user",
        str(res)[:200],
    )
    status, res, _ = request("GET", f"/api/admin/admins/{new_id}", token=super_token)
    check("被撤销后不再是管理员（详情 404）", status == 404, str(res)[:200])
    status, res, _ = request("GET", "/api/admin/admins", token=new_token)
    check("被撤销的账号无法再访问管理员接口（403）", status == 403, str(res)[:200])

    # ---------- 12) 撤销后账号依然存在（只是降级为普通用户）----------
    status, res, _ = request(
        "POST", "/api/auth/login", body={"username": new_username, "password": "reset123456"}
    )
    check(
        "被撤销的账号仍是可登录的普通用户",
        status == 200
        and ((res or {}).get("data") or {}).get("user", {}).get("role") == "user",
        str(res)[:200],
    )


# ---------------------------------------------------------------------------
# 后台管理补全接口测试（用户管理 / 贴吧编辑与删除 / 全部评论与删除评论）
#   原则：自建数据、自清理，不修改演示账号、演示帖子与演示贴吧
# ---------------------------------------------------------------------------
def run_admin_backfill_checks() -> None:
    status, res, admin_token = login_token("admin", "admin123456")
    check("管理员 admin 登录成功（后台补全用例）", status == 200 and bool(admin_token), str(res)[:200])
    if not admin_token:
        return
    status, res, user_token = login_token("demo", "demo123456")
    check("普通用户 demo 登录成功（用于权限对比）", status == 200 and bool(user_token), str(res)[:200])
    status, res, super_token = login_token(SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)

    # ---------- 1) 用户管理 ----------
    status, res, _ = request("GET", "/api/admin/users", token=user_token)
    check("普通用户访问用户管理被拒（403）", status == 403, str(res)[:200])

    status, res, _ = request("GET", "/api/admin/users?page=1&pageSize=50", token=admin_token)
    data = (res or {}).get("data") or {}
    names = {item["username"] for item in data.get("list", [])}
    first = (data.get("list") or [{}])[0]
    check(
        "用户列表返回分页结构且含演示账号",
        status == 200 and {"superadmin", "admin", "demo"} <= names,
        str(res)[:200],
    )
    check(
        "用户项含 roleText / statusText / postCount",
        {"roleText", "statusText", "postCount", "commentCount"} <= set(first),
        str(first)[:200],
    )

    status, res, _ = request("GET", "/api/admin/users?keyword=demo", token=admin_token)
    check("按关键字筛选用户可用", status == 200 and any(u["username"] == "demo" for u in res["data"]["list"]), str(res)[:200])
    status, res, _ = request("GET", "/api/admin/users?role=super_admin", token=admin_token)
    check("按角色筛选用户可用", status == 200 and all(u["role"] == "super_admin" for u in res["data"]["list"]), str(res)[:200])
    status, res, _ = request("GET", "/api/admin/users?role=boss", token=admin_token)
    check("非法角色被拒（400）", status == 400, str(res)[:200])

    demo_id = next((u["id"] for u in data.get("list", []) if u["username"] == "demo"), None)
    status, res, _ = request("GET", f"/api/admin/users/{demo_id}", token=admin_token)
    check("用户详情可读", status == 200 and res["data"]["username"] == "demo", str(res)[:200])

    # 禁用 / 启用：注册一个临时用户来测（不碰演示账号）
    tmp_username = f"perm_{uuid.uuid4().hex[:6]}"
    status, res, _ = request(
        "POST",
        "/api/auth/register",
        body={"username": tmp_username, "password": "tmp123456", "nickname": "禁用测试"},
    )
    tmp_id = ((res or {}).get("data") or {}).get("user", {}).get("id")
    check("注册临时用户成功", status == 200 and bool(tmp_id), str(res)[:200])

    status, res, _ = request("PATCH", f"/api/admin/users/{tmp_id}/status", body={"status": 0}, token=admin_token)
    check("禁用用户成功", status == 200 and res["data"]["status"] == 0, str(res)[:200])
    status, res, _ = request("POST", "/api/auth/login", body={"username": tmp_username, "password": "tmp123456"})
    check("被禁用用户无法登录（403）", status == 403, str(res)[:200])
    status, res, _ = request("PATCH", f"/api/admin/users/{tmp_id}/status", body={"status": 1}, token=admin_token)
    check("重新启用用户成功", status == 200 and res["data"]["status"] == 1, str(res)[:200])
    status, res, _ = request("POST", "/api/auth/login", body={"username": tmp_username, "password": "tmp123456"})
    check("启用后可正常登录", status == 200, str(res)[:200])

    status, res, _ = request("GET", "/api/auth/me", token=admin_token)
    my_id = (res or {}).get("data", {}).get("id")
    status, res, _ = request("PATCH", f"/api/admin/users/{my_id}/status", body={"status": 0}, token=admin_token)
    check("不能禁用自己的账号（400）", status == 400, str(res)[:200])

    # ---------- 2) 贴吧编辑 / 删除（自建吧，测完删掉） ----------
    tmp_bar_name = f"冒烟吧{uuid.uuid4().hex[:4]}"
    status, res, _ = request(
        "POST",
        "/api/bars",
        body={"name": tmp_bar_name, "icon": "🧪", "intro": "临时吧", "owner": "冒烟", "sort": 999},
        token=super_token,
    )
    tmp_bar_id = ((res or {}).get("data") or {}).get("id")
    check("新建临时贴吧成功", status == 200 and bool(tmp_bar_id), str(res)[:200])

    status, res, _ = request(
        "PUT",
        f"/api/bars/{tmp_bar_id}",
        body={
            "name": f"{tmp_bar_name}改",
            "icon": "🧪",
            "image": None,
            "intro": "简介已改",
            "owner": "冒烟",
            "sort": 998,
        },
        token=admin_token,
    )
    check("编辑贴吧成功（简介已更新）", status == 200 and res["data"].get("desc") == "简介已改", str(res)[:200])

    status, res, _ = request(
        "PUT",
        f"/api/bars/{tmp_bar_id}",
        body={"name": "前端吧", "icon": "💻", "image": None, "intro": "x", "owner": "y", "sort": 1},
        token=admin_token,
    )
    check("改成已存在的吧名被拒（400）", status == 400, str(res)[:200])

    status, res, _ = request("DELETE", f"/api/bars/{tmp_bar_id}", token=admin_token)
    check("删除贴吧成功", status == 200, str(res)[:200])
    status, res, _ = request("GET", f"/api/bars/{tmp_bar_id}", token=admin_token)
    check("删除后该贴吧不可访问（404）", status == 404, str(res)[:200])

    # ---------- 3) 全部评论 / 删除评论（自建评论，测完删掉） ----------
    status, res, _ = request("GET", "/api/admin/comments?page=1&pageSize=20", token=user_token)
    check("普通用户访问全部评论被拒（403）", status == 403, str(res)[:200])

    status, res, _ = request("GET", "/api/admin/comments?page=1&pageSize=20", token=admin_token)
    cdata = (res or {}).get("data") or {}
    cfirst = (cdata.get("list") or [{}])[0]
    check(
        "全部评论列表可读且含所属帖子标题",
        status == 200 and cdata.get("total", 0) >= 1 and {"postTitle", "text", "author"} <= set(cfirst),
        str(res)[:200],
    )

    status, res, _ = request("GET", "/api/posts?page=1&pageSize=1")
    target = (res or {}).get("data", {}).get("list", [{}])[0]
    post_id, before_count = target.get("id"), target.get("commentCount")

    status, res, _ = request(
        "POST", f"/api/posts/{post_id}/comments", body={"text": "冒烟删除用评论"}, token=user_token
    )
    tmp_comment_id = ((res or {}).get("data") or {}).get("id")
    check("临时评论创建成功", status == 200 and bool(tmp_comment_id), str(res)[:200])

    status, res, _ = request("GET", f"/api/posts/{post_id}")
    check(
        "发表后帖子评论数 +1",
        status == 200 and res["data"]["commentCount"] == before_count + 1,
        str(res)[:200],
    )

    status, res, _ = request("DELETE", f"/api/comments/{tmp_comment_id}", token=admin_token)
    check("删除评论成功（软删除 status=0）", status == 200 and res["data"]["status"] == 0, str(res)[:200])

    status, res, _ = request("GET", f"/api/posts/{post_id}")
    check("删除后帖子评论数复原", status == 200 and res["data"]["commentCount"] == before_count, str(res)[:200])

    status, res, _ = request("DELETE", f"/api/comments/{tmp_comment_id}", token=admin_token)
    check("重复删除幂等（仍返回成功）", status == 200, str(res)[:200])


# ---------------------------------------------------------------------------
# 数据收口测试（足迹 / 转发 / 改资料 / 互动消息 / 启动自动补演示数据）
#   · 足迹、资料、消息都用自己的临时数据测，不改动演示账号的核心信息
#   · 演示数据补全由服务启动时的自检完成，这里断言"确实补进去了"
# ---------------------------------------------------------------------------
def run_data_centralization_checks() -> None:
    status, res, admin_token = login_token("admin", "admin123456")
    status, res, demo_token = login_token("demo", "demo123456")
    check("演示账号可登录（数据收口用例）", bool(admin_token) and bool(demo_token), str(res)[:200])
    if not admin_token or not demo_token:
        return

    # ---------- 0) 启动自检：新表已建 + 前端本地演示数据已补进数据库 ----------
    status, res, _ = request("GET", "/api/bars")
    bars = (res or {}).get("data") or []
    check(
        "16 个吧，且吧图已由数据库提供",
        status == 200
        and len(bars) == 16
        and all(str(b.get("img") or "").startswith("/static/images/bars/") for b in bars),
        f"{len(bars)} 个吧，示例 img={bars[0].get('img') if bars else None}",
    )

    status, res, _ = request("GET", "/api/posts?page=1&pageSize=50")
    posts = ((res or {}).get("data") or {}).get("list") or []
    extra_title = "整理了 50 个前端面试高频考点，需要的自取"
    extra_post = next((p for p in posts if p["title"] == extra_title), None)
    check(
        "前端本地那 3 篇帖子已补进数据库",
        status == 200 and len(posts) >= 8 and extra_post is not None,
        f"共 {len(posts)} 篇",
    )
    check(
        "补进来的帖子带图与评论（作者账号也建好了）",
        bool(extra_post)
        and len(extra_post.get("images") or []) == 1
        and extra_post.get("commentCount") == 1
        and extra_post.get("author") == "代码搬运工",
        str(extra_post)[:200],
    )

    # ---------- 1) 足迹 ----------
    status, res, _ = request("GET", "/api/users/me/footprints")
    check("未登录访问足迹 → 401", status == 401, str(res)[:200])

    status, res, _ = request("POST", "/api/bars/1/visit", token=demo_token)
    check("记录足迹成功", status == 200, str(res)[:200])
    request("POST", "/api/bars/3/visit", token=demo_token)

    status, res, _ = request("GET", "/api/users/me/footprints", token=demo_token)
    data = (res or {}).get("data") or []
    check(
        "足迹按最近浏览倒序、字段齐全",
        status == 200
        and [f["barId"] for f in data] == [3, 1]
        and data[0]["name"] == "游戏吧"
        and {"badge", "img", "icon", "viewedAt"} <= set(data[0]),
        str(data)[:200],
    )

    request("POST", "/api/bars/1/visit", token=demo_token)
    status, res, _ = request("GET", "/api/users/me/footprints", token=demo_token)
    data = (res or {}).get("data") or []
    check("重复浏览不产生重复足迹（最近浏览排最前）", len(data) == 2 and data[0]["barId"] == 1, str(data)[:200])
    check("刚浏览过的吧角标为 0", all(f["badge"] == 0 for f in data), str(data)[:200])

    # 足迹角标：浏览之后该吧新增的帖子要计入角标
    time.sleep(1.2)  # 帖子时间是秒级精度，等 1.2 秒确保新帖时间晚于刚才的浏览时间
    raw, ctype = build_multipart(
        {
            "barId": 1,
            "title": f"足迹角标测试帖 {uuid.uuid4().hex[:6]}",
            "content": "验证足迹角标",
            "tag": "闲聊",
        },
        [],
    )
    status, res, _ = request("POST", "/api/posts", raw=raw, content_type=ctype, token=admin_token)
    check("管理员在吧 1 发帖成功（角标用例）", status == 200, str(res)[:200])
    status, res, _ = request("GET", "/api/users/me/footprints", token=demo_token)
    badge_map = {f["barId"]: f["badge"] for f in ((res or {}).get("data") or [])}
    check("浏览后该吧新增的帖子计入角标", badge_map.get(1) == 1, str(badge_map))

    # ---------- 2) 转发 ----------
    status, res, _ = request("POST", "/api/posts/4/forward")
    check("未登录转发 → 401", status == 401, str(res)[:200])
    status, res, _ = request("POST", "/api/posts/999999/forward", token=demo_token)
    check("转发不存在的帖子 → 404", status == 404, str(res)[:200])
    status, res, _ = request("GET", "/api/posts/4")
    before_forwards = (res or {}).get("data", {}).get("forwards", 0)
    status, res, _ = request("POST", "/api/posts/4/forward", token=demo_token)
    check(
        "转发成功且计数 +1",
        status == 200 and (res.get("data") or {}).get("forwards") == before_forwards + 1,
        str(res)[:200],
    )

    # ---------- 3) 修改我的资料（用临时用户，不动演示账号） ----------
    suffix = uuid.uuid4().hex[:6]
    tmp_username = f"profile_{suffix}"
    status, res, _ = request(
        "POST",
        "/api/auth/register",
        body={"username": tmp_username, "password": "tmp123456", "nickname": "待改名"},
    )
    tmp_token = ((res or {}).get("data") or {}).get("token")
    check("注册临时用户（改资料用）", bool(tmp_token), str(res)[:200])

    tmp_avatar = app_module.AVATAR_OPTIONS[2]
    status, res, _ = request(
        "PATCH", "/api/users/me", body={"nickname": "改过的昵称", "avatar": tmp_avatar}, token=tmp_token
    )
    check(
        "改昵称 + 头像成功",
        status == 200
        and (res.get("data") or {}).get("nickname") == "改过的昵称"
        and (res.get("data") or {}).get("avatar") == tmp_avatar,
        str(res)[:200],
    )
    status, res, _ = request("GET", "/api/auth/me", token=tmp_token)
    check("再查 /auth/me 已是新资料", status == 200 and res["data"]["nickname"] == "改过的昵称", str(res)[:200])
    status, res, _ = request("PATCH", "/api/users/me", body={}, token=tmp_token)
    check("昵称与头像都不传 → 400", status == 400, str(res)[:200])
    status, res, _ = request("PATCH", "/api/users/me", body={"nickname": "无 token"})
    check("未登录改资料 → 401", status == 401, str(res)[:200])

    # 头像必须是图片地址：emoji 一律拒绝，保证数据库里永远不出现表情字符
    status, res, _ = request(
        "POST",
        "/api/auth/register",
        body={"username": f"emoji_{uuid.uuid4().hex[:6]}", "password": "tmp123456", "avatar": "😀"},
    )
    check("注册时头像传 emoji 会被拒绝（422）", status == 422, str(res)[:200])

    status, res, _ = request("PATCH", "/api/users/me", body={"avatar": "🐼"}, token=tmp_token)
    check("改资料时头像传 emoji 也会被拒绝（422）", status == 422, str(res)[:200])

    # ---------- 4) 互动消息（点赞 / 回复 / @我） ----------
    status, res, _ = request("GET", "/api/users/me/notifications")
    check("未登录看互动消息 → 401", status == 401, str(res)[:200])
    status, res, _ = request("GET", "/api/users/me/notifications?type=boss", token=demo_token)
    check("非法消息类型 → 422", status == 422, str(res)[:200])

    status, res, _ = request("GET", "/api/users/me/notifications", token=demo_token)
    data = (res or {}).get("data") or {}
    check(
        "互动消息返回分页结构 + unreadCount",
        status == 200 and {"list", "total", "unreadCount"} <= set(data),
        str(data)[:200],
    )
    before_total = data.get("total", 0)

    # demo 的帖子（帖 4）被管理员点赞 + 评论；管理员又在别人的帖子里 @demo
    status, res, _ = request("POST", "/api/posts/4/like", token=admin_token)
    check("管理员点赞 demo 的帖子", status == 200, str(res)[:200])
    status, res, _ = request("POST", "/api/posts/4/comments", body={"text": "写得不错！"}, token=admin_token)
    check("管理员评论 demo 的帖子", status == 200, str(res)[:200])
    status, res, _ = request(
        "POST", "/api/posts/1/comments", body={"text": "@测试用户 求分享经验"}, token=admin_token
    )
    check("管理员在别人的帖子下 @demo", status == 200, str(res)[:200])

    status, res, _ = request("GET", "/api/users/me/notifications", token=demo_token)
    data = (res or {}).get("data") or {}
    kinds = sorted({m["type"] for m in data.get("list", [])})
    check("收到点赞 / 回复 / @我 三类消息", status == 200 and kinds == ["like", "mention", "reply"], str(kinds))
    check("消息总数 = 原有 + 3", data.get("total") == before_total + 3, f"{before_total} → {data.get('total')}")
    check(
        "未读数与总数一致（尚未标记已读）",
        data.get("unreadCount") == data.get("total"),
        str(data.get("unreadCount")),
    )
    first = (data.get("list") or [{}])[0]
    check(
        "消息字段可直接渲染（avatar / name / action / content / time）",
        {"avatar", "name", "action", "content", "time", "postId"} <= set(first),
        str(first)[:200],
    )

    for kind in ("like", "reply", "mention"):
        status, res, _ = request(f"GET", f"/api/users/me/notifications?type={kind}", token=demo_token)
        rows = ((res or {}).get("data") or {}).get("list") or []
        check(
            f"按类型筛选：{kind}",
            status == 200 and bool(rows) and all(r["type"] == kind for r in rows),
            str(rows)[:150],
        )

    status, res, _ = request("GET", "/api/users/me/notifications/unread", token=demo_token)
    check("未读数接口可读", status == 200 and res["data"]["unreadCount"] >= 3, str(res)[:200])
    status, res, _ = request("POST", "/api/users/me/notifications/read", token=demo_token)
    check("标记已读成功（角标清零）", status == 200 and res["data"]["unreadCount"] == 0, str(res)[:200])
    status, res, _ = request("GET", "/api/users/me/notifications/unread", token=demo_token)
    check("标记后未读数为 0", status == 200 and res["data"]["unreadCount"] == 0, str(res)[:200])
    time.sleep(1.2)  # 评论时间也是秒级精度，等 1.2 秒确保新评论时间晚于"标记已读"时间
    request("POST", "/api/posts/4/comments", body={"text": "已读之后的新评论"}, token=admin_token)
    status, res, _ = request("GET", "/api/users/me/notifications/unread", token=demo_token)
    check("标记后再有新互动 → 未读数回到 1", status == 200 and res["data"]["unreadCount"] == 1, str(res)[:200])

    # ---------- 5) 头像：库里只有图片地址，没有 emoji ----------
    png_status, png_head = 0, b""
    try:
        with urllib.request.urlopen(BASE_URL + app_module.DEFAULT_AVATAR, timeout=10) as resp:
            png_status, png_head = resp.status, resp.read(8)
    except urllib.error.HTTPError as err:
        png_status = err.code
    check(
        "静态头像图可访问（/static 已挂载）",
        png_status == 200 and png_head.startswith(b"\x89PNG"),
        f"status={png_status}",
    )

    status, res, _ = request("GET", "/api/admin/users?page=1&pageSize=50", token=admin_token)
    rows = ((res or {}).get("data") or {}).get("list") or []
    bad = [u["username"] for u in rows if not str(u.get("avatar") or "").startswith("/static/avatars/")]
    check("用户头像全部是图片地址（库里不再有 emoji）", status == 200 and rows and not bad, f"异常账号：{bad}")

    status, res, _ = request("GET", "/api/bars")
    bar_icons = {(b.get("icon") or "") for b in ((res or {}).get("data") or [])}
    check(
        "吧图标仍是 emoji（本方案只改头像，符合预期）",
        status == 200 and any(not str(i).startswith("/") for i in bar_icons),
        str(sorted(bar_icons))[:120],
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 78)
    print("bbs-app 后端冒烟测试")
    print("=" * 78)

    # 1) 应用装配
    print("[1/3] 校验应用与路由")
    paths = {getattr(route, "path", None) for route in app_module.app.routes}
    missing = EXPECTED_PATHS - paths
    check("核心路由全部注册", not missing, f"缺失：{sorted(missing)}")
    schema = app_module.app.openapi()
    check("OpenAPI 文档可生成", "/api/posts" in schema.get("paths", {}))
    check("Bearer 鉴权已写入文档", "HTTPBearer" in json.dumps(schema.get("components", {})))

    # 2) 起临时服务（不占用 8000 端口，避免和你正在跑的服务冲突）
    print(f"[2/3] 在 127.0.0.1:{PORT} 启动临时服务")
    config = uvicorn.Config(app_module.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.2)
    check("临时服务启动成功", server.started)

    # 3) 通用行为 + 业务闭环
    print("[3/3] 校验接口行为")
    try:
        status, res, _ = request("GET", "/api/health")
        check("健康检查返回统一响应体 {code,message,data}", status == 200 and res.get("code") == 0, str(res))
        db_ok = bool(((res or {}).get("data") or {}).get("db"))
        print(f"      数据库状态：{'可用（继续跑完整业务闭环）' if db_ok else '不可用（跳过业务闭环）'}")

        status, res, headers = request("GET", "/api/bars", extra_headers={"Origin": "http://localhost:8080"})
        check("CORS 跨域头存在（access-control-allow-origin: *）", headers.get("access-control-allow-origin") == "*", str(headers.get("access-control-allow-origin")))

        status, res, _ = request("GET", "/api/auth/me")
        check("未登录访问受保护接口 → 401 + 统一错误体", status == 401 and res.get("code") == 401, str(res))

        status, res, _ = request("POST", "/api/auth/register", body={"username": "a", "password": "1"})
        check("参数不合法 → 422 + 统一错误体", status == 422 and res.get("code") == 422, str(res)[:200])

        if db_ok:
            run_api_checks()
            print("  --- 高级管理员 / 管理员账号管理 ---")
            run_super_admin_checks()
            print("  --- 后台管理补全（用户管理 / 贴吧编辑删除 / 全部评论） ---")
            run_admin_backfill_checks()
            print("  --- 数据收口（足迹 / 转发 / 改资料 / 互动消息） ---")
            run_data_centralization_checks()
        else:
            print("      提示：数据库不可用时业务接口返回 503，message 里带 MySQL 报错；")
            print("            先 net start MySQL 并导入 sql/bbs_schema.sql，再重跑本脚本。")
    finally:
        server.should_exit = True
        time.sleep(0.6)

    print("-" * 78)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  [X] {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())


