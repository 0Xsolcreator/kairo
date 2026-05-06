"""
Thin Python wrappers around external CLIs / SDKs we shell out to.

Each client owns its preflight (binary present, version sane) and any
process-global concurrency concerns. Actions in agent/executor/actions/
import these clients directly.
"""
