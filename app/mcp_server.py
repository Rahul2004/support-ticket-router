from mcp.server.fastmcp import FastMCP

mcp = FastMCP("TicketManagement")


@mcp.tool()
def lookup_kb_article(category: str) -> str:
    """Queries knowledge base articles for troubleshooting steps.

    Args:
        category: The ticket category (e.g. Billing, Technical, Account).
    """
    kb = {
        "billing": "For payment failures, verify credit card expiry and billing zip code. Refund requests require manager approval if >$50.",
        "technical": "If service is down, check system status page. Restarting the client application fixes 80% of login issues. Clear cache if images don't load.",
        "account": "Password reset link is valid for 24 hours. For account deletion, verify user consent and confirm no active subscriptions.",
    }
    return kb.get(
        category.lower(), "No specific article found. Escalate to Tier 2 support."
    )


@mcp.tool()
def get_ticket_history(customer_id: str) -> str:
    """Gets the past ticket IDs and summary for a customer.

    Args:
        customer_id: The unique ID of the customer (e.g. CUST-101).
    """
    history = {
        "cust-101": "Ticket #2912: Billing issue - Resolved. Ticket #3104: Password reset - Closed.",
        "cust-102": "Ticket #1094: Slow loading - Escalated.",
        "cust-103": "No recent tickets in past 90 days.",
    }
    return history.get(customer_id.lower(), "New customer. No history found.")


@mcp.tool()
def check_escalation_status(customer_tier: str) -> str:
    """Verifies escalation path based on customer tier (e.g., standard, gold, enterprise).

    Args:
        customer_tier: Customer support SLA tier (e.g. standard, gold, enterprise).
    """
    tier = customer_tier.lower()
    if tier == "enterprise":
        return "SLA: 1 hour response time. Direct path to Senior On-Call Engineer."
    elif tier == "gold":
        return "SLA: 4 hours response time. Direct path to Tier 2 Support Team."
    else:
        return "SLA: 24 hours response time. Standard queue."


if __name__ == "__main__":
    mcp.run()
