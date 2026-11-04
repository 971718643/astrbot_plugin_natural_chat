# 测试参考代码

本目录只包含用于离线集成测试的代码快照，不含配置、密钥、聊天记录或运行数据。

- `native_result_decorate.py`、`native_scheduler.py`：AstrBot v4.29.1 的结果装饰与调度阶段代码快照。上游为 https://github.com/AstrBotDevs/AstrBot ，按 AGPL-3.0 分发。
- `iris_cron.py`：基于 Iris Chat Memory 0.3.0 的本地 CronAdapter 适配版本，用于验证私聊/群聊归属。上游为 https://github.com/Leafliber/astrbot_plugin_iris_chat_memory ，按 AGPL-3.0 分发。
- `voice_fallback.py`：本地语音文字回退实现的测试参考副本，不作为此插件的运行入口安装。

前述 AGPL 参考代码的许可文本保留于 `LICENSE-AGPL-3.0.txt`。测试通过 AST 提取所需方法，或为边界提供 mock；不会加载真实 QQ 平台、调用模型或发送消息。
