"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the project instructions and rubric for guidance.
Work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ──  1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()



# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ──  2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in the infrastructure setup section of the project instructions.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# This starter uses an unsigned MCP connection and therefore assumes the
# project Gateway is configured with the NONE authorizer.
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-h1h9omcc6y.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   
KB_ID       = "AU9EJL5XIJ"          
REGION      = "us-east-1"    
MEMORY_ID   = "CustomerSupportMemory-i5cSdCHxIN"        


# ──  3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "us.amazon.nova-2-lite-v1:0"

#  Create the BedrockModel instance
model = BedrockModel(model_id=model_id)

# Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)

# Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── 4 Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Read the namespace from strategy["namespaceTemplates"][0].
#      For compatibility with older AgentCore responses, fall back to
#      strategy["namespaces"][0] when namespaceTemplates is absent.
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    # Implement this function
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ──  5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        # Store actor_id, session_id, memory_id, memory_client as attributes
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = memory_id
        self.memory_client = memory_client
        # Call get_namespaces() and store the result as self.namespaces
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)
        print("Memory ID" , self.memory_id)
        print("Namespaces:", self.namespaces)

    def _retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        
        actor_id = event.agent.state.get("actor_id")
        if not actor_id:
            return
        
        #   1. Get the last message from event.agent.messages
        messages = event.agent.messages
        
        #   2. Check it is a user message and not a tool result
        if (
            not messages
            or messages[-1]["role"] != "user"
            or "toolResult" in messages[-1]["content"][0]
        ):
            return    
        #   3. Extract the user query text
        user_query = messages[-1]["content"][0]["text"]
        
        #   4. For each namespace in self.namespaces, call retrieve_memories()
        try:
            all_context = []
            for strategy_type, namespace_template in self.namespaces.items():
                resolved_namespace = namespace_template.format(actorId=actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=resolved_namespace,
                    query=user_query,
                    top_k=5
                )
            #   5. Collect non-empty memory texts with strategy type tags
                for memory in memories:
                    if isinstance(memory, dict):
                        text = memory.get("content", {}).get("text", "").strip()
                        if text:
                            all_context.append(f"[{strategy_type}] {text}")
                        
                        if memory.get("text", ""):
                            all_context.append(f"[{strategy_type}] {memory['text']}.strip()")

            #   6. If any found, prepend them to the user message
            if all_context:
                context_block = "\n".join(all_context) + "\n"
                original_text = messages[-1]["content"][0]["text"]
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{context_block}\n{original_text}"
                )
                logger.info("Retrieved %d memory items for actor %s", len(all_context), actor_id)

        except Exception as e:
            logger.warning("Error retrieving memories for actor %s: %s", actor_id, e)

    def _save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        #  Implement memory saving
        actor_id   = event.agent.state.get("actor_id")
        session_id = event.agent.state.get("session_id")
        if not actor_id or not session_id:
            return
        #   1. Get messages from event.agent.messages
        try:
            messages = event.agent.messages
            user_text = agent_text = None
            #   2. Walk backwards to find the last user query (plain text)
            #      and the last assistant response
            for message in reversed(messages):
                if message["role"] == "assistant" and agent_text is None:
                    content = message["content"]
                    if isinstance(content, list):
                        agent_text = content[0].get("text", "")
                    else:
                        agent_text = str(content)

                elif ( message["role"] == "user" and not user_text and "toolResult" not in message["content"][0] ):
                    user_text = message["content"][0]["text"]
                    break
            #   3. Call memory_client.create_event() with both messages
            if user_text and agent_text:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (user_text, "USER"),
                        (agent_text, "ASSISTANT")
                    ],
                )
                logger.info("Saved interaction to memory for actor %s, session %s", actor_id, session_id)

        except Exception as e:
            logger.error("Error saving interaction to memory for actor %s, session %s: %s", actor_id, session_id, e)


    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self._retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self._save_support_interaction)
        


# ──  6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.

# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    #  Implement the Knowledge Base search
    #   1. Guard: if KB_ID is empty return "Knowledge base not configured."
    if not KB_ID:
        return "Knowledge base not configured."

    #   2. Call _bedrock_runtime.retrieve(
    #          knowledgeBaseId=KB_ID,
    #          retrievalQuery={"text": query}
    #      )
    try:
        response = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query}
        )

        # Extract retrieval results
        #   3. Extract resp["retrievalResults"]; return a message if empty
        retrieval_results = response.get("retrievalResults", [])
        if not retrieval_results:
            return "No relevant information found."

        # Join the text chunks
        #   4. Join the text chunks with "\n---\n" and return the result
        return "\n---\n".join([result["content"]["text"] for result in retrieval_results])
    except  Exception as e:
        logger.warning(f"Error searching knowledge base: {e}")
        return "Error searching knowledge base."


