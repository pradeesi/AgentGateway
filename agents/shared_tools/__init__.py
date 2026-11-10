"""
Purpose: Exports the shared Cymbal Bank MCP JSON-RPC client and gateway-verdict
         parser for local ADK execution and automated pytest verification.
Architecture/Context: Each deployable Agent Runtime directory (`tier1_support_agent`
         and `risk_specialist_agent`) also carries a self-contained copy inside
         its own `app/` package so `agents-cli deploy` Docker builds succeed
         without parent-directory context.
Dependencies/Side Effects: Imports `agents.shared_tools.mcp_client`.
Author: Pradeep Singh
License: Apache-2.0
"""

from agents.shared_tools.mcp_client import CymbalMcpClient, classify_gateway_block

__all__ = ["CymbalMcpClient", "classify_gateway_block"]
