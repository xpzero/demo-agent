import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useLocalRuntime,
} from "@assistant-ui/react";

import { chatAdapter } from "./adapter";
import "./App.css";

const UserMessage = () => (
  <MessagePrimitive.Root className="message user">
    <div className="bubble">
      <MessagePrimitive.Parts />
    </div>
  </MessagePrimitive.Root>
);

const AssistantMessage = () => (
  <MessagePrimitive.Root className="message assistant">
    <div className="bubble">
      <MessagePrimitive.Parts />
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

export default function App() {
  const runtime = useLocalRuntime(chatAdapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  );
}
