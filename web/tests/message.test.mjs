import assert from "node:assert/strict";
import { test } from "node:test";
import { getMessageText } from "../src/chat/message.ts";

test("提取原始消息内容而非工具事件或状态文本", () => {
  assert.equal(getMessageText({ kind: "user", id: "user", text: "消息\n正文", createdAt: 1 }), "消息\n正文");
  assert.equal(getMessageText({ kind: "assistant", id: "assistant", createdAt: 2, events: [
    { type: "tool_call", id: "1", name: "search", args: {} },
    { type: "text_delta", text: "回" },
    { type: "text_delta", text: "复" },
    { type: "done", content: "回复", message_id: 5 },
  ] }), "回复");
  assert.equal(getMessageText({ kind: "assistant", id: "pending", createdAt: 3, events: [] }), "");
});

test("流式文本可在回复完成前提取，空完成事件不覆盖正文", () => {
  assert.equal(getMessageText({ kind: "assistant", id: "partial", createdAt: 2, events: [
    { type: "text_delta", text: "部分" },
    { type: "done", content: "", message_id: 5 },
  ] }), "部分");
});
