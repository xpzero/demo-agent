import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  groupPartByType,
  MessagePrimitive,
  ThreadPrimitive,
  useLocalRuntime,
} from "@assistant-ui/react";
import type {
  TextMessagePartProps,
  ThreadMessageLike,
} from "@assistant-ui/react";
import { useEffect, useRef, useState } from "react";

import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from "@/components/assistant-ui/elements/tool-group.aui";
import {
  chatAdapter,
  loadPendingMessages,
  type InitialSessionState,
} from "./adapter";
import "./App.css";
import { expandPreviewTools, previewMessages } from "./preview";
import { ToolCallPart } from "./ToolCallPart";

const UserMessage = () => (
  <MessagePrimitive.Root className="message user">
    <div className="bubble">
      <MessagePrimitive.Parts />
    </div>
  </MessagePrimitive.Root>
);

const TextPart = ({ text }: TextMessagePartProps) =>
  text ? <p>{text}</p> : null;

const AssistantMessage = () => (
  <MessagePrimitive.Root className="message assistant">
    <div className="bubble">
      <MessagePrimitive.GroupedParts
        groupBy={groupPartByType({ "tool-call": ["group-tool"] })}
      >
        {({ part, children }) => {
          switch (part.type) {
            case "group-tool":
              return (
                <ToolGroupRoot
                  variant="ghost"
                  defaultOpen={
                    expandPreviewTools || part.status.type !== "complete"
                  }
                >
                  <ToolGroupTrigger
                    count={part.indices.length}
                    active={part.status.type === "running"}
                  />
                  <ToolGroupContent>{children}</ToolGroupContent>
                </ToolGroupRoot>
              );
            case "text":
              return <TextPart {...part} />;
            case "tool-call":
              return part.toolUI ?? <ToolCallPart {...part} />;
            case "indicator":
              return null;
            default:
              return null;
          }
        }}
      </MessagePrimitive.GroupedParts>
    </div>
  </MessagePrimitive.Root>
);

const Thread = () => (
  <ThreadPrimitive.Root className="thread">
    <ThreadPrimitive.Viewport className="viewport">
      <ThreadPrimitive.Empty>
        <p className="empty">问点什么吧，比如「北京天气？再算 (38-12)*3」</p>
      </ThreadPrimitive.Empty>
      <ThreadPrimitive.Messages
        components={{ UserMessage, AssistantMessage }}
      />
      <ThreadPrimitive.If running>
        <p className="thinking">思考中…</p>
      </ThreadPrimitive.If>
    </ThreadPrimitive.Viewport>
    <ComposerPrimitive.Root className="composer">
      <ComposerPrimitive.Input className="input" placeholder="输入消息，回车发送" />
      <ComposerPrimitive.Send className="send">发送</ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  </ThreadPrimitive.Root>
);

function RuntimeApp({
  initialMessages,
  resumeOnLoad,
}: {
  initialMessages: ThreadMessageLike[];
  resumeOnLoad: boolean;
}) {
  const runtime = useLocalRuntime(chatAdapter, {
    initialMessages,
  });
  const resumed = useRef(false);

  useEffect(() => {
    if (!resumeOnLoad || resumed.current) return;
    resumed.current = true;
    const parentId = runtime.thread.getState().messages.at(-1)?.id ?? null;
    runtime.thread.startRun({ parentId });
  }, [resumeOnLoad, runtime]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  );
}

export default function App() {
  const [initialState, setInitialState] = useState<InitialSessionState | null>(
    previewMessages.length
      ? { messages: previewMessages, resumeOnLoad: false }
      : null,
  );
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (initialState !== null) return;
    loadPendingMessages()
      .then(setInitialState)
      .catch(error => {
        setLoadError(error instanceof Error ? error.message : String(error));
      });
  }, [initialState]);

  if (loadError) {
    return <p className="empty">恢复会话失败：{loadError}</p>;
  }
  if (initialState === null) {
    return <p className="empty">正在恢复会话…</p>;
  }

  return (
    <RuntimeApp
      initialMessages={initialState.messages}
      resumeOnLoad={initialState.resumeOnLoad}
    />
  );
}
