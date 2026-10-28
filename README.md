# 语义表情包（astrbot_plugin_sticker_semantic）

AstrBot 的独立表情包管理与语境化发送插件：

- 从聊天中自动学习表情包，或从网页/目录导入；
- 用视觉模型给表情包分类、写描述，生成中文标签与匹配关键词；
- 在 LLM 请求前注入当前语境匹配的表情包候选；
- 在回复发出前，根据“上下文 + 将要发出的回复”选择表情包：
  - 轻回复：整条替换成一张表情包；
  - 普通回复：按概率在末尾附加一张；
- 自带可视化表情包管理页面，不需要逐条输入命令。

插件拥有自己的 SQLite 数据库和 `stickers/` 目录，不读取也不修改其他插件的配置、数据库或表情包文件。

## 功能

- **自动学习**：群里有人发表情包时，按 sha256 去重后收进插件自己的库。
- **批量导入**：扫描目录或单张图片，自动复制并后台分类。
- **视觉分类**：描述、标签、匹配关键词、强度、用法、安全标记。
- **新加入待确认**：新收录的图先进入“新加入”栏，人工确认后才进入正常库并参与回复。
- **语境匹配**：标签 + 关键词 + 极性 + 强度 + 冷却时间综合打分，不用向量数据库。
- **轻回复只发表情**：短反应、纯表情/标点等回复可整条替换成表情包。
- **中文标签选择器**：标签在界面上显示中文，内部使用固定英文键参与匹配。
- **按描述重算**：改完描述后，用文字模型重新生成标签和关键词，不覆盖描述。
- **模型主动选图**：提供 `search_stickers` / `pick_sticker` 两个 LLM 工具。
- **可视化管理**：卡片网格、缩略图、搜索、筛选、分页、上传、删除、重新分类。

## 环境要求

- AstrBot `4.28+`（使用插件 Page 与插件 Web API）
- AstrBot 自带的 `aiosqlite`、`Pillow`
- 一个支持图片输入的 Provider（用于识图分类）
- 可选：一个纯文字 Provider（用于按描述重算标签）

## 安装

把本目录放到 AstrBot 的插件目录：

```text
<AstrBot 数据目录>/plugins/astrbot_plugin_sticker_semantic
```

然后在 AstrBot WebUI 插件页重载插件。

打开插件详情页，会看到 **「表情包管理」** 页面。

## 首次配置

在插件配置中至少确认：

```text
learning.classify_provider_id   识图分类用的视觉 Provider ID
learning.retag_provider_id      按描述重算标签用的文字 Provider ID
```

两项都可以留空：

- `classify_provider_id` 留空时，依次回退到 AstrBot 的图片理解 Provider、当前对话模型；
- `retag_provider_id` 留空时，使用当前对话模型。

然后：

1. 上传几张表情包，或使用 `/sk import <目录>`；
2. 等后台分类完成；
3. 在顶部“新加入”栏检查描述/标签；
4. 确认无误后点“确认加入”；
5. 之后这些图才会参与回复。

## 工作流程

```text
用户发表情包
  → 识别图片
  → 复制到插件目录 + sha256 去重
  → 视觉模型分类
  → 写入 SQLite，confirmed=0（新加入）
  → 人工确认后 confirmed=1

用户发文字
  → on_llm_request 读取最近上下文
  → 本地打分选候选
  → 注入候选 ID/标签/描述
  → LLM 生成回复
  → on_decorating_result 结合上下文和回复重新打分
  → 轻回复替换成表情包
  → 普通回复概率附加表情包
```

## 标签与关键词

标签是固定的内置分类，内部使用英文键：

```text
happy, laugh, sad, cry, angry, surprised, confused, shy, love, calm,
tired, disgust, fear, excited, awkward, smug, greet, morning, goodnight,
agree, disagree, praise, comfort, tease, apologize, thank, celebrate,
encourage, ask, refuse, wait, react
```

界面显示对应中文（开心、大笑、难过等），点击切换即可，不需要手输英文。

描述和匹配关键词可以是任意中文。匹配时：

```text
标签权重最高，其次关键词、意图、极性、强度。
关键词使用子串命中 + 字符 bigram 相似度，不使用向量检索。
```

需要新增自定义标签时，修改 `core/taxonomy.py` 的 `TAG_LABELS` / `TAG_KEYWORDS`。

## 可视化管理页面

重载后在插件详情页打开“表情包管理”。

页面功能：

- 顶部“新加入”待确认栏；
- 卡片网格 + 缩略图；
- 搜索 ID / 描述 / 标签 / 关键词；
- 状态、标签筛选与分页；
- 点卡片编辑：描述、中文标签选择器、匹配关键词、usage、强度、safe、状态；
- 按描述重算标签/关键词；
- 多选上传；
- 单张/全部重新分类；
- 启用、停用、删除；
- 安全过滤开关。

页面通过 AstrBot 自带的插件 Page 和插件 Web API 工作，不需要另开端口，也不需要额外服务。

### 新加入与确认

