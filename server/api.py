import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import SYSTEM_PROMPT, stream_events
from sessions import SessionManager

app = FastAPI(title="demo-agent")

# vite dev server 跑在 5173，跨端口即跨源，不放行浏览器会直接拦掉请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 单进程单例，与 CLI 共用同一套 .sessions/ 数据。
# 注意：current 指针是共享状态，暂不支持并发请求（单 worker 顺序处理够用）
manager = SessionManager(SYSTEM_PROMPT)


class ChatRequest(BaseModel):
    message: str


@app.get("/api/sessions")
def list_sessions():
    return [
        {
            "id": session.id,
            "summary": session.summary,
            "message_count": len(session.messages),
            "current": is_current,
        }
        for is_current, session in manager.listing()
    ]


@app.post("/api/sessions")
def create_session():
    session = manager.new()
    return {"id": session.id}


@app.post("/api/sessions/{session_id}/chat")
def chat(session_id: int, body: ChatRequest):
    if not manager.switch(session_id):
        raise HTTPException(status_code=404, detail=f"没有 {session_id} 号会话")

    messages = manager.current.messages
    messages.append({"role": "user", "content": body.message})

    def sse():
        for event in stream_events(messages):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        # 流走完时配对一定完整，此刻落盘（与 CLI 的时机一致）
        manager.save_current()

    return StreamingResponse(sse(), media_type="text/event-stream")
