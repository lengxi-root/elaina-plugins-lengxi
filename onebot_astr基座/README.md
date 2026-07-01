# astr基座 (AstrBot 插件适配基座)

让 **ElainaBot-Onebot** 直接运行 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 框架的插件。

> ⚠️ **OneBot 版**：收发走 OneBot v11（`send_group_msg` + 消息段），只能装在 **ElainaBot-Onebot** 上；勿与本仓库的 v2 官机框架插件混装。

## 它做什么

把 AstrBot 插件放进 `apps/<插件名>/`（含 `main.py`），基座加载期会：

1. 注入 `astrbot` 兼容层（`astrbot.api.*`、`filter` 装饰器、消息组件、`Star`/`Context`/`event` 等）；
2. 扫描 `import` 各插件，实例化 `Star` 并调用 `initialize()`；
3. 把 `@filter.command` / `command_group` / `regex` / `event_message_type` 注册成框架 handler；
4. 把消息组件（`Plain`/`Image`/`At`/`Reply`/`Node`/`Record` 等）翻译成 OneBot 消息段发送；
5. `html_render()` / `text_to_image()` 用 jinja2 + 无头 Chrome 出图。

## 目录结构

```
astr基座/
├── main.py            # 插件入口: 注入 shim -> 扫描 apps -> 注册指令 -> 挂 Web 面板
├── requirements.txt   # 基座自身依赖 (jinja2, html_render 需要)
├── runtime/           # 兼容层与运行时桥接 (commands/components/events/sending/...)
├── webpanel/          # Web 管理面板 (panel.py + panel.html): 装/卸/启停/配置/日志
├── apps/              # 放 AstrBot 插件, 每个一个子目录 (含 main.py); 出厂为空
└── tests/             # 冒烟测试 + 测试用样例插件 (tests/fixtures/)
```

> **出厂 apps/ 为空**（基座本体 ~140KB）。插件按需通过 Web 面板安装，或手动放进 `apps/<插件名>/`。

## 使用

1. 把本插件目录放到 ElainaBot-Onebot 的 `plugins/` 下（如 `plugins/astr基座/`）。
2. 启动框架，打开 Web 面板，在侧边栏进入 **「AstrBot 基座」** 页。
3. 在「安装插件」页从 GitHub 仓库 / zip 直链 / 上传 zip 安装 AstrBot 插件；安装后基座自动热重载即生效。

也可手动把插件放到 `apps/<插件名>/`（含 `main.py`）后在面板点「热重载」。

## Web 面板

面板通过框架的 `core.plugin.web_pages`（`register_page` / `register_route`）挂载，接口前缀 `/api/ext/astrbot_base`。功能：

- **已安装**：列出各插件（版本、指令数、启用开关），可启停、设指令前缀、编辑配置、卸载；
- **安装插件**：GitHub 仓库 URL / `.zip` 直链下载安装，或上传本地 zip；安装后自动补装 `requirements.txt` 并热重载；
- **指令正则**：查看基座已注册的全部指令与其匹配正则（装/卸插件后自动更新）；
- **日志**：查看基座运行日志（可自动刷新 / 清空）。

## 配置

面板改动都会写入 `data/config.json`（首次加载自动生成），也可直接编辑：

| 键 | 说明 |
|---|---|
| `command_prefixes` | `{插件目录名: 指令前缀}`，给某插件所有指令加前缀，避免冲突 |
| `disabled_apps` | 停用的插件目录名列表（不加载、不注册指令） |
| `apps` | `{插件目录名: {配置覆盖}}`，覆盖各 AstrBot 插件 `_conf_schema.json` 的配置项 |

## 冒烟自测

装进 ElainaBot-Onebot 后，从框架根目录运行：

```bash
python plugins/astr基座/tests/functional_test.py
```

测试会临时把 `tests/fixtures/hello_astr` 装入 `apps/`，跑 4 条收发闭环用例后自动清理（不污染出厂空 `apps/`）。
