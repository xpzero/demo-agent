import assert from "node:assert/strict";
import { test } from "node:test";
import { appendPendingTurn, applyChatEvent } from "../src/chat/messageUpdates.ts";

const ids = { userId: "local-user", assistantId: "local-assistant" };

test("乐观消息沿用稳定 UI ID，服务端 ID 与时间随事件更新", () => {
  const pending = appendPendingTurn([], "你好", ids, 10);
  assert.deepEqual(pending, [
    { kind: "user", id: ids.userId, text: "你好", createdAt: 10 },
    { kind: "assistant", id: ids.assistantId, events: [], createdAt: 10 },
  ]);
  const userSaved = applyChatEvent(pending, { type: "user_message", message_id: 1 }, ids, 11);
  const streaming = applyChatEvent(userSaved, { type: "text_delta", text: "你好" }, ids, 12);
  const done = applyChatEvent(streaming, { type: "done", content: "你好", message_id: 2 }, ids, 13);
  assert.equal(done[0].id, ids.userId);
  assert.equal(done[0].messageId, 1);
  assert.deepEqual(done[1], {
    kind: "assistant", id: ids.assistantId, createdAt: 13, messageId: 2,
    events: [
      { type: "user_message", message_id: 1 },
      { type: "text_delta", text: "你好" },
      { type: "done", content: "你好", message_id: 2 },
    ],
  });
  assert.equal(pending[1].events.length, 0);
});

test("空回复和错误保留现有事件展示而不制造助手消息 ID", () => {
  const pending = appendPendingTurn([], "问题", ids, 10);
  const empty = applyChatEvent(pending, { type: "done", content: "", message_id: 1 }, ids, 11);
  assert.equal(empty[1].messageId, undefined);
  assert.equal(empty[1].createdAt, 10);
  const error = applyChatEvent(pending, { type: "error", message: "中断" }, ids, 12);
  assert.deepEqual(error[1].events, [{ type: "error", message: "中断" }]);
  assert.equal(pending[1].events.length, 0);
});

test("迟到事件不会写入另一轮消息", () => {
  const other = appendPendingTurn([], "后续问题", { userId: "next-user", assistantId: "next-assistant" }, 20);
  assert.deepEqual(applyChatEvent(other, { type: "done", content: "旧回复", message_id: 2 }, ids, 21), other);
});
