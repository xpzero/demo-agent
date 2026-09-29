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

test("刷新保留已建立会话，未提交草稿不占用持久会话标识", async () => {
  const values = new Map();
  const before = globalThis.window;
  globalThis.window = { sessionStorage: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  } };
  try {
    useSessionStore.getState().newSession();
    const id = useSessionStore.getState().sessionId;
    useSessionStore.getState().beginSession("draft");
    assert.equal(values.size, 0);
    useSessionStore.getState().setCurrentMessageId(12);
    assert.equal(values.get("demo-agent.active-session"), id);
    const { useSessionStore: refreshed } = await import(`../src/stores/session.ts?reload=${Date.now()}`);
    assert.equal(refreshed.getState().sessionId, id);
    assert.equal(refreshed.getState().isNewSession, false);
    refreshed.getState().newSession();
    assert.equal(values.size, 0);
  } finally {
    globalThis.window = before;
  }
});

test("每次切换或新建会话都产生新的视图代次，消息写入不改变代次", () => {
  const initial = useSessionStore.getState().sessionGeneration;
  useSessionStore.getState().newSession();
  assert.equal(useSessionStore.getState().sessionGeneration, initial + 1);
  useSessionStore.getState().setCurrentMessageId(42);
  assert.equal(useSessionStore.getState().sessionGeneration, initial + 1);

  const existingId = crypto.randomUUID();
  useSessionStore.getState().switchSession(existingId);
  assert.equal(useSessionStore.getState().sessionGeneration, initial + 2);
  useSessionStore.getState().switchSession(existingId);
  assert.equal(useSessionStore.getState().sessionGeneration, initial + 3);
});
