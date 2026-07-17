# astr基座 (AstrBot 插件适配基座)

让 **ElainaBot_v2**（QQ 官方机器人框架）直接运行 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 框架的插件。

> ⚠️ **v2 官机版**：收发走 QQ 官方接口（支持 markdown 发图 / 富媒体），只能装在 **ElainaBot_v2** 上；OneBot 框架请用本仓库 `ElainaBot_onebot/astr基座`。

## 它做什么

把 AstrBot 插件放进 `apps/<插件名>/`（含 `main.py`），基座加载期会：

1. 注入 `astrbot` 兼容层（`astrbot.api.*`、`filter` 装饰器、消息组件、`Star`/`Context`/`event` 等）；
2. 扫描 `import` 各插件，实例化 `Star` 并调用 `initialize()`；
3. 把 `@filter.command` / `command_group` / `regex` / `event_message_type` 注册成框架 handler；
4. 把消息组件（`Plain`/`Image`/`At`/`Reply`/`Node`/`Record` 等）翻译成 QQ 官方接口消息发送（发图支持官机 markdown 直链，失败自动回退富媒体）；
5. `html_render()` / `text_to_image()` 复用框架共享 Playwright 浏览器出图。

## 目录结构

```
astr基座/
├── main.py            # 插件入口: 注入 shim -> 扫描 apps -> 注册指令 -> 挂 Web 面板
├── requirements.txt   # 基座自身依赖 (jinja2, html_render 需要)
├── runtime/           # 兼容层与运行时桥接 (commands/components/events/sending/rendering/...)
├── webpanel/          # Web 管理面板 (panel.py + panel.html): 装/卸/启停/配置/日志
└── apps/              # 放 AstrBot 插件, 每个一个子目录 (含 main.py); 出厂为空
```

> **出厂 apps/ 为空**。插件按需通过 Web 面板安装，或手动放进 `apps/<插件名>/`。

## 使用

1. 把本插件目录放到 ElainaBot_v2 的 `plugins/` 下（如 `plugins/astr基座/`）。
2. 启动框架，打开后台 → 自定义页面 → **「AstrBot 基座」**。
3. 在「安装插件」页从 GitHub 仓库 URL / zip 直链 / 上传 zip 安装 AstrBot 插件；安装后基座自动热重载即生效。

也可手动把插件放到 `apps/<插件名>/`（含 `main.py`）后在面板点「重载基座」。

## Web 面板

面板通过框架的 `core.plugin.web_pages`（`register_page` / `register_route`）挂载，接口前缀 `/api/ext/astrbot_base`。界面完全复刻 AstrBot WebUI 风格。功能：

- **插件管理 · 已安装**：列出各插件（版本、指令数、启停开关），可停用/启用、编辑配置（含指令前缀）、更新、卸载；
- **插件管理 · 插件市场**：接入 AstrBot 官方插件市场（api.soulter.top），按 star 排序、可搜索，一键下载安装；
- **安装插件**：GitHub 仓库 URL / `.zip` 直链下载安装，或上传本地 zip（右上角对话框）；安装后自动补装 `requirements.txt` 并热重载；
- **插件管理 · 指令面板**：查看基座已注册的全部指令与其匹配正则（装/卸插件后自动更新）；
- **设置**：主动推送 bot、订阅权限、markdown 发图与图床桶、pip 源，及框架 Playwright / 图床模块一键启停；
- **控制台**：查看框架实时运行日志（等级筛选 / 自动滚动 / 清空）。

## 配置

面板改动都会写入插件数据目录的 `config.yaml`（首次加载自动生成），也可直接编辑：

| 键 | 说明 |
|---|---|
| `appid` | 主动推送/订阅用哪个机器人的 appid（多 bot 时建议填，留空=自动选第一个） |
| `allow_user_subscribe` | 是否允许普通群成员订阅（false=群内仅主人/私聊任何人） |
| `image_as_markdown` | 发图是否优先用官机 markdown 直链（false=直接走 QQ 原生富媒体） |
| `image_host` | markdown 发图用的图床桶（nature=免配置 / chatglm / ukaka / xingye / cos / bilibili / qq_channel） |
| `pip_index` | 安装插件依赖的 pip 源（auto=多镜像兜底 / official / 镜像URL） |
| `command_prefixes` | `{插件目录名: 指令前缀}`，给某插件所有指令加前缀，避免冲突 |
| `disabled_apps` | 停用的插件目录名列表（不加载、不注册指令） |
| `apps` | `{插件目录名: {配置覆盖}}`，覆盖各 AstrBot 插件 `_conf_schema.json` 的配置项 |
