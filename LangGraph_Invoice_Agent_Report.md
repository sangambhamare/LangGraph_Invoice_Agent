# LangGraph Invoice Processing Agent with Human-In-The-Loop (HITL)

**Author:** Sangam Sanjay Bhamare
**Date:** Jan 01, 2026

## 1. Introduction and Agent Architecture

This document details the implementation of **Langie – the Invoice Processing LangGraph Agent**. The agent is designed to model a complex, multi-stage invoice processing workflow, incorporating deterministic steps, dynamic routing, and a critical Human-In-The-Loop (HITL) checkpoint for exception handling.

The core architecture leverages **LangGraph** to represent the workflow as a state machine, ensuring persistent state management and the ability to pause and resume execution. The agent adheres to the principles of the Langie persona: structured stages, state variable propagation, dynamic decision-making, and orchestration of external capabilities via simulated **MCP Clients** and **Bigtool** selection.

## 2. Workflow Configuration and State Management

### 2.1. Workflow Configuration (`WORKFLOW_CONFIG`)

The workflow is defined by a configuration dictionary that outlines the retry policy and the 12 distinct stages of the invoice processing lifecycle. This configuration simulates the Bigtool selection by specifying `pool` options for certain stages and enforces execution on either the `COMMON` or `ATLAS` MCP servers.

| Stage ID | Mode | Server | Key Abilities/Pools | Description |
| :--- | :--- | :--- | :--- | :--- |
| `INTAKE` | Deterministic | COMMON | `validate_schema`, `persist_raw` | Accepts the invoice payload and validates its structure. |
| `UNDERSTAND` | Deterministic | ATLAS | `google_vision`, `tesseract` | Performs OCR and extracts line items (simulated Bigtool selection). |
| `PREPARE` | Deterministic | ATLAS | `clearbit`, `vendor_db` | Normalizes and enriches vendor data (simulated Bigtool selection). |
| `RETRIEVE` | Deterministic | ATLAS | `sap_sandbox` | Fetches related Purchase Orders (POs) and historical data (simulated Bigtool selection). |
| `MATCH_TWO_WAY` | Deterministic | COMMON | `compute_match_score` | Calculates the match score between the invoice and PO. |
| `CHECKPOINT_HITL` | Deterministic | COMMON | - | **Triggers only on match failure.** Persists state and pauses the workflow. |
| `HITL_DECISION` | Non-Deterministic | ATLAS | - | Resumes the workflow after a human decision (ACCEPT/REJECT). |
| `RECONCILE` | Deterministic | COMMON | - | Reconstructs accounting entries upon successful match or human acceptance. |
| `APPROVE` | Deterministic | ATLAS | - | Applies approval policy based on invoice amount. |
| `POSTING` | Deterministic | ATLAS | `sap_sandbox` | Posts the final entries to the ERP system. |
| `NOTIFY` | Deterministic | ATLAS | `sendgrid` | Notifies the vendor and internal finance team. |
| `COMPLETE` | Deterministic | COMMON | - | Generates the final structured output payload. |

### 2.2. Agent State (`AgentState`)

The `AgentState` is a `TypedDict` that defines the state variables carried forward between nodes. This ensures persistence and continuity across the entire workflow, including the pause/resume cycle of the HITL process.

| State Variable | Type | Description |
| :--- | :--- | :--- |
| `invoice_payload` | `Dict[str, Any]` | The raw invoice data. |
| `match_result` | `str` | Result of the two-way match (`MATCHED` or `FAILED`). |
| `match_score` | `float` | The computed confidence score. |
| `human_decision` | `str` | The decision from the human reviewer (`ACCEPT` or `REJECT`). |
| `reviewer_id` | `str` | Identifier of the human who made the decision. |
| `status` | `str` | Current status of the invoice (e.g., `INGESTED`, `PAUSED`, `COMPLETED`). |
| `final_payload` | `Dict[str, Any]` | The final structured output for downstream systems. |
| `audit_log` | `List[str]` | A chronological log of all major events and decisions. |

## 3. Node Implementation and Error Handling

### 3.1. MCP Orchestration and Error Handling

All node functions are wrapped by the `node_wrapper` function. This wrapper serves two critical purposes:

