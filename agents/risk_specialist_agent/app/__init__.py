"""
Purpose: Package initializer exposing `root_agent` for the Cymbal Bank Risk &
         Disputes Specialist Google ADK Agent.
Architecture/Context: Imported both as `app` inside the Agent Runtime container
         and as `agents.risk_specialist_agent.app` during local portal/pytest runs.
Dependencies/Side Effects: Imports `root_agent` from `.agent`.
Author: Pradeep Singh
License: Apache-2.0
"""

from .agent import root_agent

__all__ = ["root_agent"]
