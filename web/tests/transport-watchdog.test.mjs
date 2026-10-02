import assert from "node:assert/strict";
import { test } from "node:test";
import { readSse } from "../src/adapter/transport.ts";

// 说明：本项目 node --experimental-strip-types 只 strip 被导入的 .ts，
// 测试文件本身必须是纯 JS（无类型注解）。

// 挂死流：连接开着但永不发字节（模拟半死连接）
function stalledStream() {
  return new ReadableStream({
    start() {},
  });
}

// 正常流：两帧后正常关闭
function normalStream() {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('data: {"type":"user_message","message_id":1}\n\n'));
      controller.enqueue(encoder.encode('data: {"type":"done","message_id":2}\n\n'));
      controller.close();
    },
  });
}

test("readSse 正常流：帧解析不受看门狗影响", async () => {
  const events = [];
  for await (const event of readSse(normalStream())) {
    events.push(event);
  }
  assert.equal(events.length, 2);
  assert.equal(events[1].type, "done");
});

test("readSse 看门狗：挂死流在 30s 上限抛错，不永久挂起", async () => {
  const start = Date.now();
  await assert.rejects(
    async () => {
      for await (const event of readSse(stalledStream())) {
        void event;
      }
    },
    (error) => {
      assert.ok(error instanceof Error);
      assert.ok(
        error.message.includes("连接超时") || error.message.includes("中断"),
        `unexpected message: ${error.message}`,
      );
      return true;
    },
  );
  const elapsed = Date.now() - start;
  // 看门狗生效：约 30s 处 reject（允许 29–45s 窗口）；失效则测试超时器杀掉
  assert.ok(elapsed >= 29_000 && elapsed <= 45_000, `elapsed=${elapsed}`);
}, 60_000);
