# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。同名批次按导入顺序生成递增 `version`，新版本通过 `replaces_id` 指向上一版本并置 `current`，补报即并入原批次。旧版本保留供追溯。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。`pinned` 保存已发月报当时采用的餐单组合，`superseded_by` 指向同批更晚受理的试算。`status` 增加 `superseded`。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核和报表分发动作。

## 从旧版本升级

`datasets` 新增 `version`、`current`、`replaces_id`，`runs` 新增 `pinned`、`superseded_by`，`status` 新增取值。对已有库执行：

```sql
ALTER TABLE datasets ADD COLUMN version INT NOT NULL DEFAULT 1,
  ADD COLUMN current TINYINT(1) NOT NULL DEFAULT 1,
  ADD COLUMN replaces_id INT NULL;
ALTER TABLE datasets ADD INDEX ix_datasets_current (current);
UPDATE datasets SET current = 0 WHERE id NOT IN (
  SELECT keep_id FROM (SELECT MAX(id) AS keep_id FROM datasets GROUP BY name) AS latest);
ALTER TABLE runs ADD COLUMN pinned JSON NULL, ADD COLUMN superseded_by INT NULL;
```

全新部署 `docker compose down -v` 后由模型自动建表，无需迁移。