1.  **MCP Ability Enforcement:** It iterates through the `abilities` defined in the stage configuration and calls the simulated `mcp_ability_executor`, enforcing the execution on the specified `COMMON` or `ATLAS` server.
2.  **Retry Policy:** It implements a retry mechanism with exponential backoff, as defined in `WORKFLOW_CONFIG`. If all retries fail, it updates the state to `FAILED` and adds an `OPS_NOTIFY` entry to the `audit_log`, demonstrating robust error handling.

### 3.2. Key Node Logic

*   **`match_node`**: Simulates the two-way matching process. For demonstration, it sets the `match_score` to 0.85 (below the 0.90 threshold) if the invoice amount is high (`> 1000`), forcing a `FAILED` result and triggering the HITL checkpoint.
*   **`checkpoint_hitl_node`**: This node is responsible for pausing the workflow. In a real system, it would persist the full state to a business database and add an entry to a human review queue. It sets the status to `PAUSED`.
*   **`approve_node`**: Implements a simple approval policy. Invoices under 2000 are `AUTO_APPROVED`, while higher-value invoices are `ESCALATED`, and metadata is added to the state.
*   **`complete_node`**: The final stage, which aggregates all relevant state information (`invoice_id`, `status`, `approval`, `posting`) into the clean, structured `final_payload`.

## 4. Graph Construction and HITL Routing

The `build_graph` function dynamically constructs the `StateGraph`.

### 4.1. Dynamic Node Addition

Nodes are added dynamically based on the `WORKFLOW_CONFIG["stages"]`. This allows the graph structure to be easily modified by updating the configuration.

### 4.2. Conditional Edges for HITL

The core of the HITL logic is managed by two conditional edges:

1.  **Match Failure Check**:
    ```python
    builder.add_conditional_edges("match_two_way", 
        lambda x: "checkpoint_hitl" if x["match_result"] == "FAILED" else "reconcile")
    ```
    If the match fails, the workflow routes to `checkpoint_hitl` and then pauses at `hitl_decision` due to the `interrupt_after` setting. Otherwise, it proceeds to `reconcile`.

2.  **Human Decision Routing**:
    ```python
    builder.add_conditional_edges("hitl_decision",
        lambda x: "reconcile" if x.get("human_decision") == "ACCEPT" else "complete")
    ```
    When the human decision is submitted, the workflow resumes. If the human **ACCEPTS**, it routes to `reconcile` to continue processing. If the human **REJECTS**, it routes directly to `complete` to finalize the status as `REQUIRES_MANUAL_HANDLING`.

### 4.3. Checkpointing

The graph is compiled with `SqliteSaver` for checkpointing, and `interrupt_after=["checkpoint_hitl"]` is set. This ensures that when the workflow reaches the `hitl_decision` node, it pauses and waits for an external trigger (the human decision) to resume.

## 5. HITL API and Demo Execution

The `api_submit_decision` function simulates the external API call that a human reviewer's UI would make to resume the workflow.

*   It takes the `thread_id`, `decision` (`ACCEPT`/`REJECT`), and `reviewer_id`.
*   It updates the state with the human's decision and sets the final status accordingly (`RECONCILED` for ACCEPT, `REQUIRES_MANUAL_HANDLING` for REJECT).
*   Crucially, it uses `langie_app.update_state(..., as_node="hitl_decision")` to inject the state update at the exact point where the graph is paused, allowing the conditional edge logic to execute upon the next stream call.

### Demo Run Output (Rejection Scenario)

The demo simulates an invoice with a high amount (`9000`), which triggers a match failure and the HITL checkpoint. The human reviewer then submits a **REJECT** decision, leading to the `REQUIRES_MANUAL_HANDLING` final status.

```
--- STARTING WORKFLOW ---
--- SUBMITTING REJECTION ---
HITL Response: {
  "resume_token": "...",
  "next_stage": "COMPLETE"
}
--- FINAL OUTPUT ---
{
  "invoice_id": "INV-ERR-404",
  "final_status": "REQUIRES_MANUAL_HANDLING",
  "approval": null,
  "posting": null,
  "audit_count": 1
}
```

## 6. Full Annotated Code

The complete, executable Python code for the LangGraph Invoice Processing Agent is provided below.

