"""Agent interface: a headless editing engine plus an MCP server that lets AI
agents (e.g. Claude) view and edit PDFs with Quill's editing operations."""
from .engine import AgentDocument, EngineError

__all__ = ["AgentDocument", "EngineError"]
