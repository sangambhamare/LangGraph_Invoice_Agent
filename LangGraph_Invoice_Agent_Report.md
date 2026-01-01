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
