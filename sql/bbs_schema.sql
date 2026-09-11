-- =============================================================================
-- 贴吧社区（bbs-app）数据库建表脚本 —— MySQL 8.0
-- 与前端 bbs-app-frontend（uni-app + Vue3）的数据结构对齐：
--   前端 utils/store.js 里的 bars / posts / comments / favorites / followed_bars
--   在数据库里对应：bars / posts / post_images / comments / likes / favorites / follows
-- 使用方式：
--   D:\Mysql\mysql-8.0.46-winx64\bin\mysql.exe -u root -p < sql/bbs_schema.sql
--   或在 Navicat / DBeaver 里整段执行
-- 说明：不使用任何 ORM；表结构用 snake_case，接口返回时转成前端使用的
--       camelCase（见 main.py 的序列化函数），前端无需改动即可对接。
-- =============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE DATABASE IF NOT EXISTS `bbs_app`
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_general_ci;
USE `bbs_app`;

-- 先删后建（顺序：先删引用方，再删被引用方）
DROP TABLE IF EXISTS `notify_read`;
DROP TABLE IF EXISTS `footprints`;
DROP TABLE IF EXISTS `likes`;
DROP TABLE IF EXISTS `favorites`;
DROP TABLE IF EXISTS `follows`;
DROP TABLE IF EXISTS `post_images`;
DROP TABLE IF EXISTS `comments`;
DROP TABLE IF EXISTS `posts`;
DROP TABLE IF EXISTS `bars`;
DROP TABLE IF EXISTS `users`;

