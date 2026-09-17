# QQ 官方文档更新监控

按配置的分钟间隔读取 QQ 开放平台官方 sitemap.xml，检测开发文档页面的新增、修改和删除。

## 行为

- 首次加载会抓取全部开发文档并建立基线，不发送历史页面通知。
- 后续每次只抓取 sitemap 标记为新增或更新时间变化的页面。
- 发生更新时，向每个已启动机器人的全部 `owner_ids` 主动私聊，并向订阅群主动发送；每个目标只发送一条消息，其中包含本次所有页面链接和正文差异。
- 订阅群按收到设置命令的机器人 `appid` 分组保存，同一个群在不同机器人下互不混用。
- 通知未全部发送成功时不提交新快照；发送进度记录在 `data/delivery.json`，下次只重试失败的目标，已经成功的目标不会重复收到同一批更新。

运行后自动生成 `data/config.yaml`：

```yaml
enabled: true
interval_minutes: 5
request_timeout_seconds: 30
concurrency: 8
```

interval_minutes 最低为 1 分钟（兼容旧版 interval_seconds 配置）。快照保存在 data/state.json，群订阅保存在 data/subscriptions.json。

主人命令：

- `文档监控状态`
- `开放订阅群状态`（状态命令别名）
- `立即检查文档`
- `设置开放订阅群`（仅群内使用，绑定当前机器人 appid）
- `取消开放订阅群`（仅群内使用）
