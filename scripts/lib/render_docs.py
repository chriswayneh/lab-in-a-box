#!/usr/bin/env python3
"""Render the service catalogue and dependency graph from a compose project.

Reads `docker compose config --format json` on stdin. Invoked by
scripts/generate-docs.sh; not intended to be run directly.

Kept in Python rather than shell because the input is deeply nested JSON, and
the shell version of this was three hundred lines of jq that nobody could
safely change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

# Which fragment each service belongs to, for grouping in the catalogue. The
# compose model does not record which file a service came from, so this mirrors
# compose/ by hand — the one place duplication was worth accepting.
LAYERS: list[tuple[str, str, list[str]]] = [
    ("Core", "Edge routing, data stores and the landing page", [
        "traefik", "socket-proxy", "postgres", "redis", "landing",
    ]),
    ("Identity & Secrets", "Who you are, and what you are allowed to know", [
        "keycloak", "keycloak-init", "vault", "vault-init",
    ]),
    ("Observability", "Metrics, logs and dashboards", [
        "prometheus", "grafana", "loki", "promtail", "cadvisor", "node-exporter",
    ]),
    ("AI", "Local model serving and chat", [
        "ollama", "ollama-init", "open-webui", "qdrant",
    ]),
    ("Platform", "Source control and object storage", [
        "gitea", "gitea-init", "minio", "minio-init",
    ]),
    ("Tools", "Operator conveniences", [
        "portainer", "pgadmin", "adminer", "watchtower",
    ]),
]

# Traefik router rules look like: Host(`grafana.lab.localhost`)
HOST_RULE = re.compile(r"Host\(`([^`]+)`\)")


def service_url(service: dict[str, Any]) -> str:
    """Extract the public hostname a service is routed at, if any."""
    labels = service.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(
            item.split("=", 1) for item in labels if "=" in item
        )

    hosts = []
    for key, value in labels.items():
        if ".rule" in key and isinstance(value, str):
            hosts.extend(HOST_RULE.findall(value))

    return ", ".join(f"`{h}`" for h in sorted(set(hosts))) or "—"


def published_ports(service: dict[str, Any]) -> str:
    """Host ports this service binds directly, bypassing the edge router."""
    entries = []
    for port in service.get("ports") or []:
        if isinstance(port, dict):
            published = port.get("published")
            target = port.get("target")
            if published:
                entries.append(f"`{published}→{target}`")
        elif isinstance(port, str):
            entries.append(f"`{port}`")
    return ", ".join(entries) or "—"


def networks_of(service: dict[str, Any]) -> str:
    nets = service.get("networks") or {}
    names = sorted(nets.keys()) if isinstance(nets, dict) else sorted(nets)
    return ", ".join(n.replace("lab_", "") for n in names) or "—"


def dependencies_of(service: dict[str, Any]) -> list[str]:
    depends = service.get("depends_on") or {}
    if isinstance(depends, dict):
        return sorted(depends.keys())
    return sorted(depends)


def security_notes(name: str, service: dict[str, Any]) -> str:
    """Summarise the privilege posture, so the risky containers are obvious."""
    notes = []

    if service.get("privileged"):
        notes.append("**privileged**")

    if service.get("user"):
        notes.append(f"uid `{service['user']}`")

    for volume in service.get("volumes") or []:
        source = volume.get("source") if isinstance(volume, dict) else str(volume)
        if source and "docker.sock" in str(source):
            read_only = isinstance(volume, dict) and volume.get("read_only")
            notes.append("docker socket (ro)" if read_only else "**docker socket (rw)**")

    if service.get("read_only"):
        notes.append("read-only fs")

    for cap in service.get("cap_add") or []:
        notes.append(f"cap `{cap}`")

    return ", ".join(notes) or "—"


def render_services(config: dict[str, Any], domain: str) -> str:
    services = config.get("services", {})
    categorised = {name for _, _, names in LAYERS for name in names}
    uncategorised = sorted(set(services) - categorised)
    optional_services = sorted(
        name for name, service in services.items() if service.get("profiles")
    )
    default_service_count = len(services) - len(optional_services)

    out: list[str] = []
    out.append("<!--")
    out.append("  GENERATED FILE — do not edit by hand.")
    out.append("  Regenerate with: make docs")
    out.append("  Source of truth: docker-compose.yml and compose/*.yml")
    out.append("-->")
    out.append("")
    out.append("# Service catalogue")
    out.append("")
    out.append(
        f"Every container in the lab, grouped by the compose fragment that "
        f"defines it. Hostnames assume `LAB_DOMAIN={domain}`."
    )
    out.append("")
    service_summary = f"**{len(services)} available services** across **{len(LAYERS)} layers**. "
    if optional_services:
        service_summary += (
            f"**{default_service_count} start by default**; "
            f"{', '.join(f'`{name}`' for name in optional_services)} require "
            "their optional Compose profiles. "
        )
    service_summary += (
        "Services whose name ends in `-init` are one-shot provisioning jobs: "
        "they run once, do their work, and exit. A stopped `-init` container "
        "is a success, not a fault."
    )
    out.append(service_summary)
    out.append("")

    groups = list(LAYERS)
    if uncategorised:
        groups.append(("Other", "Not yet categorised", uncategorised))

    for title, description, names in groups:
        present = [n for n in names if n in services]
        if not present:
            continue

        out.append(f"## {title}")
        out.append("")
        out.append(f"_{description}_")
        out.append("")
        out.append("| Service | Image | URL | Host ports | Networks | Health | Privileges |")
        out.append("| --- | --- | --- | --- | --- | :---: | --- |")

        for name in present:
            svc = services[name]
            image = svc.get("image", "—")
            health = "✅" if svc.get("healthcheck") else ("n/a" if name.endswith("-init") else "❌")
            out.append(
                f"| `{name}` "
                f"| `{image}` "
                f"| {service_url(svc)} "
                f"| {published_ports(svc)} "
                f"| {networks_of(svc)} "
                f"| {health} "
                f"| {security_notes(name, svc)} |"
            )
        out.append("")

    out.append("## Networks")
    out.append("")
    out.append("| Network | Isolated | Purpose |")
    out.append("| --- | :---: | --- |")
    purposes = {
        "lab_edge": "Public-facing. Traefik and everything it routes.",
        "lab_data": "PostgreSQL, Redis and their clients. No internet access.",
        "lab_observability": "Scrape targets, log shippers and their backends.",
        "lab_ai": "Model serving. Needs egress to pull model weights.",
        "lab_socket": "Read-only Docker API, via the socket proxy.",
    }
    for net_name, net in sorted((config.get("networks") or {}).items()):
        isolated = "🔒" if net.get("internal") else "—"
        out.append(f"| `{net_name}` | {isolated} | {purposes.get(net_name, '')} |")
    out.append("")

    out.append("## Volumes")
    out.append("")
    volumes = sorted((config.get("volumes") or {}).keys())
    out.append(
        f"{len(volumes)} named volumes hold all persistent state: "
        + ", ".join(f"`{v}`" for v in volumes)
        + "."
    )
    out.append("")
    out.append(
        "`make down` preserves every one of them. Only `make clean` removes "
        "them, and it asks first."
    )
    out.append("")

    return "\n".join(out)


def render_graph(config: dict[str, Any]) -> str:
    """Emit a Mermaid graph of the startup dependency order."""
    services = config.get("services", {})

    layer_of = {
        name: title
        for title, _, names in LAYERS
        for name in names
    }

    out: list[str] = []
    out.append("%% GENERATED FILE — do not edit by hand. Regenerate with: make docs")
    out.append("%% Renders natively on GitHub inside a ```mermaid fence.")
    out.append("graph TD")

    # Group nodes into subgraphs so the picture reads as layers, not a hairball.
    for title, _, names in LAYERS:
        present = [n for n in names if n in services]
        if not present:
            continue
        safe_title = title.replace(" & ", " and ")
        out.append(f'    subgraph {safe_title.replace(" ", "_")}["{title}"]')
        for name in present:
            node = name.replace("-", "_")
            shape = f'{node}(["{name}"])' if name.endswith("-init") else f'{node}["{name}"]'
            out.append(f"        {shape}")
        out.append("    end")

    out.append("")

    edges = []
    for name in sorted(services):
        for dep in dependencies_of(services[name]):
            if dep in services:
                edges.append(f"    {dep.replace('-', '_')} --> {name.replace('-', '_')}")
    out.extend(edges)

    out.append("")
    out.append("    classDef initJob fill:#1e293b,stroke:#64748b,stroke-width:1px,color:#cbd5e1;")
    init_nodes = [n.replace("-", "_") for n in sorted(services) if n.endswith("-init")]
    if init_nodes:
        out.append(f"    class {','.join(init_nodes)} initJob;")

    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services-out", required=True)
    parser.add_argument("--graph-out", required=True)
    parser.add_argument("--domain", default="lab.localhost")
    args = parser.parse_args()

    try:
        config = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"could not parse compose output: {exc}", file=sys.stderr)
        return 1

    with open(args.services_out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_services(config, args.domain))

    with open(args.graph_out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_graph(config))

    return 0


if __name__ == "__main__":
    sys.exit(main())
