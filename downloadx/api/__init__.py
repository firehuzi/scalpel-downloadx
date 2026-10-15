"""接口层：本地 REST API 与 Agent（工具调用）API。"""

from .agent_api import build_manifest, handle_natural_language, invoke_tool
from .local_api import create_app

__all__ = ["create_app", "build_manifest", "invoke_tool", "handle_natural_language"]
