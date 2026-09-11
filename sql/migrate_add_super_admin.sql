-- =============================================================================
-- 增量升级脚本：为已有数据库加「高级管理员」能力
--   （如果你的 bbs_app 库是旧版本建的、不想重建数据，执行本脚本即可）
-- 用法：D:\Mysql\mysql-8.0.46-winx64\bin\mysql.exe -u root -p < sql/migrate_add_super_admin.sql
-- =============================================================================
USE `bbs_app`;

-- 1) 角色枚举从两档扩展为三档：user / admin / super_admin
ALTER TABLE `users`
  MODIFY COLUMN `role` ENUM('user','admin','super_admin') NOT NULL DEFAULT 'user'
  COMMENT '角色：user 普通用户 / admin 管理员 / super_admin 高级管理员';

-- 2) 写入高级管理员账号（用户名唯一键保证重复执行不会插入两条）
--    username: superadmin    password: super123456    （上线后请立即改密码）
INSERT IGNORE INTO `users` (`username`, `password_hash`, `nickname`, `avatar`, `role`)
VALUES ('superadmin',
        'pbkdf2_sha256$200000$5928dd9a39cc4123a74609201524efbd$64b71fd237337e2b138e32c402d178a1ea2574fc4d7413478eb1e828dff65853',
        '高级管理员', '👑', 'super_admin');

-- 3) 可选：把已有的 admin 也提升为高级管理员（按需执行）
-- UPDATE `users` SET `role` = 'super_admin' WHERE `username` = 'admin';

-- 4) 自查：应看到 super_admin 一行
-- SELECT id, username, nickname, role, status FROM `users` WHERE role <> 'user';
