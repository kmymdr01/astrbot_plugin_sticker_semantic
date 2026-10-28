# 发布到 GitHub

这个目录已经是干净的插件源码，不包含运行时数据库、表情包、缩略图或用户配置。

## 上传前需要改的地方

1. `metadata.yaml`
   - `author`：改成你的 GitHub 用户名或组织名；
   - `repo`：填成最终仓库地址，例如
     `https://github.com/<your-name>/astrbot_plugin_sticker_semantic`。
2. `README.md`
   - 可以把标题/介绍改成自己的风格；
   - 不要在 README 里加入本机绝对路径、账号、API Key 或真实表情包截图。
3. 确认 `LICENSE` 中的版权人符合你的预期。

## 初始化并推送

```bash
cd astrbot_plugin_sticker_semantic
git init
git add .
git commit -m "feat: initial astrbot sticker semantic plugin"
git branch -M main
git remote add origin https://github.com/<your-name>/astrbot_plugin_sticker_semantic.git
git push -u origin main
```

## 发布前检查

```bash
git status --short
```

确认没有被误加入的文件：

```text
stickers.db
stickers/
thumbs/
uploads/
imports/
__pycache__/
docs/*.png
```

`.gitignore` 已经排除了这些路径。也可以执行：

```bash
git grep -n -E 'C:\\\\Users|api[_-]?key|secret|token' || true
```

确认没有个人路径或密钥。

## AstrBot 插件市场

如果提交到 AstrBot 插件市场，确保：

- `metadata.yaml` 的 `name` 与插件目录名一致；
- `author` / `repo` 已填写；
- 插件根目录有 `main.py` 和 `_conf_schema.json`；
- `pages/stickers/index.html` 与 `app.js` 一起提交；
- 不要提交 `data/`、`stickers/`、`*.db`。

## 版本

当前 `metadata.yaml` 版本为 `1.3.0`。更新功能后同步修改版本号，并更新 `CHANGELOG.md`。
