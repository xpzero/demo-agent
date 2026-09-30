import assert from "node:assert/strict";
import { test } from "node:test";
import { readSse } from "../src/adapter/transport.ts";

function stream(...parts) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const part of parts) {
        controller.enqueue(encoder.encode(part));
      }
      controller.close();
    },
  });
}

const frame = (event) => `data: ${JSON.stringify(event)}\n\n`;

test("完整回复以 done 正常结束", async () => {
  const events = await Array.fromAsync(readSse(stream(
    frame({ type: "user_message", message_id: 1 }),
    frame({ type: "text_delta", text: "你好" }),
    frame({ type: "done", content: "你好", message_id: 2 }),
  )));
  assert.deepEqual(events.map((event) => event.type), ["user_message", "text_delta", "done"]);
});

test("error 是合法终止事件", async () => {
  const events = await Array.fromAsync(readSse(stream(frame({ type: "error", message: "失败" }))));
  assert.equal(events[0].message, "失败");
});

test("完整帧后缺少终止事件也视为中断", async () => {
  await assert.rejects(
    Array.fromAsync(readSse(stream(frame({ type: "user_message", message_id: 1 })))),
    /完成前中断/,
  );
});

test("半帧在 EOF 时被拒绝", async () => {
  await assert.rejects(
    Array.fromAsync(readSse(stream('data: {"type":"done"'))),
    /不完整的消息帧/,
  );
});

test("终止事件后的多余事件被拒绝", async () => {
  await assert.rejects(
    Array.fromAsync(readSse(stream(
      frame({ type: "done", content: "ok", message_id: 1 }),
      frame({ type: "text_delta", text: "late" }),
    ))),
    /结束后仍有事件/,
  );
});
