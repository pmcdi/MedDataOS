"""Capabilities plugged into the core: tools, skills, agents, sandbox, MCP.

``meddataos.core`` holds what every answer passes through (the controller, the
loop and model clients, the evidence registry, the verifier). Everything here is
optional: a capability that cannot run removes its tools, never breaks the core.
This package imports ``core`` and never the server.
"""
