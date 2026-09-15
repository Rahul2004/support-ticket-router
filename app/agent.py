# ruff: noqa
import re
import json
import logging
from typing import Any
from pydantic import BaseModel, Field

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool, McpToolset
from google.adk.events import RequestInput
import google.adk.workflow as wf
from mcp.client.stdio import StdioServerParameters

from app.config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("support_ticket_router")


# Define state schema
class WorkflowState(BaseModel):
    ticket_id: str = ""
    customer_id: str = ""
    customer_tier: str = "standard"
    ticket_text: str = ""
    clean_ticket_text: str = ""
    classification: str = ""
    draft_response: str = ""
    approved: bool = False
    feedback: str = ""
    audit_log: list[dict] = Field(default_factory=list)


# 1. MCP Toolset initialization
mcp_toolset = McpToolset(
    connection_params=StdioServerParameters(
        command="uv", args=["run", "python", "app/mcp_server.py"]
    )
)

# 2. Specialized sub-agents
ticket_classifier = Agent(
    name="ticket_classifier",
    model=Gemini(model=config.model),
    instruction="""You are a customer support ticket classifier.
Analyze the customer's support ticket and determine:
- Category (Billing, Technical, Account, General)
- Sentiment (Positive, Neutral, Negative, Frustrated)
- Escalation path (using check_escalation_status tool based on customer_tier)

Always respond with a clear summary outlining Category, Sentiment, Priority, and Escalation SLA.
""",
    tools=[mcp_toolset],
)

response_drafter = Agent(
    name="response_drafter",
    model=Gemini(model=config.model),
    instruction="""You are a customer support response drafter.
Draft a professional response to the customer. Use the get_ticket_history tool to check past tickets
and lookup_kb_article to find the correct troubleshooting steps or billing guidelines.
Always refer to the ticket classification details from the classification analysis.
Provide a complete, polite response draft.
""",
    tools=[mcp_toolset],
)

# 3. Orchestrator agent
orchestrator = Agent(
    name="orchestrator",
    model=Gemini(model=config.model),
    instruction="""You are the Support Ticket Routing Orchestrator.
Your goal is to coordinate classification and drafting of support responses.
1. Run the ticket_classifier to analyze the incoming ticket.
2. Run the response_drafter to write a professional response.
Synthesize the final draft response and output it clearly.
""",
    tools=[AgentTool(agent=ticket_classifier), AgentTool(agent=response_drafter)],
)


# 4. Security Checkpoint node
@wf.node
def security_checkpoint(ctx: Context, node_input: Any = None):
    # Retrieve and parse raw input robustly
    raw_text = ""
    ticket_id = "UNKNOWN"
    customer_id = "UNKNOWN"
    customer_tier = "standard"

    if node_input is not None:
        if isinstance(node_input, dict):
            raw_text = node_input.get("ticket_text", "")
            ticket_id = node_input.get("ticket_id", "UNKNOWN")
            customer_id = node_input.get("customer_id", "UNKNOWN")
            customer_tier = node_input.get("customer_tier", "standard")
        else:
            input_str = ""
            if isinstance(node_input, str):
                input_str = node_input
            elif hasattr(node_input, "parts") and node_input.parts:
                parts_text = []
                for part in node_input.parts:
                    if hasattr(part, "text") and part.text:
                        parts_text.append(part.text)
                input_str = "".join(parts_text)
            else:
                input_str = str(node_input)

            try:
                parsed_json = json.loads(input_str)
                if isinstance(parsed_json, dict):
                    raw_text = parsed_json.get("ticket_text", "")
                    ticket_id = parsed_json.get("ticket_id", "UNKNOWN")
                    customer_id = parsed_json.get("customer_id", "UNKNOWN")
                    customer_tier = parsed_json.get("customer_tier", "standard")
                else:
                    raw_text = input_str
            except Exception:
                raw_text = input_str

    # Initialize state
    ctx.state["ticket_id"] = ticket_id
    ctx.state["customer_id"] = customer_id
    ctx.state["customer_tier"] = customer_tier
    ctx.state["ticket_text"] = raw_text
    ctx.state["audit_log"] = []

    # PII Scrubbing
    scrubbed_text = raw_text
    email_pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    phone_pattern = (
        r"\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}"
    )

    scrubbed_text = re.sub(email_pattern, "[EMAIL_REDACTED]", scrubbed_text)
    scrubbed_text = re.sub(phone_pattern, "[PHONE_REDACTED]", scrubbed_text)
    ctx.state["clean_ticket_text"] = scrubbed_text

    pii_scrubbed = scrubbed_text != raw_text
    ctx.state["audit_log"].append(
        {
            "event": "PII_CHECK",
            "scrubbed": pii_scrubbed,
            "severity": "INFO",
            "message": "PII scrubbing checks completed.",
        }
    )

    # Prompt Injection Detection
    injection_keywords = [
        "ignore previous instructions",
        "system override",
        "jailbreak",
        "sudo bash",
        "ignore all rules",
    ]
    detected = False
    for kw in injection_keywords:
        if kw in raw_text.lower():
            detected = True
            break

    if detected:
        ctx.state["audit_log"].append(
            {
                "event": "SECURITY_ALERT",
                "type": "PROMPT_INJECTION",
                "severity": "CRITICAL",
                "message": "Prompt injection attempt detected in ticket text.",
            }
        )
        ctx.route = "security_event"
        return "Security checkpoint: FAILED"

    ctx.state["audit_log"].append(
        {
            "event": "SECURITY_CHECK_PASS",
            "severity": "INFO",
            "message": "Security checks completed successfully.",
        }
    )
    ctx.route = "clean"
    return "Security checkpoint: PASSED"


