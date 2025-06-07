from pydantic import BaseModel
from typing import Annotated, List, Literal
from langchain_core.messages import (
    BaseMessage,
    SystemMessage,
    AIMessage,
    ToolMessage
)
from langchain_openai import ChatOpenAI 
from langgraph.types import Command, interrupt
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from langchain_mcp_adapters.client import MultiServerMCPClient
from ralph.my_mcp.config import mcp_config
from ralph.prompts import ralph_system_prompt
import json
import os


class AgentState(BaseModel):
    """
    The state of the agent.
    """
    messages: Annotated[List[BaseMessage], add_messages] = []
    protected_tools: List[str] = ["create_campaign", "send_campaign_email"]
    yolo_mode: bool = False


async def build_graph():
    """
    Build the LangGraph application.
    """
    client = MultiServerMCPClient(connections=mcp_config["mcpServers"])
    tools = await client.get_tools()

    # ✅ NVIDIA model using OpenAI-compatible endpoint
    llm = ChatOpenAI(
        model="nvidia/llama-3.1-nemotron-nano-4b-v1.1",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=os.getenv("NVIDIA_API_KEY"),  # 🔐 Use your NVIDIA key
    ).bind_tools(tools)

    def assistant_node(state: AgentState) -> AgentState:
        response = llm.invoke(
            [SystemMessage(content=ralph_system_prompt)] +
            state.messages
        )
        state.messages = state.messages + [response]
        return state

    def human_tool_review_node(state: AgentState) -> Command[Literal["assistant_node", "tools"]]:
        last_message = state.messages[-1]
        tool_call = last_message.tool_calls[-1]

        human_review: dict = interrupt({
            "message": "Your input is required for the following tool:",
            "tool_call": tool_call
        })

        review_action = human_review["action"]
        review_data = human_review.get("data")

        if review_action == "continue":
            return Command(goto="tools")

        elif review_action == "update":
            updated_message = AIMessage(
                content=last_message.content,
                tool_calls=[{
                    "id": tool_call["id"],
                    "name": tool_call["name"],
                    "args": json.loads(review_data)
                }],
                id=last_message.id
            )
            return Command(goto="tools", update={"messages": [updated_message]})

        elif review_action == "feedback":
            tool_message = ToolMessage(
                content=review_data,
                name=tool_call["name"],
                tool_call_id=tool_call["id"]
            )
            return Command(goto="assistant_node", update={"messages": [tool_message]})

    def assistant_router(state: AgentState) -> str:
        last_message = state.messages[-1]
        if not last_message.tool_calls:
            return END
        else:
            if not state.yolo_mode:
                if any(tool_call["name"] in state.protected_tools for tool_call in last_message.tool_calls):
                    return "human_tool_review_node"
            return "tools"

    builder = StateGraph(AgentState)
    builder.add_node(assistant_node)
    builder.add_node(human_tool_review_node)
    builder.add_node("tools", ToolNode(tools))

    builder.add_edge(START, "assistant_node")
    builder.add_conditional_edges("assistant_node", assistant_router, ["tools", "human_tool_review_node", END])
    builder.add_edge("tools", "assistant_node")

    return builder.compile(checkpointer=MemorySaver())


def inspect_graph(graph):
    from IPython.display import display, Image
    display(Image(graph.get_graph(xray=True).draw_mermaid_png()))


if __name__ == "__main__":
    import asyncio
    import nest_asyncio
    nest_asyncio.apply()

    graph = asyncio.run(build_graph())
    inspect_graph(graph)
