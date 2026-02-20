# cloud-node-pod-cleanup

Baseline OpenStack node lifecycle and Kubernetes duplicate-pod cleanup workflow used as the predecessor to the hardened `cloud-node-pod-cleanup-tool` version.

## Problem
Node transitions can leave stale pods and inconsistent scheduling state. Operations teams need a repeatable workflow to start/stop nodes and clean duplicates quickly.

## Security Context
- Supports operational hygiene after node transitions.
- Reduces risk of orphaned workloads and misaligned runtime state.
- Provides logs that can support incident reconstruction.

## Architecture/Flow
Flow summary:
1. Connect to OpenStack cloud.
2. Locate target node(s) by partial name match.
3. Execute start/stop action with status checks.
4. Run Kubernetes duplicate-pod cleanup by namespace.
5. Write operational logs.

## Setup
```bash
git clone git@github.com:jaskaranhundal/cloud-node-pod-cleanup.git
cd cloud-node-pod-cleanup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python control_and_cleanup.py start
```

## Example Output
```text
Connected to OpenStack cloud 'otc'
Found 1 server(s) matching 'node2'
Starting node2...
Server is now ACTIVE
Duplicate pods cleaned in 3 namespaces
```

## Limitations
- Uses static inline configuration in script by default.
- Limited retry/error handling compared with the newer tool repository.
- No structured JSON log export.

## Roadmap
- Keep this repository as a baseline reference.
- Migrate active improvements to `cloud-node-pod-cleanup-tool`.
- Add compatibility notes between baseline and hardened versions.