- 自动学习、网页上传、`/sk import` 默认进入“新加入”（`confirmed=0`）；
- AI 仍会正常分类、写描述，但未确认的图不会参与回复、注入或搜索；
- 点“确认加入”后 `confirmed=1`，才进入正常表情库；
- 旧数据库升级时，已有数据默认标记为已确认，不会突然全部进入待审核。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/sk help` | 所有人 | 帮助 |
| `/sk list [页]` | 所有人 | 查看表情包 |
| `/sk stats` | 所有人 | 状态与标签统计 |
| `/sk test <文本>` | 管理员 | 查看某段文本的匹配分数 |
| `/sk import [路径]` | 管理员 | 导入目录/单文件，后台分类 |
| `/sk add` | 管理员 | 把当前消息里的图片加入库 |
| `/sk tag <ID> <标签>` | 管理员 | 手动改标签 |
| `/sk desc <ID\|回复> <描述>` | 管理员 | 改描述；回复表情包时可省略 ID |
| `/sk keywords <ID\|回复> <词1,词2>` | 管理员 | 改匹配关键词；回复时可省略 ID |
| `/sk retag <ID\|回复>` | 管理员 | 按描述重算标签和关键词 |
| `/sk info <ID\|回复>` | 管理员 | 查看详情 |
| `/sk usage <ID> reaction\|augment\|both` | 管理员 | 改用法 |
| `/sk safe <ID> on\|off` | 管理员 | 标记安全/不安全 |
| `/sk safety on\|off` | 管理员 | 开关不适宜内容过滤 |
| `/sk enableall` | 管理员 | 恢复所有 disabled 表情包 |
| `/sk enable <ID>` / `/sk disable <ID>` | 管理员 | 启用/停用 |
| `/sk delete <ID>` | 管理员 | 删除文件和记录 |
| `/sk reclassify <ID\|all>` | 管理员 | 重新调用视觉模型分类 |
| `/sk reload` | 管理员 | 刷新缓存 |

ID 是 sha256 前 16 位，`/sk list` 会显示。

回复表情包后，以下命令可省略 ID：

```text
/sk desc 一只猫笑到打滚
/sk keywords 哈哈哈,笑死,绷不住
/sk retag
/sk info
```

## 分类/重算使用的模型

**识图分类**：

1. `learning.classify_provider_id`
2. AstrBot `provider_settings.default_image_caption_provider_id`
3. 当前对话模型

**按描述重算标签/关键词**：

1. `learning.retag_provider_id`
2. 当前对话模型

Provider ID 的写法取决于你的 AstrBot 配置，例如：

```text
<provider-source-id>/<model-id>
```

## 配置项

### general

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `admin_users` | [] | 额外管理员 |
| `target_scopes` | [] | 生效会话白名单，留空全部 |
| `max_stickers` | 500 | 表情包上限 |
| `import_dir` | "" | `/sk import` 默认目录 |
| `import_recursive` | false | 递归扫描 |

### learning

| 键 | 默认 | 说明 |
|---|---|---|
| `auto_learn` | true | 自动学习 |
| `learn_target_users` | [] | 只学这些用户，留空全部 |
| `learn_all_images` | false | 所有图片都当表情包 |
| `auto_classify` | true | 新图自动分类 |
| `safety_filter_enabled` | true | 关闭后不再按 `safe=false` 自动禁用 |
| `classify_provider_id` | "" | 视觉 Provider；留空自动回退 |
| `retag_provider_id` | "" | 文字 Provider；留空用当前对话模型 |
| `classify_concurrency` | 2 | 并发分类数 |
| `classify_timeout` | 60 | 单张超时秒数 |
| `max_file_mb` | 8 | 单文件上限 |

### selection

| 键 | 默认 | 说明 |
|---|---|---|
| `light_reply_enabled` | true | 轻回复只发表情包 |
| `light_reply_max_chars` | 12 | 轻回复最大字数 |
| `light_min_score` | 0.30 | 轻回复门槛 |
| `light_trigger_probability` | 0.9 | 轻回复触发概率 |
| `append_enabled` | true | 普通回复允许附加 |
| `append_min_text_chars` | 8 | 附加最短回复字数 |
| `append_min_score` | 0.45 | 附加门槛 |
| `append_probability` | 0.25 | 附加概率 |
| `max_per_hour` | 10 | 每会话每小时上限，0 不限制 |
| `sticker_cooldown_seconds` | 180 | 同一张最短复用间隔 |
| `recent_history` | 6 | 参与匹配的最近消息数 |

### prompt

| 键 | 默认 | 说明 |
|---|---|---|
| `inject_enabled` | true | 注入候选 |
| `inject_candidates` | 3 | 候选数量 |
| `inject_max_chars` | 600 | 注入最大字符数 |
| `tool_enabled` | true | 启用 `search_stickers` / `pick_sticker` |

## 数据目录

运行时数据在：

```text
<AstrBot 数据目录>/plugin_data/astrbot_plugin_sticker_semantic/
  stickers.db
  stickers/
  thumbs/
  uploads/
  imports/
```

仓库本身不包含数据库、表情包、缩略图或用户配置。发布前请确保这些目录没有被打包。

## 可选：从旧插件迁移

`tools/migrate_legacy.py` 可以从 `astrbot_plugin_self_evolution` 的 `stickers/` 目录复制表情包并分类：

```powershell
python tools/migrate_legacy.py --model <vision-model-id>
```

脚本只读取旧目录，复制到本插件；旧库不会被修改。迁移进来的图默认标记为已确认。使用前需要安装 `anthropic` SDK，并保证 AstrBot 配置中有可用的 Anthropic 兼容 Provider。

## 测试

```powershell
python -m unittest discover -s tests -t . -v
```

覆盖标签提取、极性、轻回复判定、打分排序、usage 过滤、SQLite 去重、描述/关键词编辑、确认流程等。

## 隐私

- 插件不包含任何作者账号、服务器地址、API Key 或用户表情包；
- 运行时数据库与图片只存在本机 AstrBot 数据目录；
- 分类会把表情包发送给你配置的视觉 Provider，请自行确认 Provider 的隐私政策；
- 删除插件不会自动删除数据目录，需要彻底清理时手动删除。

## License

MIT
