# CLAUDE.md — 前端

本文件为 Claude Code 提供 `frontend/` 的工作指南，只在处理本目录下的文件时加载。

> 2026-10-09 从项目根 `CLAUDE.md` 的「开发注意事项」迁出——这三条只涉及 Vue 组件与 SSE 消费端，
> 在后端工作时不会被读到。根文件仍保留前后端共享的约束（如 SSE 空闲超时契约）。

## 流式与等待文案

- **前端流式与竞态**：SSE 解析统一走 `api/index.ts` 的 `consumeSSE`（分隔符按 `\r\n\r\n|\n\n|\r\r`、多行 `data:` 拼接、校验 `content-type` 含 `text/event-stream`、解析失败至少 `console.warn`、**空闲**超时而非总时长超时）；`HomeView.vue` 的每轮流回调经 `streamSeq` 守卫、会话加载经 `sessionLoadSeq` 守卫，切会话/新建/删除/卸载都先 `cancelActiveStream()`，`messages.value[idx]` 一律做存在性判断（否则流式中途新建对话会 `TypeError` 白屏）。
- **`session_id` 的生成必须带兜底**：`crypto.randomUUID` 只在安全上下文（HTTPS / localhost）可用，`http://<公网IP>` 下是 `undefined`，直接调用抛 TypeError。它发生在 `restoreSession()` 里，异常会让 `onMounted` 余下的 `refreshSessions()` 一起跳过，且 `sessionId` 恒为空串 —— 请求仍正常发出、后端跳过持久化（`request.session_id` 为假），界面毫无异常，只是「历史会话里一条都没有」。故 `HomeView.vue` 用 `randomUUID()` 包装：优先 `crypto.randomUUID`，否则用非安全上下文可用的 `crypto.getRandomValues` 手写 UUID v4（保留 `Math.random` 末级兜底），`localStorage.setItem` 也包 try/catch（隐私模式）。
- **报告阶段的进度要单独开位**（前端 `Message.reportPending` / `reportProgress`）：`onResult` 会把 `loading` 置 false，而 loading 块受 `v-if="msg.loading"` 守卫，`result` 与 `report` 之间报告 agent 还要跑多轮 LLM——**不单独标识的话这段时间界面上没有任何指示**。进度文案还必须在 `HomeView.vue` 的 `finally` 里兜底清空，不能只在 `onReport`/`onResult` 里清：报告被关闭（`REPORT_ENABLED=false`）、报告节点失败静默跳过、或用户中途点「停止」时都不会再有后续事件，占位块会永久停在「正在生成决策报告…」。
- **前端等待文案：`MessagePhase` 与 `MessageKind` 正交，不要合并**。`MessageKind`（`query`/`chat`）同时是渲染分区守卫与 `QueryResponse.kind` 的前后端契约，`HomeView.vue` 的错误路径会把 `current.kind` 直接写进 `QueryResponse` —— 把 `retrieval`/`schema` 混进 `MessageKind` 会让这两个值经错误路径漏进结果载荷与历史回放。故等待文案走独立的 `Message.phase`（`api/index.ts` 的 `asMessagePhase` 从 intent 事件归一，`redraw`/未知值 → `query`，**`analysis` 原样透传**——它一次 SQL 都不生成，压成 `query` 会让整段数据分析全程显示「正在生成 SQL…」），只有 `MessageList.vue` 的 `loadingText()` 读它。**兜底必须是中性的「正在思考…」**：意图分类是一次 2~5s 的同步 LLM 往返，`intent` 事件到达前 `phase` 一直是 `undefined`，早前按「非 chat 即 SQL」兜底，导致包括文档问答在内**每一次**提问的前几秒都在显示「正在生成 SQL…」。同理 `graph.py` 的 `frontend_intent` 不再把 `retrieval`/`schema` 压平成 `chat`。
