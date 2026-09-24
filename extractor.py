import json
import sys
import docker


def extract_topology():
    try:
        client = docker.from_env()
        # Verify connection to Docker daemon
        client.ping()
    except Exception as exc:
        print(
            f"Error: Could not connect to Docker daemon: {exc}", file=sys.stderr
        )
        sys.exit(1)

    nodes = []
    edges = []

    # 1. Collect all Docker Networks
    for network in client.networks.list():
        network.reload()
        ipam_configs = network.attrs.get("IPAM", {}).get("Config", [])
        subnet = (
            ipam_configs[0].get("Subnet", "N/A") if ipam_configs else "N/A"
        )

        nodes.append({
            "id": f"net_{network.id[:12]}",
            "name": network.name,
            "type": "network",
            "driver": network.attrs.get("Driver", "bridge"),
            "subnet": subnet,
        })

    # 2. Collect Containers and establish Edges
    for container in client.containers.list(all=True):
        container_node_id = f"cnt_{container.id[:12]}"

        # Parse port mappings
        ports = []
        port_settings = (
            container.attrs.get("NetworkSettings", {}).get("Ports") or {}
        )
        for internal_port, host_bindings in port_settings.items():
            if host_bindings:
                for b in host_bindings:
                    ports.append(f"{b.get('HostPort')}->{internal_port}")
            else:
                ports.append(internal_port)

        nodes.append({
            "id": container_node_id,
            "name": container.name,
            "type": "container",
            "image": container.image.tags[0]
            if container.image.tags
            else "untagged",
            "status": container.status,
            "ports": ports,
        })

        # Connect container to its networks
        net_settings = (
            container.attrs.get("NetworkSettings", {}).get("Networks") or {}
        )
        for net_name, net_data in net_settings.items():
            net_id = net_data.get("NetworkID", "")[:12]
            if net_id:
                edges.append({
                    "source": container_node_id,
                    "target": f"net_{net_id}",
                    "ip_address": net_data.get("IPAddress", ""),
                })

    return {"nodes": nodes, "edges": edges}


if __name__ == "__main__":
    topology = extract_topology()
    print(json.dumps(topology, indent=2))