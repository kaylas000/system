# specs/06_ops/hitl/HITL_API.py
"""
FastAPI Service for Human-in-the-Loop (HITL) Interactions.
Endpoints: Get Interrupt, Submit Approval, Stream Logs.
"""

from __future__ import annotations
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing: Dict, List, Any, Optional, Literal
from contextlib import asynccontextmanager
import asyncio
import json
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from kernel.state import AgentState, RunStatus
from kernel.config import settings

# --- Models ---
class InterruptPayload(BaseModel):
    run_id: str
    interrupt_type: Literal["plan_review", "gate_failure", "destructive_action", "budget_exceeded"]
    payload: Dict[str, Any]
    actions: List[Literal["approve", "reject", "edit", "abort", "skip_gate"]]
    created_at: str

class ApprovalRequest(BaseModel):
    action: Literal["approve", "reject", "edit", "abort", "skip_gate"]
    edited_data: Optional[Dict[str, Any]] = None # For 'edit' action
    comment: Optional[str] = None

class RunStatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    progress: float # 0.0 - 1.0
    current_node: Optional[str]
    logs: List[str]

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, run_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(run_id, []).append(websocket)

    def disconnect(self, run_id: str, websocket: WebSocket):
        if run_id in self.active_connections:
            self.active_connections[run_id].remove(websocket)

    async def broadcast(self, run_id: str, message: Dict):
        if run_id in self.active_connections:
            dead = []
            for ws in self.active_connections[run_id]:
                try:
                    await ws.send_json(message)
                except:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(run_id, ws)

manager = ConnectionManager()

# --- App Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Checkpointer
    app.state.checkpointer = PostgresSaver.from_conn_string(settings.database.postgres_dsn)
    yield
    # Shutdown
    pass

app = FastAPI(title="AutoGen HITL API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- REST Endpoints ---
@app.get("/runs/{run_id}/interrupt", response_model=InterruptPayload)
async def get_interrupt(run_id: str):
    """Fetch current interrupt payload for a run."""
    # Check if graph is interrupted
    config = {"configurable": {"thread_id": run_id}}
    state_snapshot = await app.state.checkpointer.aget_tuple(config)
    if not state_snapshot or not state_snapshot.metadata.get("interrupts"):
        raise HTTPException(404, "No active interrupt")
    
    interrupt = state_snapshot.metadata["interrupts"][-1] # Latest
    return InterruptPayload(**interrupt.value)

@app.post("/runs/{run_id}/approve")
async def submit_approval(run_id: str, request: ApprovalRequest, background_tasks: BackgroundTasks):
    """Resume graph with human decision."""
    config = {"configurable": {"thread_id": run_id}}
    
    # Map action to LangGraph Command
    if request.action == "approve":
        cmd = Command(resume={"action": "approve"})
    elif request.action == "edit":
        cmd = Command(resume={"action": "edit", "data": request.edited_data})
    elif request.action == "skip_gate":
        cmd = Command(resume={"action": "skip_gate"})
    elif request.action == "abort":
        cmd = Command(resume={"action": "abort"})
    else:
        raise HTTPException(400, "Invalid action")
    
    # Resume graph in background
    background_tasks.resume_graph(run_id, cmd) # Custom background task
    
    return {"status": "resumed", "action": request.action}

@app.get("/runs/{run_id}/status", response_model=RunStatusResponse)
async def get_run_status(run_id: str):
    config = {"configurable": {"thread_id": run_id}}
    state = await app.state.checkpointer.aget(config)
    if not state:
        raise HTTPException(404, "Run not found")
    
    # Calculate progress (heuristic)
    tasks = state.get("task_graph", [])
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    progress = completed / len(tasks) if tasks else 0.0
    
    return RunStatusResponse(
        run_id=run_id,
        status=state.get("status", "unknown"),
        progress=progress,
        current_node=state.get("current_node"),
        logs=state.get("logs", [])[-50:] # Last 50 logs
    )

# --- WebSocket for Real-time Logs ---
@app.websocket("/ws/runs/{run_id}")
async def websocket_logs(websocket: WebSocket, run_id: str):
    await manager.connect(run_id, websocket)
    try:
        # Send current state immediately
        state = await app.state.checkpointer.aget({"configurable": {"thread_id": run_id}})
        if state:
            await websocket.send_json({"type": "state", "data": state})
        
        # Keep alive, listen for client messages (ping)
        while True:
            data = await websocket.receive_text()
            if data == "ping": await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(run_id, websocket)

# --- Internal Helper for Nodes to Stream Logs ---
async def stream_log(run_id: str, level: str, message: str, node: str = ""):
    """Called by Kernel Nodes to push logs to UI."""
    log_entry = {"timestamp": asyncio.get_event_loop().time(), "level": level, "node": node, "message": message}
    await manager.broadcast(run_id, {"type": "log", "data": log_entry})

# --- Background Task to Resume Graph ---
async def resume_graph_task(run_id: str, command: Command):
    from kernel.graph.builder import build_graph
    from kernel.vertical.loader import VerticalLoader
    # Reconstruct graph (or get from pool)
    # This is simplified; production needs a persistent Graph Runner Pool
    vertical = VerticalLoader(Path("verticals")).load_vertical("saas_web") # Dynamic lookup needed
    graph = build_graph(vertical)
    await graph.ainvoke(command, config={"configurable": {"thread_id": run_id}})
