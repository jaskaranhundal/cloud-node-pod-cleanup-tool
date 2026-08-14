#!/usr/bin/env python3
"""OTC node stop/start with graceful cordon + drain and health-gated uncordon."""
import os
import sys
import time
import re
import logging
from datetime import datetime

import pytz
from dotenv import load_dotenv

load_dotenv()

# clouds.yml lives next to this script unless overridden
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["OS_CLIENT_CONFIG_FILE"] = os.getenv(
    "OS_CONFIG_FILE", os.path.join(BASE_DIR, "clouds.yml")
)

import openstack
from kubernetes import client, config

from notifications import (
    notify_success, notify_error, notify_warning,
    notify_auth_failure, notify_script_start, notify_script_complete,
)

# --- Configuration (env-driven, production defaults) ---
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_DIR = os.getenv("LOG_DIR", os.path.join(BASE_DIR, "log"))
PARTIAL_SERVER_NAME = os.getenv("PARTIAL_SERVER_NAME", "node-2")
CLOUD_NAME = os.getenv("CLOUD_NAME", "otc")
KUBECONFIG_PATH = os.getenv("KUBECONFIG_PATH", os.path.join(BASE_DIR, ".kube", "config"))
# Deliberately no default: this tool deletes pods, so an unset or misspelled
# variable must stop the run rather than fall back to some other cluster's
# namespaces. Read as NAMESPACES to match config.example and the README.
NAMESPACES = [ns.strip() for ns in os.getenv("NAMESPACES", "").split(",") if ns.strip()]

