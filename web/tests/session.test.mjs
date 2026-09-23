import assert from "node:assert/strict";
import { test } from "node:test";
import { useSessionStore } from "../src/stores/session.ts";

test("未发送的新会话不生成侧边栏记录，提交后出现临时标题", () => {
  useSessionStore.getState().newSession();
  const sessionId = useSessionStore.getState().sessionId;
  assert.equal(useSessionStore.getState().pendingSession, null);

  const pending = useSessionStore.getState().beginSession("  第一条消息  ");
  assert.deepEqual(pending, { id: sessionId, title: "第一条消息" });
  assert.equal(useSessionStore.getState().pendingSession, pending);

  useSessionStore.getState().setCurrentMessageId(42);
  assert.equal(useSessionStore.getState().isNewSession, false);
  assert.equal(useSessionStore.getState().pendingSession, pending);
  assert.equal(useSessionStore.getState().beginSession("后续消息"), null);

  useSessionStore.getState().switchSession(crypto.randomUUID());
  assert.equal(useSessionStore.getState().pendingSession, null);
});

test("失败请求只移除自己的临时条目，不影响重试或新会话", () => {
  useSessionStore.getState().newSession();
  const first = useSessionStore.getState().beginSession("第一个请求");
  const second = useSessionStore.getState().beginSession("重试");
  useSessionStore.getState().discardPendingSession(first);
  assert.equal(useSessionStore.getState().pendingSession, second);
  useSessionStore.getState().discardPendingSession(second);
  assert.equal(useSessionStore.getState().pendingSession, null);

  useSessionStore.getState().newSession();
  assert.equal(useSessionStore.getState().pendingSession, null);
  const next = useSessionStore.getState().beginSession("新对话");
  assert.notEqual(next.id, second.id);
  assert.equal(useSessionStore.getState().pendingSession, next);
});
