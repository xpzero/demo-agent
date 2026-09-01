import type {
  ThreadAssistantMessagePart,
  ThreadMessageLike,
} from "@assistant-ui/react";

const previewMode = import.meta.env.DEV
  ? new URLSearchParams(window.location.search).get("preview")
  : null;

const isToolPreview = [
  "tools-running",
  "tools-complete",
  "tools-expanded",
].includes(previewMode ?? "");
const isPermissionPreview = [
  "permission-allow",
  "permission-ask",
  "permission-approved",
  "permission-deny",
].includes(previewMode ?? "");
const isComplete = previewMode !== "tools-running";

export const expandPreviewTools =
  previewMode === "tools-expanded" || isPermissionPreview;

const assistantContent: ThreadAssistantMessagePart[] = [
  {
    type: "tool-call",
    toolCallId: "preview-weather",
    toolName: "get_weather",
    args: { city: "北京" },
    argsText: '{\n  "city": "北京"\n}',
    result: "北京今天晴，最高气温38℃",
  },
  {
    type: "tool-call",
    toolCallId: "preview-calculation",
    toolName: "calculate",
    args: { expression: "(38-12)*3" },
    argsText: '{\n  "expression": "(38-12)*3"\n}',
    ...(isComplete && { result: "78" }),
  },
  ...(isComplete
    ? [
        {
          type: "text" as const,
          text: "北京今天晴，最高气温38℃。\n\n(38-12)*3 的计算结果是 78。",
        },
      ]
    : []),
];

const permissionMessages: Record<string, ThreadMessageLike[]> = {
  "permission-allow": [
    {
      role: "user",
      content: [
        {
          type: "text",
          text: "请使用 read_file 读取 notes/permission-read-demo.txt。",
        },
      ],
    },
    {
      role: "assistant",
      content: [
        {
          type: "tool-call",
          toolCallId: "preview-read-allow",
          toolName: "read_file",
          args: { path: "notes/permission-read-demo.txt" },
          argsText: '{\n  "path": "notes/permission-read-demo.txt"\n}',
          result: "这是允许自动读取的演示内容。",
        },
        {
          type: "text",
          text: "文件内容是：这是允许自动读取的演示内容。",
        },
      ],
      status: { type: "complete", reason: "stop" },
    },
  ],
  "permission-ask": [
    {
      role: "user",
      content: [
        {
          type: "text",
          text: "请使用 write_file，把 notes/approval-demo.txt 的完整内容改成：这是经过用户批准后写入的内容。",
        },
      ],
    },
    {
      role: "assistant",
      content: [
        {
          type: "tool-call",
          toolCallId: "preview-write-ask",
          toolName: "write_file",
          args: {
            path: "notes/approval-demo.txt",
            content: "这是经过用户批准后写入的内容。",
          },
          argsText:
            '{\n  "path": "notes/approval-demo.txt",\n  "content": "这是经过用户批准后写入的内容。"\n}',
          artifact: {
            type: "code_diff",
            path: "notes/approval-demo.txt",
            additions: 1,
            deletions: 1,
            lines: [
              { kind: "removed", text: "原始内容：请不要自动覆盖" },
              { kind: "added", text: "这是经过用户批准后写入的内容。" },
            ],
          },
          approval: { id: "preview-write-ask" },
        },
      ],
      status: { type: "requires-action", reason: "tool-calls" },
    },
  ],
  "permission-approved": [
    {
      role: "user",
      content: [
        {
          type: "text",
          text: "请使用 write_file，把 notes/approval-demo.txt 的完整内容改成：这是经过用户批准后写入的内容。",
        },
      ],
    },
    {
      role: "assistant",
      content: [
        {
          type: "tool-call",
          toolCallId: "preview-write-approved",
          toolName: "write_file",
          args: {
            path: "notes/approval-demo.txt",
            content: "这是经过用户批准后写入的内容。",
          },
          argsText:
            '{\n  "path": "notes/approval-demo.txt",\n  "content": "这是经过用户批准后写入的内容。"\n}',
          artifact: {
            type: "code_diff",
            path: "notes/approval-demo.txt",
            additions: 1,
            deletions: 1,
            lines: [
              { kind: "removed", text: "原始内容：请不要自动覆盖" },
              { kind: "added", text: "这是经过用户批准后写入的内容。" },
            ],
          },
          approval: { id: "preview-write-approved", approved: true },
          result: "已写入 notes/approval-demo.txt（15 字符）",
        },
        { type: "text", text: "文件已按批准的内容写入。" },
      ],
      status: { type: "complete", reason: "stop" },
    },
  ],
  "permission-deny": [
    {
      role: "user",
      content: [
        {
          type: "text",
          text: "请使用 write_file 覆盖 notes/permission-deny-demo.txt。",
        },
      ],
    },
    {
      role: "assistant",
      content: [
        {
          type: "tool-call",
          toolCallId: "preview-write-denied",
          toolName: "write_file",
          args: {
            path: "notes/permission-deny-demo.txt",
            content: "这段内容不应该写入。",
          },
          argsText:
            '{\n  "path": "notes/permission-deny-demo.txt",\n  "content": "这段内容不应该写入。"\n}',
          result:
            "权限规则拒绝执行工具 write_file：匹配 permission.json 规则：write notes/permission-deny-demo.txt；工具未执行。",
          isError: true,
        },
        { type: "text", text: "权限规则禁止了这次写入，文件没有修改。" },
      ],
      status: { type: "complete", reason: "stop" },
    },
  ],
};

export const previewMessages: ThreadMessageLike[] = isToolPreview
  ? [
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "北京今天天气怎么样？再算一下 (38-12)*3。",
          },
        ],
      },
      {
        role: "assistant",
        content: assistantContent,
        status: isComplete
          ? { type: "complete", reason: "stop" }
          : { type: "running" },
      },
    ]
  : isPermissionPreview
    ? permissionMessages[previewMode ?? ""]
    : [];
