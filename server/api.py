import json
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import SYSTEM_PROMPT, stream_events
from logging_setup import setup_logging

# uvicorn 先配好自己的日志再导入本模块，放在这里设置才不会被它覆盖
setup_logging()

app = FastAPI(title="demo-agent")

# vite dev server 跑在 5173，跨端口即跨源，不放行浏览器会直接拦掉请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 单会话上下文，只存在内存里，后端重启即清空
items: list = [{"role": "system", "content": SYSTEM_PROMPT}]
_running = False
_lock = Lock()


class ChatRequest(BaseModel):
    message: str


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
def chat(body: ChatRequest):
    global _running
    with _lock:
        if _running:
            raise HTTPException(status_code=409, detail="已有任务正在运行")
        _running = True
        items.append({"role": "user", "content": body.message})

    def sse():
        global _running
        print(json.dumps(items, ensure_ascii=False, indent=2), flush=True)
        try:
            for event in stream_events(items):
                yield _sse(event)
        finally:
            with _lock:
                _running = False
            print(json.dumps(items, ensure_ascii=False, indent=2), flush=True)

    return StreamingResponse(sse(), media_type="text/event-stream")
