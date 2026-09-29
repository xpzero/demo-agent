# web

前端使用 Vite + React，通过 `fetch` + `ReadableStream` 消费 `POST /api/chat` SSE。HTTP 和事件封装位于 `src/adapter/`，消息模型位于 `src/chat/`，请求及界面状态位于 `src/hooks/`。

## 会话状态与停止

- 历史接口的 `can_send_message` 决定是否允许发送。打开正在处理的会话时，自动查询 `GET /api/sessions/{id}/status`。
- SSE 意外断开后，仅在页面可见且浏览器在线时检查状态；同一时间最多一个恢复请求，隐藏或离线时取消请求，恢复可见或在线后继续检查。服务端允许发送后重新读取历史，同步消息、摘要游标及父消息 ID。状态或历史请求失败时持续重试，并保持发送禁用。恢复不会重新提交聊天请求。
- 点击「停止」调用 `POST /api/sessions/{id}/stop`。`processing` 表示停止已请求但尚未结束；`ended` 和 `no_active_request` 也需要同步历史后才能继续发送。停止失败会显示错误，并允许重试。
- 草稿和附件在聊天 POST 被接受后清空；409 或其他 HTTP 请求失败保留原输入。正常 SSE 回复保留文本与工具事件的到达顺序；断流恢复使用服务端存档历史。
- 会话切换或卸载时取消旧恢复请求，旧会话的结果不会写入当前视图。

轮询间隔通过 Vite 环境变量 `VITE_SESSION_STATUS_POLL_MS` 配置（毫秒）。默认 5000 毫秒是前端实现选择，可按服务端负载调整，实际范围限制为 2000–60000 毫秒；未配置或无效值使用默认值。修改后重启开发服务或重新构建。

## 检查

从本目录执行：

```sh
pnpm lint
pnpm build
pnpm test
```