# 5. Human Review node
@wf.node
def human_review(ctx: Context, node_input: Any):
    # Save draft response to state
    if node_input:
        ctx.state["draft_response"] = str(node_input)

    # Check if we have response from human review
    interrupt_id = f"review_draft_{ctx.state['ticket_id']}"
    user_response = ctx.resume_inputs.get(interrupt_id)

    if user_response is None:
        prompt_message = (
            f"Ticket ID: {ctx.state['ticket_id']}\n"
            f"Customer ID: {ctx.state['customer_id']} ({ctx.state['customer_tier']})\n"
            f"Ticket Text: {ctx.state['clean_ticket_text']}\n\n"
            f"Draft Response:\n{ctx.state['draft_response']}\n\n"
            "Please review. Type 'yes' to approve, or type your feedback to request revision:"
        )
        yield RequestInput(interrupt_id=interrupt_id, message=prompt_message)
        return

    # Process response
    response_text = str(user_response).strip()
    if response_text.lower() in ["yes", "approve", "y"]:
        ctx.state["approved"] = True
        ctx.state["audit_log"].append(
            {"event": "HUMAN_APPROVAL", "status": "APPROVED", "response": response_text}
        )
    else:
        ctx.state["approved"] = False
        ctx.state["feedback"] = response_text
        ctx.state["audit_log"].append(
            {"event": "HUMAN_APPROVAL", "status": "REJECTED", "feedback": response_text}
        )

    return "Human review complete."


# 6. Security Failure handler node
@wf.node
def security_failure_handler(ctx: Context):
    ctx.state["draft_response"] = (
        "BLOCKED: Security violation detected. Ticket context contains potential prompt injection."
    )
    ctx.state["approved"] = False
    return "Security violation handled."


# 7. Final Output node
@wf.node
def final_output(ctx: Context):
    status = "APPROVED" if ctx.state.get("approved", False) else "REJECTED_OR_BLOCKED"
    result = {
        "ticket_id": ctx.state.get("ticket_id"),
        "status": status,
        "clean_ticket_text": ctx.state.get("clean_ticket_text"),
        "final_response": ctx.state.get("draft_response"),
        "audit_log": ctx.state.get("audit_log", []),
    }
    logger.warning(
        "AUDIT LOG:\n" + json.dumps(ctx.state.get("audit_log", []), indent=2)
    )
    return result


# 8. Workflow definition
workflow = wf.Workflow(
    name="support_ticket_routing_workflow",
    description="Secure multi-agent support ticket routing and drafting workflow",
    state_schema=WorkflowState,
    edges=[
        (wf.START, security_checkpoint),
        (
            security_checkpoint,
            {"clean": orchestrator, "security_event": security_failure_handler},
        ),
        (orchestrator, human_review),
        (human_review, final_output),
        (security_failure_handler, final_output),
    ],
)

app = App(
    root_agent=workflow,
    name="app",
)