SERVER_TIMEOUT = int(os.getenv("SERVER_START_TIMEOUT", "300"))
NODE_READY_TIMEOUT = int(os.getenv("NODE_READY_TIMEOUT", "600"))
DRAIN_TIMEOUT = int(os.getenv("DRAIN_TIMEOUT", "180"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

os.makedirs(LOG_DIR, exist_ok=True)

success_count = 0
error_count = 0
LOG_FILE = None
KUBE_LOG_FILE = None


def setup_logging(action):
    global LOG_FILE, KUBE_LOG_FILE
    date_str = datetime.now(BERLIN_TZ).strftime("%Y-%m-%d")
    LOG_FILE = os.path.join(LOG_DIR, f"{date_str}_{action}.log")
    KUBE_LOG_FILE = os.path.join(LOG_DIR, f"{date_str}_k8s_cleanup_{action}.log")
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(message)s")


def log(msg):
    print(msg)
    logging.info(msg)


# --- OpenStack ---
def connect():
    try:
        conn = openstack.connect(cloud=CLOUD_NAME)
        list(conn.identity.projects(limit=1))  # fail fast on bad creds
        return conn
    except Exception as e:
        msg = str(e)
        log(f"OpenStack connection failed: {msg}")
        if "401" in msg or "Unauthorized" in msg:
            notify_auth_failure(msg)
        else:
            notify_error("OpenStack Connection Failed", f"Failed to connect: {msg}",
                         details={"Cloud": CLOUD_NAME, "Error": msg})
        return None


def find_servers(conn, partial_name):
    try:
        return [s for s in conn.compute.servers() if partial_name in s.name]
    except Exception as e:
        log(f"Error finding servers: {e}")
        notify_error("Server Discovery Failed", f"Failed to list servers: {e}")
        return []


def wait_for_server_status(conn, server_id, desired, timeout, poll=POLL_INTERVAL):
    waited = 0
    while waited < timeout:
        try:
            if conn.compute.get_server(server_id).status.lower() == desired.lower():
                return True
        except Exception as e:
            log(f"Error checking server status: {e}")
        time.sleep(poll)
        waited += poll
    return False


def get_server_ip(conn, server):
    try:
        for network in server.addresses.values():
            for addr in network:
                if addr.get("OS-EXT-IPS:type") == "fixed":
                    return addr["addr"]
    except Exception as e:
        log(f"Error getting server IP: {e}")
    return None


# --- Kubernetes ---
def load_k8s():
    """Return a CoreV1Api, or None if no config can be loaded."""
    try:
        try:
            config.load_incluster_config()
            log("Loaded in-cluster Kubernetes config")
        except Exception:
            config.load_kube_config(config_file=KUBECONFIG_PATH)
            log(f"Loaded kube config from {KUBECONFIG_PATH}")
        return client.CoreV1Api()
    except Exception as e:
        log(f"Failed to load Kubernetes config: {e}")
        notify_warning("Kubernetes Config Failed", f"Could not load kube config: {e}")
        return None


def find_node_by_ip(v1, server_ip):
    try:
        for node in v1.list_node().items:
            for addr in node.status.addresses:
                if addr.address == server_ip:
                    return node.metadata.name
    except Exception as e:
        log(f"Error finding node by IP: {e}")
    return None


def is_node_ready(v1, node_name):
    try:
        node = v1.read_node(name=node_name)
        for cond in node.status.conditions:
            if cond.type == "Ready":
                return cond.status == "True"
    except Exception as e:
        log(f"Failed to read node {node_name}: {e}")
    return False


def wait_for_node_ready(v1, node_name, timeout=NODE_READY_TIMEOUT, poll=POLL_INTERVAL):
    waited = 0
    while waited < timeout:
        if is_node_ready(v1, node_name):
            return True
        time.sleep(poll)
        waited += poll
    return False


def cordon_node(v1, node_name):
    v1.patch_node(node_name, {"spec": {"unschedulable": True}})
    log(f"Cordoned {node_name}")


def uncordon_node(v1, node_name):
    v1.patch_node(node_name, {"spec": {"unschedulable": False}})
    log(f"Uncordoned {node_name}")


def _is_evictable(pod):
    """Skip DaemonSet-owned, static-mirror, and already-terminating pods."""
    owners = pod.metadata.owner_references or []
    if any(o.kind == "DaemonSet" for o in owners):
        return False
    if "kubernetes.io/config.mirror" in (pod.metadata.annotations or {}):
        return False
    return pod.metadata.deletion_timestamp is None


def drain_node(v1, node_name, timeout=DRAIN_TIMEOUT, poll=POLL_INTERVAL):
    """Evict workload pods off the node (respects PodDisruptionBudgets)."""
    fs = f"spec.nodeName={node_name}"
    for pod in filter(_is_evictable, v1.list_pod_for_all_namespaces(field_selector=fs).items):
        body = client.V1Eviction(metadata=client.V1ObjectMeta(
            name=pod.metadata.name, namespace=pod.metadata.namespace))
        try:
            v1.create_namespaced_pod_eviction(pod.metadata.name, pod.metadata.namespace, body)
            log(f"Evicted {pod.metadata.namespace}/{pod.metadata.name}")
        except Exception as e:
            log(f"Eviction failed for {pod.metadata.name}: {e}")
    waited = 0
    while waited < timeout:
        remaining = list(filter(_is_evictable,
                                v1.list_pod_for_all_namespaces(field_selector=fs).items))
        if not remaining:
            return True
        time.sleep(poll)
        waited += poll
    return False


# --- Stop ---
def stop_server():
    global success_count, error_count
    conn = connect()
    if not conn:
        error_count += 1
        return
    servers = find_servers(conn, PARTIAL_SERVER_NAME)
    if not servers:
        notify_warning("No Servers Found",
                       f"No servers matching '{PARTIAL_SERVER_NAME}'", "stop")
        return

    notify_script_start("stop", len(servers))
    v1 = load_k8s()

    for server in servers:
        try:
            if server.status.lower() == "shutoff":
                log(f"Server already stopped: {server.name}")
                success_count += 1
                continue

            # graceful drain before power-off
            node_name = find_node_by_ip(v1, get_server_ip(conn, server)) if v1 else None
            if node_name:
                cordon_node(v1, node_name)
                if drain_node(v1, node_name):
                    log(f"Drained node {node_name}")
                else:
                    log(f"Drain timed out for {node_name}; stopping anyway")
                    notify_warning("Drain Timeout",
                                   f"Node '{node_name}' not fully drained in {DRAIN_TIMEOUT}s",
                                   "stop", {"Node": node_name})
            else:
                notify_warning("Stop Without Drain",
                               f"Could not map '{server.name}' to a node; stopping without drain",
                               "stop", {"Server": server.name})

            conn.compute.stop_server(server.id)
            log(f"Stopping server: {server.name}")
            if wait_for_server_status(conn, server.id, "shutoff", SERVER_TIMEOUT):
                notify_success("Server Stopped", f"Server '{server.name}' stopped", "stop",
                               {"Server Name": server.name, "Node": node_name or "unknown"})
                success_count += 1
            else:
                notify_error("Server Stop Timeout",
                             f"Server '{server.name}' did not reach SHUTOFF in {SERVER_TIMEOUT}s",
                             "stop", {"Server Name": server.name})
                error_count += 1
        except Exception as e:
            log(f"Error stopping server {server.name}: {e}")
            notify_error("Server Stop Failed", f"Failed to stop '{server.name}': {e}",
                         "stop", {"Server Name": server.name, "Error": str(e)})
            error_count += 1


# --- Start ---
def start_server():
    global success_count, error_count
    conn = connect()
    if not conn:
        error_count += 1
        return
    servers = find_servers(conn, PARTIAL_SERVER_NAME)
    if not servers:
        notify_warning("No Servers Found",
                       f"No servers matching '{PARTIAL_SERVER_NAME}'", "start")
        return

    notify_script_start("start", len(servers))
    v1 = load_k8s()

    for server in servers:
        try:
            if server.status.lower() == "shutoff":
                conn.compute.start_server(server.id)
                log(f"Starting server: {server.name} ...")
                if not wait_for_server_status(conn, server.id, "active", SERVER_TIMEOUT):
                    notify_error("Server Start Timeout",
                                 f"Server '{server.name}' did not become ACTIVE in {SERVER_TIMEOUT}s",
                                 "start", {"Server Name": server.name})
                    error_count += 1
                    continue
                log(f"Server {server.name} is active.")
                success_count += 1
            else:
                log(f"Server already running: {server.name}")
                success_count += 1

            if v1:
                handle_node_readiness(conn, server, v1)
            else:
                notify_warning("Skipped K8s Recovery",
                               "Kubernetes unavailable; node left as-is", "start",
                               {"Server": server.name})
        except Exception as e:
            log(f"Error starting server {server.name}: {e}")
            notify_error("Server Start Failed", f"Failed to start '{server.name}': {e}",
                         "start", {"Server Name": server.name, "Error": str(e)})
            error_count += 1


def handle_node_readiness(conn, server, v1):
    """Uncordon + clean up on Ready; alert and stay cordoned on timeout."""
    global error_count
    node_name = find_node_by_ip(v1, get_server_ip(conn, server))
    if not node_name:
        notify_warning("Node Not Found",
                       f"No cluster node matches '{server.name}'; cannot uncordon",
                       "start", {"Server": server.name})
        return

    log(f"Waiting for node '{node_name}' to become Ready...")
    if wait_for_node_ready(v1, node_name):
        uncordon_node(v1, node_name)
        notify_success("Node Ready", f"Node '{node_name}' Ready and uncordoned",
                       "start", {"Node": node_name})
        cleanup_stuck_pods("start", v1)
    else:
        # broken node stays cordoned so it cannot receive workloads
        notify_error("Node Ready Timeout",
                     f"Node '{node_name}' started but never became Ready in "
                     f"{NODE_READY_TIMEOUT}s — left cordoned. Possible OTC network fault.",
                     "start", {"Node": node_name})
        error_count += 1


# --- Cleanup (safe: never deletes a healthy replica) ---
def get_base_name(pod_name):
    parts = pod_name.split("-")
    base = parts[0]
    for i in range(1, len(parts)):
        if len(parts[i]) < 4 or not re.match(r"^[a-z0-9]+$", parts[i]):
            base += "-" + parts[i]
        else:
            break
    return base


def _is_healthy(pod):
    if pod.metadata.deletion_timestamp is not None:
        return False
    if pod.status.phase != "Running":
        return False
    for cond in (pod.status.conditions or []):
        if cond.type == "Ready":
            return cond.status == "True"
    return False


def cleanup_stuck_pods(action, v1):
    """Delete only unhealthy duplicates, and only when a healthy replica survives."""
    k8s_logger = logging.getLogger("k8s_cleanup")
    k8s_logger.setLevel(logging.INFO)
    if not k8s_logger.hasHandlers():
        h = logging.FileHandler(KUBE_LOG_FILE)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        k8s_logger.addHandler(h)

    def klog(msg):
        print(msg)
        k8s_logger.info(msg)

    groups = {}
    for ns in NAMESPACES:
        try:
            pods = v1.list_namespaced_pod(namespace=ns).items
        except Exception as e:
            klog(f"Failed to list pods in {ns}: {e}")
            continue
        for pod in pods:
            key = f"{ns}:{get_base_name(pod.metadata.name)}"
            groups.setdefault(key, []).append(pod)

    to_delete = []
    for key, pods in groups.items():
        if len(pods) <= 1:
            continue
        healthy = [p for p in pods if _is_healthy(p)]
        unhealthy = [p for p in pods if not _is_healthy(p)]
        if healthy and unhealthy:  # only prune when a good replica remains
            to_delete.extend(unhealthy)

    if not to_delete:
        klog("No unhealthy duplicate pods to delete.")
        notify_success("Pod Cleanup Complete", "No stuck duplicates found", action,
                       {"Pods Deleted": 0})
        return

    deleted, failed = 0, 0
    for pod in to_delete:
        try:
            v1.delete_namespaced_pod(name=pod.metadata.name, namespace=pod.metadata.namespace)
            klog(f"Deleted stuck pod {pod.metadata.namespace}/{pod.metadata.name}")
            deleted += 1
        except Exception as e:
            klog(f"Failed to delete {pod.metadata.name}: {e}")
            failed += 1

    if failed:
        notify_warning("Pod Cleanup Partial",
                       f"Deleted {deleted}, failed {failed}", action,
                       {"Deleted": deleted, "Failed": failed})
    else:
        notify_success("Pod Cleanup Complete", f"Deleted {deleted} stuck pods", action,
                       {"Pods Deleted": deleted})


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "stop"):
        print("Usage: python control_and_cleanup.py [start|stop]")
        sys.exit(1)

    if not NAMESPACES:
        print(
            "Error: NAMESPACES is empty. Set it to the comma-separated namespaces "
            "to clean up, e.g. NAMESPACES=team-production,team-staging",
            file=sys.stderr,
        )
        sys.exit(2)

    action = sys.argv[1]
    setup_logging(action)
    log(f"Script called with action: {action}")

    if action == "start":
        start_server()
    else:
        stop_server()

    notify_script_complete(action, success_count, error_count)
    log(f"Completed. Success: {success_count}, Errors: {error_count}")


if __name__ == "__main__":
    main()
