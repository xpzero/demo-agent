import assert from "node:assert/strict";
import { test } from "node:test";
import { deriveSegments, isAwaitingReply } from "../src/components/ui/chat-messages/utils/segments.ts";

test("工具完成到下一段正文之前显示等待；正文、终止或历史回放后停止", () => {
  const call = { type: "tool_call", id: "t1", name: "get_weather", args: {} };
  const result = { type: "tool_result", id: "t1", content: "晴", elapsed: 98 };
  assert.equal(isAwaitingReply([], false), true);
  assert.equal(isAwaitingReply([call], false), false);
  assert.equal(isAwaitingReply([call, result], false), true);
  assert.equal(isAwaitingReply([call, result, { type: "text_delta", text: "今天晴" }], false), false);
  assert.equal(isAwaitingReply([call, result, { type: "error", message: "超时" }], false), false);
  assert.equal(isAwaitingReply([call, result, { type: "max_turns" }], false), false);
  assert.equal(isAwaitingReply([call, result], true), false);
  assert.deepEqual(deriveSegments([call, result, { type: "text_delta", text: "今天晴" }]).map((segment) => segment.kind), ["tool", "text"]);
});
