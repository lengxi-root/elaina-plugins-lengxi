# OneBot 群管

## 跨群批量邀请

在 Elaina_QQBot 的群管 Web 面板打开「批量邀请」：

1. 选择执行邀请的在线机器人。
2. 选择来源群和目标群，机器人需同时加入两个群。
3. 粘贴 QQ 列表，每行一个 QQ 号，然后点击「开始邀请」。

```text
123456789
234567890
345678901
```

每次最多 200 个不同的 QQ 号；空行自动忽略、重复号码自动去重。任务会先检查两个群的成员列表，再跳过不在来源群或已经在目标群的用户。其余用户逐个邀请，间隔 2 秒。面板显示进度及每个 QQ 的提交、跳过或失败原因；切换页面后任务继续，返回此页可查看本次进度。任务结果保留在内存中，重启框架后清除。

「邀请已提交」表示 QQ 邀请接口已接受请求，实际入群可能仍需对方同意或目标群管理员审批。目标群需要允许机器人邀请成员，QQ 拒绝或限制邀请时会显示具体回执。

接口使用 Elaina_QQBot 的账号目录、`get_group_member_list`、`get_stranger_info` 和 `send_packet`，所有请求显式携带所选 `self_id`。协议端需提供成员 UID 及原始发包能力。邀请包为 `OidbSvcTrpcTcp.0x758_1`，携带目标群、成员 UID 与来源群，字段依据 [Lagrange 的 GroupInviteService](https://github.com/LagrangeDev/Lagrange.Core/blob/v1/Lagrange.Core/Internal/Service/Action/GroupInviteService.cs) 和对应的 `OidbSvcTrpcTcp0x758_1` 定义；同时检查 OneBot 状态和 QQ OIDB 错误码。

单项验证（使用模拟接口，不发送真实邀请）：

```shell
python -m unittest discover -s onebot_groupguard/tests -p test_invites.py -v
```
