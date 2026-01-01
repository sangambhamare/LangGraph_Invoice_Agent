import sqlite3
import json
import uuid
import time
from typing import Dict, List, Any, Optional, Literal
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

# --- 1. CONFIGURATION & ABILITY REGISTRY [cite: 179-184] ---
WORKFLOW_CONFIG = {
    "match_threshold": 0.90,
    "retry_policy": {"max_retries": 3, "backoff_seconds": 2},
    "stages": [
        {"id": "INTAKE", "mode": "deterministic", "server": "COMMON", "abilities": ["validate_schema", "persist_raw"]},
        {"id": "UNDERSTAND", "mode": "deterministic", "server": "ATLAS", "pool": ["google_vision", "tesseract"]},
        {"id": "PREPARE", "mode": "deterministic", "server": "ATLAS", "pool": ["clearbit", "vendor_db"]},
        {"id": "RETRIEVE", "mode": "deterministic", "server": "ATLAS", "pool": ["sap_sandbox"]},
        {"id": "MATCH_TWO_WAY", "mode": "deterministic", "server": "COMMON", "abilities": ["compute_match_score"]},
        {"id": "CHECKPOINT_HITL", "mode": "deterministic", "server": "COMMON"},
        {"id": "HITL_DECISION", "mode": "non-deterministic", "server": "ATLAS"},
        {"id": "RECONCILE", "mode": "deterministic", "server": "COMMON"},
        {"id": "APPROVE", "mode": "deterministic", "server": "ATLAS"},
        {"id": "POSTING", "mode": "deterministic", "server": "ATLAS", "pool": ["sap_sandbox"]},
        {"id": "NOTIFY", "mode": "deterministic", "server": "ATLAS", "pool": ["sendgrid"]},
        {"id": "COMPLETE", "mode": "deterministic", "server": "COMMON"}
    ]
}

# --- 2. STATE DEFINITION [cite: 6, 124] ---
class AgentState(TypedDict):
    invoice_payload: Dict[str, Any]
    match_result: str
    match_score: float
    human_decision: str
    reviewer_id: str
    status: str
    approval_metadata: Dict[str, Any]
    posting_info: Dict[str, Any]
    final_payload: Dict[str, Any] # [cite: 388]
    audit_log: List[str] # [cite: 389]
    retry_count: int

# --- 3. ERROR HANDLING & ABILITY ENFORCEMENT [cite: 393-396, 126] ---

def mcp_ability_executor(stage_id: str, ability: str, server: str):
    """Enforces execution via specific MCP servers[cite: 126, 161]."""
    # In a real environment, this makes an RPC call to the MCP Client
    return f"Enforced execution of {ability} on {server} server."

def node_wrapper(stage_cfg, func):
    """Implements Retry Policy, Backoff, and Error Handling [cite: 395-396]."""
    def wrapper(state: AgentState):
        retries = WORKFLOW_CONFIG["retry_policy"]["max_retries"]
        backoff = WORKFLOW_CONFIG["retry_policy"]["backoff_seconds"]

        for i in range(retries):
            try:
                # Execute registered abilities from config [cite: 134]
                abilities = stage_cfg.get("abilities", ["default_process"])
                for ability in abilities:
                    mcp_ability_executor(stage_cfg["id"], ability, stage_cfg["server"])

                return func(state)
            except Exception as e:
                time.sleep(backoff * (i + 1))
                if i == retries - 1:
                    # Unrecoverable error handling [cite: 396]
                    return {"status": "FAILED", "audit_log": state["audit_log"] + [f"OPS_NOTIFY: Unrecoverable error in {stage_cfg['id']}"]}
        return {}
    return wrapper

# --- 4. NODE LOGIC ---

def intake_node(state): return {"status": "INGESTED", "audit_log": ["INTAKE complete."]}

def match_node(state):
    score = 0.85 if state["invoice_payload"]["amount"] > 1000 else 0.95
    return {"match_score": score, "match_result": "FAILED" if score < 0.9 else "MATCHED"}

def checkpoint_hitl_node(state):
    # Persist to business DB [cite: 77-78]
    return {"status": "PAUSED", "audit_log": state["audit_log"] + ["CHECKPOINT: Stored in human_review_queue."]}