-- -----------------------------------------------------------------------------
-- 1. 用户表（role 区分普通用户 / 管理员）
-- -----------------------------------------------------------------------------
CREATE TABLE `users` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username`      VARCHAR(32)     NOT NULL                COMMENT '登录用户名（唯一）',
  `password_hash` VARCHAR(255)    NOT NULL                COMMENT '密码哈希（PBKDF2-SHA256：pbkdf2_sha256$迭代次数$盐$哈希）',
  `nickname`      VARCHAR(32)     NOT NULL DEFAULT ''     COMMENT '昵称（前端展示的 author）',
  `avatar`        VARCHAR(255)    NOT NULL DEFAULT '/static/avatars/default.png' COMMENT '头像图片地址（后端 /static/avatars 下的图片，或 http URL）',
  `role`          ENUM('user','admin','super_admin') NOT NULL DEFAULT 'user' COMMENT '角色：user 普通用户 / admin 管理员 / super_admin 高级管理员',
  `status`        TINYINT         NOT NULL DEFAULT 1      COMMENT '状态：1 正常 / 0 禁用',
  `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
  `updated_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_users_username` (`username`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '用户表';

-- -----------------------------------------------------------------------------
-- 2. 贴吧表（板块）
-- -----------------------------------------------------------------------------
CREATE TABLE `bars` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '贴吧ID',
  `name`       VARCHAR(32)     NOT NULL                COMMENT '吧名，如 前端吧',
  `image`      VARCHAR(255)    DEFAULT NULL            COMMENT '吧图 URL（前端 bar.img；emoji 吧图标已废弃）',
  `intro`      VARCHAR(255)    NOT NULL DEFAULT ''     COMMENT '吧简介（前端 bar.desc）',
  `owner`      VARCHAR(32)     NOT NULL DEFAULT '官方' COMMENT '吧主昵称（前端 bar.owner）',
  `sort`       INT             NOT NULL DEFAULT 0      COMMENT '排序值，越小越靠前',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_bars_name` (`name`),
  KEY `idx_bars_sort` (`sort`, `id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '贴吧表（板块）';

-- -----------------------------------------------------------------------------
-- 3. 帖子表（点赞/评论/转发数做冗余计数，用事务 + 原生 SQL 维护，避免频繁 COUNT）
-- -----------------------------------------------------------------------------
CREATE TABLE `posts` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '帖子ID',
  `bar_id`        BIGINT UNSIGNED NOT NULL                COMMENT '所属贴吧ID',
  `user_id`       BIGINT UNSIGNED NOT NULL                COMMENT '发帖人ID',
  `tag`           VARCHAR(32)     NOT NULL DEFAULT '未分类' COMMENT '分类标签（前端 post.tag）',
  `title`         VARCHAR(100)    NOT NULL                COMMENT '标题',
  `content`       TEXT            NOT NULL                COMMENT '正文',
  `like_count`    INT             NOT NULL DEFAULT 0      COMMENT '点赞数（冗余）',
  `comment_count` INT             NOT NULL DEFAULT 0      COMMENT '评论数（冗余，与前端 commentCount 对应）',
  `forward_count` INT             NOT NULL DEFAULT 0      COMMENT '转发数（前端只展示，无转发接口）',
  `view_count`    INT             NOT NULL DEFAULT 0      COMMENT '浏览数',
  `is_top`        TINYINT         NOT NULL DEFAULT 0      COMMENT '是否置顶：1 是 / 0 否',
  `status`        TINYINT         NOT NULL DEFAULT 1      COMMENT '状态：1 正常 / 0 已删除（软删除）',
  `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发布时间',
  `updated_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_posts_bar` (`bar_id`, `status`, `is_top`, `id`),
  KEY `idx_posts_user` (`user_id`, `status`, `id`),
  KEY `idx_posts_time` (`status`, `id`),
  CONSTRAINT `fk_posts_bar`  FOREIGN KEY (`bar_id`)  REFERENCES `bars` (`id`)  ON DELETE CASCADE,
  CONSTRAINT `fk_posts_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '帖子表';

-- -----------------------------------------------------------------------------
-- 4. 帖子图片表（一个帖子最多 9 张，sort_order 记录顺序）
-- -----------------------------------------------------------------------------
CREATE TABLE `post_images` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '图片ID',
  `post_id`    BIGINT UNSIGNED NOT NULL                COMMENT '所属帖子ID',
  `image_url`  VARCHAR(255)    NOT NULL                COMMENT '图片地址（/uploads/... 或外部 http 地址）',
  `sort_order` TINYINT         NOT NULL DEFAULT 0      COMMENT '顺序（0 开始，最多 0~8）',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_post_images_sort` (`post_id`, `sort_order`),
  CONSTRAINT `fk_post_images_post` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '帖子图片表（最多9张/帖）';

-- -----------------------------------------------------------------------------
-- 5. 评论表（parent_id 预留二级回复）
-- -----------------------------------------------------------------------------
CREATE TABLE `comments` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '评论ID',
  `post_id`    BIGINT UNSIGNED NOT NULL                COMMENT '所属帖子ID',
  `user_id`    BIGINT UNSIGNED NOT NULL                COMMENT '评论人ID',
  `parent_id`  BIGINT UNSIGNED DEFAULT NULL            COMMENT '父评论ID（NULL 表示一级评论）',
  `content`    VARCHAR(1000)   NOT NULL                COMMENT '评论内容（前端字段名 text）',
  `like_count` INT             NOT NULL DEFAULT 0      COMMENT '点赞数（冗余）',
  `status`     TINYINT         NOT NULL DEFAULT 1      COMMENT '状态：1 正常 / 0 已删除',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '评论时间',
  PRIMARY KEY (`id`),
  KEY `idx_comments_post` (`post_id`, `status`, `id`),
  KEY `idx_comments_user` (`user_id`),
  KEY `idx_comments_parent` (`parent_id`),
  CONSTRAINT `fk_comments_post` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_comments_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '评论表';

-- -----------------------------------------------------------------------------
-- 6. 点赞表（同一张表承载「帖子点赞」与「评论点赞」，target_type 区分）
--    注意：target_id 指向 posts.id 或 comments.id（多态关联），因此不加外键，
--          由业务代码保证；唯一键保证同一用户对同一对象只能点赞一次。
-- -----------------------------------------------------------------------------
CREATE TABLE `likes` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '点赞记录ID',
  `user_id`     BIGINT UNSIGNED NOT NULL                COMMENT '点赞用户ID',
  `target_type` ENUM('post','comment') NOT NULL         COMMENT '点赞对象类型：post 帖子 / comment 评论',
  `target_id`   BIGINT UNSIGNED NOT NULL                COMMENT '点赞对象ID（posts.id 或 comments.id）',
  `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '点赞时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_likes_user_target` (`user_id`, `target_type`, `target_id`),
  KEY `idx_likes_target` (`target_type`, `target_id`),
  CONSTRAINT `fk_likes_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '点赞表（帖子/评论共用）';

-- -----------------------------------------------------------------------------
-- 7. 收藏表（用户收藏帖子）
-- -----------------------------------------------------------------------------
CREATE TABLE `favorites` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '收藏记录ID',
  `user_id`    BIGINT UNSIGNED NOT NULL                COMMENT '用户ID',
  `post_id`    BIGINT UNSIGNED NOT NULL                COMMENT '收藏的帖子ID',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '收藏时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_favorites_user_post` (`user_id`, `post_id`),
  KEY `idx_favorites_user` (`user_id`, `id`),
  CONSTRAINT `fk_favorites_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_favorites_post` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '帖子收藏表';

-- -----------------------------------------------------------------------------
-- 8. 关注表（用户关注贴吧）
-- -----------------------------------------------------------------------------
CREATE TABLE `follows` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '关注记录ID',
  `user_id`    BIGINT UNSIGNED NOT NULL                COMMENT '用户ID',
  `bar_id`     BIGINT UNSIGNED NOT NULL                COMMENT '关注的贴吧ID',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '关注时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_follows_user_bar` (`user_id`, `bar_id`),
  KEY `idx_follows_user` (`user_id`, `id`),
  KEY `idx_follows_bar` (`bar_id`),
  CONSTRAINT `fk_follows_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_follows_bar`  FOREIGN KEY (`bar_id`)  REFERENCES `bars` (`id`)  ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '贴吧关注表';

-- -----------------------------------------------------------------------------
-- 9. 足迹表（App 进吧页的「足迹」：谁进过哪个吧、最近一次浏览时间）
--    说明：后端启动时也会用 CREATE TABLE IF NOT EXISTS 自动补建，这里保持一致，
--          便于「一键重建数据库」时结构完整。
-- -----------------------------------------------------------------------------
CREATE TABLE `footprints` (
  `id`        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`   BIGINT UNSIGNED NOT NULL                COMMENT '用户ID',
  `bar_id`    BIGINT UNSIGNED NOT NULL                COMMENT '浏览过的贴吧ID',
  `viewed_at` DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '最近一次浏览时间（微秒精度，保证同秒内的先后顺序正确）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_footprints_user_bar` (`user_id`, `bar_id`),
  KEY `idx_footprints_user_time` (`user_id`, `viewed_at`),
  CONSTRAINT `fk_footprints_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_footprints_bar`  FOREIGN KEY (`bar_id`)  REFERENCES `bars`  (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '足迹（进过的吧）';

-- -----------------------------------------------------------------------------
-- 10. 互动消息已读位置（点赞 / 回复 / @我 三种消息的未读数依赖它）
--     消息本身不建表：由 likes / comments 现算，保证与帖子数据永远一致。
-- -----------------------------------------------------------------------------
CREATE TABLE `notify_read` (
  `user_id` BIGINT UNSIGNED NOT NULL COMMENT '用户ID',
  `read_at` DATETIME(6)     NOT NULL COMMENT '互动消息已读时间（微秒精度）',
  PRIMARY KEY (`user_id`),
  CONSTRAINT `fk_notify_read_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '互动消息已读位置';

SET FOREIGN_KEY_CHECKS = 1;

-- =============================================================================
-- 种子数据（与前端 bbs-app-frontend 的演示数据保持一致，方便前后端联调）
-- 演示账号：admin / admin123456（管理员）、demo / demo123456（普通用户，其余演示用户同密码）
-- =============================================================================

-- 用户：1 高级管理员 + 1 管理员 + 4 普通用户（头像统一用后端 /static/avatars 下的图片）
INSERT INTO `users` (`id`, `username`, `password_hash`, `nickname`, `avatar`, `role`) VALUES
(1, 'admin', 'pbkdf2_sha256$200000$723e821f4c68a0c3bd456a8bb65fa7a9$091ab8264ef5170edadbe92fb4dfc1d7da4fe7af601c2412c5c909981857873a', '吧务管理', '/static/avatars/avatar-1.png', 'admin'),
(2, 'demo', 'pbkdf2_sha256$200000$637bebcae8aedab5a6a7a7c2181d9f4a$ff07605227ed94319cb0a20f673850f8f125de61277686d137ae15672d55ff7a', '测试用户', '/static/avatars/avatar-2.png', 'user'),
(3, 'frontend_girl', 'pbkdf2_sha256$200000$637bebcae8aedab5a6a7a7c2181d9f4a$ff07605227ed94319cb0a20f673850f8f125de61277686d137ae15672d55ff7a', '前端小白', '/static/avatars/avatar-3.png', 'user'),
(4, 'foodie', 'pbkdf2_sha256$200000$637bebcae8aedab5a6a7a7c2181d9f4a$ff07605227ed94319cb0a20f673850f8f125de61277686d137ae15672d55ff7a', '干饭人', '/static/avatars/avatar-4.png', 'user'),
(5, 'gamer', 'pbkdf2_sha256$200000$637bebcae8aedab5a6a7a7c2181d9f4a$ff07605227ed94319cb0a20f673850f8f125de61277686d137ae15672d55ff7a', '摸鱼达人', '/static/avatars/avatar-5.png', 'user'),
-- 高级管理员：唯一拥有「管理员账号管理」权限的角色（密码 super123456）
(6, 'superadmin', 'pbkdf2_sha256$200000$5928dd9a39cc4123a74609201524efbd$64b71fd237337e2b138e32c402d178a1ea2574fc4d7413478eb1e828dff65853', '高级管理员', '/static/avatars/avatar-6.png', 'super_admin');

-- 贴吧：16 个，id / 名称与前端 store.js 的 BARS 一一对应
-- （emoji 吧图标已废弃：表里没有 icon 列，吧的形象统一用 image 吧图）
INSERT INTO `bars` (`id`, `name`, `image`, `intro`, `owner`, `sort`) VALUES
(1,  '前端吧', NULL, '前端开发交流：HTML/CSS/JS/Vue/React', '阿华', 1),
(2,  '美食吧', NULL, '探店、菜谱、深夜放毒', '食堂大妈', 2),
(3,  '游戏吧', NULL, '开黑、攻略、版本讨论', '手速怪', 3),
(4,  '电影吧', NULL, '影评、片单、冷门佳作', '放映员', 4),
(5,  '读书吧', NULL, '书单、书评、一起读书', '书虫', 5),
(6,  '音乐吧', NULL, '歌单、乐评、乐器交流', '耳机党', 6),
(7,  '足球吧', NULL, '赛事、转会、战术分析', '老球迷', 7),
(8,  '科技吧', NULL, '数码、硬件、AI 前沿', '极客', 8),
(9,  '摄影吧', NULL, '出片、后期、器材讨论', '快门', 9),
(10, '吉他吧', NULL, '指弹、弹唱、装备交流', '六弦', 10),
(11, '养猫吧', NULL, '铲屎官日常、养猫经验', '猫奴', 11),
(12, '跑步吧', NULL, '跑步打卡、马拉松、配速', '跑者', 12),
(13, '烘焙吧', NULL, '蛋糕、面包、烤箱食谱', '烤箱前', 13),
(14, '手工吧', NULL, '木工、羊毛毡、手作教程', '手艺人', 14),
(15, '钓鱼吧', NULL, '钓点、装备、渔获分享', '空军司令', 15),
(16, '旅游吧', NULL, '攻略、穷游、风景大片', '背包客', 16);

-- 帖子：5 条，分布在不同吧；created_at 用相对时间，便于前端展示「3小时前 / 昨天」等
INSERT INTO `posts` (`id`, `bar_id`, `user_id`, `tag`, `title`, `content`, `like_count`, `comment_count`, `forward_count`, `view_count`, `created_at`) VALUES
(1, 1, 3, '技术交流', '自学前端半年，终于拿到 offer 了！', '从零开始学 HTML/CSS/JS，再到 Vue3，一路踩坑无数，分享几点心得给同样在路上的你……', 2, 2, 1, 128, NOW() - INTERVAL 3 HOUR),
(2, 2, 4, '探店',   '周末探店：这家螺蛳粉真的绝了', '汤底浓郁，酸笋给得超多，加个炸蛋直接封神。坐标学校后街第二条巷子……', 1, 0, 0, 86, NOW() - INTERVAL 5 HOUR),
(3, 3, 5, '攻略',   '新赛季上分阵容推荐，亲测好用', '这套阵容稳吃鸡，前期运营思路简单，适合和我一样的休闲玩家……', 1, 0, 0, 233, NOW() - INTERVAL 1 DAY),
(4, 4, 2, '推荐',   '推荐几部值得二刷的冷门高分电影', '都是豆瓣 8.5 以上的冷门佳作，周末片荒的可以收藏了慢慢看……', 0, 0, 0, 156, NOW() - INTERVAL 2 DAY),
(5, 1, 1, '求助',   'Vue3 的 computed 和 watch 到底什么时候用哪个？', '最近在重构项目，经常纠结该用 computed 还是 watch。有没有大佬能给个清晰的判断标准？', 0, 1, 0, 56, NOW() - INTERVAL 6 HOUR);

-- 帖子图片：帖子 1 单图，帖子 2 三图（演示多图上传结果；演示数据用占位图，真实上传的图会存 /uploads/...）
INSERT INTO `post_images` (`post_id`, `image_url`, `sort_order`) VALUES
(1, 'https://picsum.photos/seed/bbs1/640/360', 0),
(2, 'https://picsum.photos/seed/bbs2/400/400', 0),
(2, 'https://picsum.photos/seed/bbs3/400/400', 1),
(2, 'https://picsum.photos/seed/bbs4/400/400', 2);

-- 评论
INSERT INTO `comments` (`id`, `post_id`, `user_id`, `content`, `like_count`, `created_at`) VALUES
(101, 1, 2, '恭喜恭喜，太励志了！', 1, NOW() - INTERVAL 2 HOUR),
(102, 1, 5, '求分享简历模板，蹲一个！', 0, NOW() - INTERVAL 1 HOUR),
(103, 5, 3, '简单说：能算出来的用 computed，需要做副作用的用 watch。', 0, NOW() - INTERVAL 5 HOUR);

-- 点赞（target_type='post' 帖子，'comment' 评论）
INSERT INTO `likes` (`user_id`, `target_type`, `target_id`) VALUES
(2, 'post', 1),
(5, 'post', 1),
(2, 'post', 2),
(5, 'post', 3),
(5, 'comment', 101);

-- 收藏
INSERT INTO `favorites` (`user_id`, `post_id`) VALUES (2, 1), (5, 3);

-- 关注贴吧
INSERT INTO `follows` (`user_id`, `bar_id`) VALUES (1, 1), (2, 1), (2, 2), (2, 3), (5, 1), (5, 3);

-- -----------------------------------------------------------------------------
-- 自查（可选，执行后应看到 8 张表与对应条数）
-- -----------------------------------------------------------------------------
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'bbs_app';
-- SELECT (SELECT COUNT(*) FROM users) AS users, (SELECT COUNT(*) FROM bars) AS bars,
--        (SELECT COUNT(*) FROM posts) AS posts, (SELECT COUNT(*) FROM post_images) AS images,
--        (SELECT COUNT(*) FROM comments) AS comments, (SELECT COUNT(*) FROM likes) AS likes,
--        (SELECT COUNT(*) FROM favorites) AS favorites, (SELECT COUNT(*) FROM follows) AS follows;
-- 前端首页信息流等价 SQL：帖子 + 作者 + 所属吧（图片另查，避免行膨胀）
-- SELECT p.id, p.title, p.like_count, u.nickname AS author, b.name AS bar_name
--   FROM posts p JOIN users u ON u.id = p.user_id JOIN bars b ON b.id = p.bar_id
--  WHERE p.status = 1 ORDER BY p.id DESC LIMIT 0, 10;



