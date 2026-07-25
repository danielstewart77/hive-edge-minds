# Federation — connecting an outpost to a second hive

**Status: specified, not implemented.** No code in this repo speaks to more
than one hive. This spec exists so the first implementation starts from a
security boundary, not from plumbing.

## The shape of the problem

Every outpost today points at exactly one hive: one `COMMS_URL`, one
`LUCENT_URL_SELF`, one bearer token for each. Federation is the ability to
connect the same outpost to a *second* hive — a friend's, a second site of
your own — without collapsing the two into one trust domain.

## What crosses, what doesn't

The boundary is the entire feature:

- **Broker messages cross.** Inter-mind messaging is the point of
  federating: a mind on hive A can address a mind on hive B and get a
  reply. Each message carries its origin hive and arrives through the
  remote hive's HITL policy like any untrusted inbound.
- **Memory does not cross.** A mind's soul, vector store, and knowledge
  graph live in its *home* hive only. The federated hive never receives
  memory writes. If a future use case wants shared knowledge, it gets an
  explicitly scoped namespace on the remote lucent — opt-in, per
  agreement, never the default — and the home hive's identity guard still
  rejects any write to another mind's nodes.
- **Sessions do not cross.** A federated hive cannot spawn, adopt, or
  attach to sessions on this outpost. Session ownership stays with the
  home hive's comms.

## Wiring (when implemented)

`.env` grows a per-federation block; each remote hive gets its own scoped
bearer, minted by that hive's operator and revocable independently:

```
FEDERATION_1_NAME=aliceshive
FEDERATION_1_COMMS_URL=https://hive.example.com:8426
FEDERATION_1_BEARER_TOKEN=<scoped token minted by that hive>
```

The scoped token authorizes broker endpoints only — it must not be the
remote hive's general `COMMS_BEARER_TOKEN`. A hive that wants to accept
federated peers exposes a broker-only token class; that change lives in
hive-comms (hive-mind repo), not here.

## Threat notes for the implementer

- Treat every inbound federated message as untrusted user input:
  prompt-injection defense per `specs/security.md` applies with no
  home-hive exemption.
- Rate-limit per federation, not globally, so one noisy peer cannot starve
  local traffic.
- Log origin hive on every crossed message; a federated conversation must
  be reconstructable from one side.
- Revocation is deletion of one env block + token invalidation on the
  remote side. No shared state may outlive the token.
