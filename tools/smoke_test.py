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
        "POST", "/api/auth/register",
        body={"username": username, "password": password, "nickname": f"冒烟{suffix}", "avatar": "🧪"},
    )
    check("注册成功并返回 token", status == 200 and res.get("code") == 0 and res["data"].get("token"), str(res))

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
            "avatar": "🧪",
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