# ──  7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # : Build the code string (use an f-string to inject the arguments)
    code = f"""
import json
import math

loyalty_points = {loyalty_points}
tier = {tier!r}
order_total = {order_total}
product_category = {product_category!r}

earn_rates = {{
    "standard": 1,
    "device": 2,
    "fresh": 5
}}

tier_rates = {{
    "Silver": 0.00,
    "Gold": 0.10,
    "Platinum": 0.15
}}

# Points can only be redeemed in multiples of 500.
# Maximum redemption is 50% of the order value.
max_points = int(order_total * 0.50 * 100)

points_redeemed = min(
    (loyalty_points // 500) * 500,
    (max_points // 500) * 500
)

# 100 points = $1
points_discount = points_redeemed / 100

subtotal_after_points = order_total - points_discount

tier_discount = (
    subtotal_after_points
    * tier_rates.get(tier, 0.0)
)

final_total = subtotal_after_points - tier_discount

total_savings = points_discount + tier_discount

points_earned = math.floor(
    final_total * earn_rates.get(product_category, 1)
)

remaining_points = loyalty_points - points_redeemed

result = {{
    "points_redeemed": points_redeemed,
    "tier_discount": round(tier_discount, 2),
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points
}}

print(json.dumps(result))
            """
    try:
        # : Execute the code using code_session and return the result
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )

        for event in response["stream"]:
            return json.dumps(event["result"])

    except Exception as e:
        # TODO: Implement fallback calculation using tier discount only
        logger.warning(
            "Code Interpreter unavailable, using fallback: %s",
            e
        )

        # Fallback: tier discount only
        tier_rates = {
            "Silver": 0.00,
            "Gold": 0.10,
            "Platinum": 0.15
        }

        rate = tier_rates.get(tier, 0.0)

        tier_discount = order_total * rate
        final_total = order_total - tier_discount

        return json.dumps({
            "tier_discount": round(tier_discount, 2),
            "final_total": round(final_total, 2),
            "total_savings": round(tier_discount, 2),
            "points_redeemed": 0,
            "points_earned": 0,
            "remaining_points": loyalty_points,
            "fallback": True
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully
SYSTEM_PROMPT = """ 
You have access to tools via the AgentCore Gateway:
- get_order(order id)          : Retrieve an order by reference (e.g. BK-1001)
- get_customer_orders(customer id)     : List all orders for a customer
- get_customer(customer id)     : Retrieve a specific customer's information (e.g. CUST-1001)
- initiate_refund(order id)          : Initiate a refund for a customer order.
- check_refund_status(refund id)          : Check the status of a refund 
- get_return_label(order id)          : Generate a prepaid return shipping label for an order.

You have access to a live web browser. Use it to look up destination information
on Wikivoyage (en.wikivoyage.org) — a free, open travel guide.

When a customer asks about a destination:
1. Navigate to en.wikivoyage.org/wiki/<DestinationName>
2. Read the page — focus on highlights, neighbourhoods, and practical tips
3. Summarise what you find clearly and concisely

Always tell the customer that the information came from Wikivoyage.

you have tool of the Amazon product catalog and support knowledge base.
- search_knowledge_base : by giving a topic or question about poducts and policies you can check actual answers
Use this to answer questions about product specifications, return policies, warranty information, 
loyalty program details, and order status definitions.

Use these tools to help customers with their requests regarding their orders, refunds, and returns.
Present results clearly and ask clarifying questions when needed
"""
        
#SYSTEM_PROMPT = """You are a customer support AI agent. Use the available tools to assist customers with their inquiries, including searching the knowledge base, calculating loyalty discounts, and browsing relevant information. Present results clearly and ask clarifying questions when needed."""
# SYSTEM_PROMPT = """You are a customer support AI agent. Use the available tools to assist customers with their inquiries, including searching the knowledge base, calculating loyalty discounts, and browsing relevant information. Present results clearly and ask clarifying questions when needed."""
    
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    # : Implement the agent invocation
    #   1. Extract user_input, actor_id, and session_id from the payload
    #      (generate a UUID if session_id is missing)
    
    print("Received payload:", payload)
    
    # logger.info("Received payload: %s", payload)
    # user_message = payload.get("prompt", "Hello!")
    # logger.info("User: %s", user_message[:80])
    # prompt_data = json.loads(user_prompt)
    # if prompt_data["actor_id"]:
    #     actor_id = prompt_data["actor_id"]
    # else:
    #     actor_id = "customer-support-user"
    
    # actor_id = payload.get("actor_id", "customer-support-user")
    # logger.info("Actor ID: %s", actor_id)
    # if prompt_data["session_id"]:
    #     session_id = prompt_data["session_id"]
    # else:
    #     session_id = str(uuid.uuid4())

    # session_id = payload.get("session_id") or str(uuid.uuid4())
    # logger.info("Session ID: %s", session_id)
    actor_id = "CUST-456" 
    session_id = "test1"
    user_message = payload.get("prompt", "Hello!")
    print("User message:", user_message)
    print("Initializing Agent for actor_id=%s, session_id=%s", actor_id, session_id)
    #   2. Instantiate MemoryHook for this actor/session
    memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
    #   3. Instantiate AgentCoreBrowser(region=REGION)
    agent_core_browser = AgentCoreBrowser(session_timeout=600)
    #   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
    #                              agent_core_browser.browser]
    tools = [
        search_knowledge_base,
        calculate_loyalty_discount,
        agent_core_browser.browser
    ]
    #   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
    client = MCPClient(
        lambda: streamable_http_client(url=GATEWAY_URL)
	)
    with client:
        gateway_tools = client.list_tools_sync()
        logger.info("Discovered %d tools from Gateway", len(gateway_tools))
        tools.extend(gateway_tools)
        #   6. Create and invoke the Agent with all tools, hooks, and system_prompt
        agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            state={"actor_id": actor_id, "session_id": session_id},
            hooks=[memory_hook]
        )
        #   7. Return the text from the first content block of the response
        logger.info("Invoking agent with message: %s", user_message[:80])
        response = await agent.invoke_async(user_message) #agent(user_message)
        return response
        #   8. Handle exceptions gracefully
    

# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
