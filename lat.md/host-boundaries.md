# Host and deployment boundaries

The repository distinguishes cycle integration, candidate promotion, and activation of a release on the constrained host.

## Separate authorities

Cycle integration, candidate promotion, and host activation are separate actions with separate gates.

The bridge's cycle branch and gate govern integration into the self-evolving instance repository. Promotion candidates follow a distinct verification and review path. Host deployment selects a release artifact, activates it, checks health, and can roll back under the contract in [[docs/specs/host-runtime/spec.md]] and [[docs/specs/promotion-and-release/spec.md]]. The relevant checked-in entrypoint is [[host/eeepc/scripts/deploy_release.sh]].

Runtime state and host evidence live outside source control and are not inferred from source code or repository CI. This graph describes checked-in contracts and mechanisms only; it makes no claim about current host topology, deployed SHA, health, or operator approvals.
