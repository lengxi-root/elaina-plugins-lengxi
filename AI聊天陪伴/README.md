# AI 聊天陪伴

ElainaBot v2 聊天陪伴插件，提供猫娘、温柔伙伴、傲娇少女、理性助手等内置人格。

## 功能

- OpenAI Chat Completions 兼容协议
- 直接使用中央 AI 模块中的接口、密钥与模型清单
- 插件内可选择自动策略、指定接口或指定接口与模型
- 每个用户使用独立上下文，私聊、艾特和全量群消息模式均不会共享群上下文
- 全量群聊消息记录；未艾特消息先判断相关性，再应用概率、冷却和每小时预算
- SQLite 持久化用户独立上下文，不混入其他群成员的消息
- 人格可按会话覆盖；支持用户显式保存、查看和清除长期记忆
- 可选公网搜索与网页读取工具，内置 SSRF 防护、网页域名白名单并隐藏所有 IP
- 内置可开关 Agent 列表；模型按语境决定调用，聊天回复不展示 Agent 名称、参数与执行过程
- 内置点歌 Agent，复刻项目点歌插件的 QQ 音乐搜索、结果序号和用户缓存，并直接发送语音
- 支持可命名的陪伴资源；每项可配置用途说明、正文或公开 URL，由模型按需读取
- 独立 AI 安全分类请求分别审核用户输入与待发送回复，只返回安全或违规标记，违规输出不会进入上下文
- AI 陪伴内置头像 meme 合成，按语境临时提供工具，不依赖娱乐拓展的 meme 实现
- 独立生图旁路，可配置多个接口/模型的优先顺序并在失败时自动切换
- 可配置违规词，输入拦截、输出替换，违规原文不会进入上下文
- Web 面板管理聊天模型、生图旁路、人格、Skills、触发方式及上下文
- 群聊默认需要 @ 机器人，可在面板开启自动回复

## 命令

- `/ai`：查看状态与人格列表
- `/ai clear`：清空当前用户的独立上下文
- `/ai personality <ID>`：主人切换当前会话人格
- `/ai remember <内容>`：保存个人长期记忆
- `/ai memories`：查看个人长期记忆
- `/ai forget`：清空个人长期记忆

先安装并启用 `v2模块/ai_llm` 中的 AI LLM 服务模块，在该模块统一配置接口、API Key 与模型目录。聊天模型和生图旁路均只能从该目录选择；生图旁路按后台列表从上到下尝试 `/images/generations`，失败后继续切换模型或接口。头像 meme 和 base64 生图结果需要 `image_hosting` 的 COS 图床。

## 编写 Agent

Agent 位于 `app/agents.py`。一个 Agent 由三部分组成：

1. 在 `AGENTS` 中声明唯一 ID、后台显示名称、用途说明和 OpenAI function tool schema。工具说明应明确“什么时候调用”，参数尽量少。
2. 在 `run(name, arguments, context)` 中按工具名分派并执行。`context['event']` 可用于 `reply`、`reply_image`、`reply_voice` 等直接发送方法。
3. 所有外部输入都需要校验、限制长度并设置网络超时；失败返回 `{'ok': True, 'sent': False}`，不要把内部异常或接口信息发给用户。

最小结构：

```python
AGENTS['weather'] = {
    'id': 'weather',
    'name': '天气查询',
    'description': '用户询问天气时使用。',
    'tool': {
        'type': 'function',
        'function': {
            'name': 'agent_weather',
            'description': '查询指定城市的天气。',
            'parameters': {
                'type': 'object',
                'properties': {'city': {'type': 'string'}},
                'required': ['city'],
                'additionalProperties': False,
            },
        },
    },
}

async def run(name, arguments, context):
    if name == 'agent_weather':
        city = str(arguments.get('city') or '').strip()[:80]
        # 调用受控服务并返回简短结构化结果
        return {'ok': True, 'city': city, 'weather': '...'}
```

新增后重载插件，Agent 会自动出现在后台 Agents 页面，可独立开启或关闭。不要让 Agent 返回系统提示、密钥、服务器环境、原始异常或不必要的大段数据。