\`\`\`python
import sqlite3
import json
import uuid
import time
from typing import Dict, List, Any, Optional, Literal
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

# --- 1. CONFIGURATION & ABILITY REGISTRY ---
# Defines the 12 stages, MCP server mapping, Bigtool pools, and retry policy.
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

# --- 2. STATE DEFINITION ---
# Defines the state variables carried forward by the LangGraph.
class AgentState(TypedDict):
    invoice_payload: Dict[str, Any]
    match_result: str
    match_score: float
    human_decision: str
    reviewer_id: str
    status: str
    approval_metadata: Dict[str, Any]
    posting_info: Dict[str, Any]
    final_payload: Dict[str, Any]
    audit_log: List[str]
    retry_count: int

# --- 3. ERROR HANDLING & ABILITY ENFORCEMENT ---

def mcp_ability_executor(stage_id: str, ability: str, server: str):
    """Simulates execution via specific MCP servers (COMMON or ATLAS)."""
    return f"Enforced execution of {ability} on {server} server."

def node_wrapper(stage_cfg, func):
    """Implements Retry Policy, Backoff, and MCP Ability Execution."""
    def wrapper(state: AgentState):
        retries = WORKFLOW_CONFIG["retry_policy"]["max_retries"]
        backoff = WORKFLOW_CONFIG["retry_policy"]["backoff_seconds"]

        for i in range(retries):
            try:
                # Execute registered abilities from config
                abilities = stage_cfg.get("abilities", ["default_process"])
                for ability in abilities:
                    mcp_ability_executor(stage_cfg["id"], ability, stage_cfg["server"])

                return func(state)
            except Exception as e:
                state["audit_log"].append(f"ERROR: {stage_cfg['id']} failed. Retry {i+1}/{retries}.")
                time.sleep(backoff * (i + 1))
                if i == retries - 1:
                    # Unrecoverable error handling
                    return {"status": "FAILED", "audit_log": state["audit_log"] + [f"OPS_NOTIFY: Unrecoverable error in {stage_cfg['id']}"]}
        return {}
    return wrapper

# --- 4. NODE LOGIC ---

def intake_node(state): 
    return {"status": "INGESTED", "audit_log": state["audit_log"] + ["INTAKE complete."]}

def match_node(state):
    # Simulates a match failure for high-value invoices to trigger HITL
    score = 0.85 if state["invoice_payload"]["amount"] > 1000 else 0.95
    result = "FAILED" if score < WORKFLOW_CONFIG["match_threshold"] else "MATCHED"
    return {"match_score": score, "match_result": result, "audit_log": state["audit_log"] + [f"MATCH_TWO_WAY: Score {score}, Result {result}"]}

def checkpoint_hitl_node(state):
    """Pauses the workflow and stores state for human review."""
    return {"status": "PAUSED", "audit_log": state["audit_log"] + ["CHECKPOINT: Stored in human_review_queue."]}

def approve_node(state):
    """Applies approval policy based on invoice amount."""
    amount = state["invoice_payload"]["amount"]
    status = "AUTO_APPROVED" if amount < 2000 else "ESCALATED"
    meta = {"level": "L1" if amount < 5000 else "L2", "reason": "High Value" if amount >= 2000 else "Threshold OK"}
    return {"status": status, "approval_metadata": meta, "audit_log": state["audit_log"] + [f"APPROVE: Status {status}"]}

def posting_node(state):
    """Simulates posting to ERP."""
    return {"posting_info": {"erp_txn_id": str(uuid.uuid4()), "posted": True}, "audit_log": state["audit_log"] + ["POSTING: ERP transaction created."]}

def complete_node(state):
    """Generates the final structured payload."""
    final = {
        "invoice_id": state["invoice_payload"].get("invoice_id"),
        "final_status": state["status"],
        "approval": state.get("approval_metadata"),
        "posting": state.get("posting_info"),
        "audit_count": len(state["audit_log"])
    }
    return {"final_payload": final, "status": "COMPLETED", "audit_log": state["audit_log"] + ["COMPLETE: Final payload generated."]}

# --- 5. DYNAMIC GRAPH BUILDING ---

def build_graph():
    builder = StateGraph(AgentState)
    
    # Map stage IDs to their corresponding node logic
    logic_map = {
        "INTAKE": intake_node, "MATCH_TWO_WAY": match_node,
        "CHECKPOINT_HITL": checkpoint_hitl_node, "APPROVE": approve_node,
        "POSTING": posting_node, "COMPLETE": complete_node
    }

    # Build nodes dynamically
    for stage in WORKFLOW_CONFIG["stages"]:
        # Default logic for generic stages (UNDERSTAND, PREPARE, RETRIEVE, RECONCILE, NOTIFY)
        node_func = logic_map.get(stage["id"], 
                                  lambda x: {"audit_log": x["audit_log"] + [f"Processed {stage['id']}"]})
        builder.add_node(stage["id"].lower(), node_wrapper(stage, node_func))

    # Edges (Standard sequential with conditional branches)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "understand")
    builder.add_edge("understand", "prepare")
    builder.add_edge("prepare", "retrieve")
    builder.add_edge("retrieve", "match_two_way")

    # Conditional routing 1: Match failure triggers HITL
    builder.add_conditional_edges("match_two_way", 
        lambda x: "checkpoint_hitl" if x["match_result"] == "FAILED" else "reconcile")
    
    builder.add_edge("checkpoint_hitl", "hitl_decision")

    # Conditional routing 2: Human decision determines next step
    # ACCEPT -> RECONCILE (Continue workflow)
    # REJECT -> COMPLETE (Finalize as REQUIRES_MANUAL_HANDLING)
    builder.add_conditional_edges("hitl_decision",
        lambda x: "reconcile" if x.get("human_decision") == "ACCEPT" else "complete")

    # Standard sequential flow for post-HITL/post-match stages
    builder.add_edge("reconcile", "approve")
    builder.add_edge("approve", "posting")
    builder.add_edge("posting", "notify")
    builder.add_edge("notify", "complete")
    builder.add_edge("complete", END)

    # Setup SQLite checkpointing and interrupt point
    memory = SqliteSaver(sqlite3.connect("checkpoints.db", check_same_thread=False))
    return builder.compile(checkpointer=memory, interrupt_after=["checkpoint_hitl"])

langie_app = build_graph()

# --- 6. HITL API & DEMO EXECUTION ---

def api_submit_decision(thread_id, decision, reviewer_id):
    """Simulates the external API call to resume the workflow after human review."""
    
    # Set final status based on decision
    final_status = "RECONCILED" if decision == "ACCEPT" else "REQUIRES_MANUAL_HANDLING"
    
    update_data = {
        "human_decision": decision, 
        "reviewer_id": reviewer_id,
        "status": final_status,
        "audit_log": [f"HITL_DECISION: {decision} by {reviewer_id}"]
    }
    
    # Update the state at the paused node ("hitl_decision")
    langie_app.update_state({"configurable": {"thread_id": thread_id}}, update_data, as_node="hitl_decision")
    
    return {"resume_token": str(uuid.uuid4()), "next_stage": "RECONCILE" if decision == "ACCEPT" else "COMPLETE"}

if __name__ == "__main__":
    # Clean up previous checkpoint file for a fresh run
    try:
        import os
        os.remove("checkpoints.db")
    except:
        pass

    config = {"configurable": {"thread_id": "REJECT_DEMO_01"}}
    # High amount (9000) to force match failure and HITL
    invoice_data = {"invoice_payload": {"invoice_id": "INV-ERR-404", "amount": 9000}, "audit_log": []}

    # Run 1: Workflow executes until it hits the HITL checkpoint and pauses
    print("--- STARTING WORKFLOW (Pauses at HITL) ---")
    for event in langie_app.stream(invoice_data, config): 
        if "__end__" not in event:
            print(event)

    # Run 2: Human Rejects, injecting the decision into the state
    print("\n--- SUBMITTING REJECTION ---")
    res = api_submit_decision("REJECT_DEMO_01", "REJECT", "AUDITOR_01")
    print(f"HITL Response: {json.dumps(res, indent=2)}")

    # Run 3: Workflow resumes from the checkpoint and completes
    print("\n--- FINAL OUTPUT (Resumes and Completes) ---")
    for event in langie_app.stream(None, config):
        if "complete" in event:
            print(json.dumps(event["complete"]["final_payload"], indent=2))
\`\`\`
