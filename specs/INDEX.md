# Specs Index

Read this file first. Load only the specs relevant to the current task.

## Core Standards (always relevant)
| Spec | File | Summary |
|------|------|---------|
| Conventions | `specs/conventions.md` | Build order (CLI → skill → spec → code), when to use skill-creator-claude / mcp-tool-builder |
| Security Policy | `specs/security.md` | Hard limits, elevated-risk rules, prompt injection defense, default stance |
| Branch Strategy | `specs/branching.md` | Branch naming, PR checklist |
| Notification Channels | `specs/notification-channels.md` | Fallback order: Telegram → Telegram API → Gmail → alert file |
| Architecture Principles | `specs/hive-mind-architecture.md` | Event → Specification → Tools pattern; what belongs where |
| Testing Guidelines | `specs/testing.md` | What makes a test worth keeping; test strategy |
| Harness-Native Ops | `specs/harness-native-operations.md` | Only write code when the harness can't do it; skills over programs; the decision boundary |
| Data Classes | `specs/data-classes/index.md` | 4-class memory taxonomy: ephemeral / current-state / future-state / feedback |

## Security Implementation
| Spec | File | Summary |
|------|------|---------|
| Secret Management | `specs/secret-management.md` | `.env` as the secret store; `get_credential` thin wrapper; rules for adding new keys |
| Tool Safety | `specs/tool-safety.md` | Ring 1 AST validation, Ring 2 subprocess isolation, blocked patterns, staging flow |
| Container Hardening | `specs/container-hardening.md` | Ring 3 runtime restrictions, compatibility exceptions, Ring 4 production volumes |
| OpenClaw CVE Analysis | `specs/openclaw-cve-analysis.md` | CVE pattern mapping to Hive Mind; hardening checklist |

## Infrastructure
| Spec | File | Summary |
|------|------|---------|
| Containers | `specs/containers.md` | All Docker services: names, ports, volumes, build context |
| Logging | `specs/logging.md` | Structured logging levels, silence rules, rotation config |
| Federation | `specs/federation.md` | Connecting an edge mind to a second hive — broker crosses, memory doesn't. Specified, not implemented |

## Skills
| Spec | File | Summary |
|------|------|---------|
| Skill reference copies | `specs/skills/` | Spec copies of the mind's core skills: memory, rotate-session, end-session, add-hive-tool |

## Voice
| Spec | File | Summary |
|------|------|---------|
| Chatterbox TTS | `specs/chatterbox.md` | Working synthesis code reference for the Chatterbox engine |
