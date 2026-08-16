"""法令Domainで公開するread-only Tool集合。"""

from app.adapters.tools.legal_search import (
    LegalFetchArticlesTool,
    LegalGraphNeighborsTool,
    LegalSearchTool,
)
from app.agent_framework.ports.tool import ToolRegistry
from app.graph_client import GraphClient
from app.opensearch_client import OpenSearchClient


def legal_tool_registry(
    client: OpenSearchClient,
    graph_client: GraphClient,
    *,
    user_clearance_level: int,
) -> ToolRegistry:
    return ToolRegistry(
        (
            LegalSearchTool(
                client,
                user_clearance_level=user_clearance_level,
            ),
            LegalFetchArticlesTool(
                client,
                user_clearance_level=user_clearance_level,
            ),
            LegalGraphNeighborsTool(
                graph_client,
                user_clearance_level=user_clearance_level,
            ),
        )
    )