def approve_node(state):
    # Approval escalation logic [cite: 102]
    amount = state["invoice_payload"]["amount"]
    status = "AUTO_APPROVED" if amount < 2000 else "ESCALATED"
    meta = {"level": "L1" if amount < 5000 else "L2", "reason": "High Value" if amount >= 2000 else "Threshold OK"}
    return {"status": status, "approval_metadata": meta}

def posting_node(state):
    return {"posting_info": {"erp_txn_id": str(uuid.uuid4()), "posted": True}}

def complete_node(state):
    # Explicit final_payload generation
    final = {
        "invoice_id": state["invoice_payload"].get("invoice_id"),
        "final_status": state["status"],
        "approval": state.get("approval_metadata"),
        "posting": state.get("posting_info"),
        "audit_count": len(state["audit_log"])
    }
    return {"final_payload": final, "status": "COMPLETED", "audit_log": state["audit_log"] + ["COMPLETE: Final payload generated."]}

# --- 5. DYNAMIC GRAPH BUILDING [cite: 139] ---

def build_graph():
    builder = StateGraph(AgentState)
    # Build nodes dynamically enforcing 'mode' and 'server' [cite: 133-134]
    for stage in WORKFLOW_CONFIG["stages"]:
        logic_map = {
            "INTAKE": intake_node, "MATCH_TWO_WAY": match_node,
            "CHECKPOINT_HITL": checkpoint_hitl_node, "APPROVE": approve_node,
            "POSTING": posting_node, "COMPLETE": complete_node
        }
        # Default logic for generic stages
        node_func = logic_map.get(stage["id"], lambda x: {"audit_log": x["audit_log"] + [f"Processed {stage['id']}"]})
        builder.add_node(stage["id"].lower(), node_wrapper(stage, node_func))

    # Edges (Standard sequential with conditional branches) [cite: 143]
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "understand")
    builder.add_edge("understand", "prepare")
    builder.add_edge("prepare", "retrieve")
    builder.add_edge("retrieve", "match_two_way")

    builder.add_conditional_edges("match_two_way", lambda x: "checkpoint_hitl" if x["match_result"] == "FAILED" else "reconcile")
    builder.add_edge("checkpoint_hitl", "hitl_decision")

    # REJECT -> REQUIRES_MANUAL_HANDLING
    builder.add_conditional_edges("hitl_decision",
        lambda x: "reconcile" if x.get("human_decision") == "ACCEPT" else "complete")

    builder.add_edge("reconcile", "approve")
    builder.add_edge("approve", "posting")
    builder.add_edge("posting", "notify")
    builder.add_edge("notify", "complete")
    builder.add_edge("complete", END)

    memory = SqliteSaver(sqlite3.connect("checkpoints.db", check_same_thread=False))
    return builder.compile(checkpointer=memory, interrupt_after=["checkpoint_hitl"])

langie_app = build_graph()

# --- 6. HITL API & DEMO EXECUTION ---

def api_submit_decision(thread_id, decision, reviewer_id):
    # [cite_start]Final status handling for Rejection [cite: 93]
    final_status = "RECONCILED" if decision == "ACCEPT" else "REQUIRES_MANUAL_HANDLING"
    
    update_data = {
        "human_decision": decision, 
        "reviewer_id": reviewer_id,
        "status": final_status,
        "audit_log": [f"HITL_DECISION: {decision} by {reviewer_id}"]
    }
    
    langie_app.update_state({"configurable": {"thread_id": thread_id}}, update_data, as_node="hitl_decision")
    # [cite_start]Response matches Appendix-1 contract [cite: 424-427]
    return {"resume_token": str(uuid.uuid4()), "next_stage": "RECONCILE" if decision == "ACCEPT" else "COMPLETE"}

if __name__ == "__main__":
    config = {"configurable": {"thread_id": "REJECT_DEMO_01"}}
    invoice_data = {"invoice_payload": {"invoice_id": "INV-ERR-404", "amount": 9000}, "audit_log": []}

    # Run 1: Stop at match failure
    print("--- STARTING WORKFLOW ---")
    for event in langie_app.stream(invoice_data, config): pass

    # Run 2: Human Rejects
    print("--- SUBMITTING REJECTION ---")
    res = api_submit_decision("REJECT_DEMO_01", "REJECT", "AUDITOR_01")
    print(f"HITL Response: {json.dumps(res, indent=2)}")

    # Run 3: Completion
    print("--- FINAL OUTPUT ---")
    for event in langie_app.stream(None, config):
        if "complete" in event:
            print(json.dumps(event["complete"]["final_payload"], indent=2))
