import openstack
import pytz
import sys
import logging
import os
from datetime import datetime
import time
import re
from kubernetes import client, config
from kubernetes.config import ConfigException

# --- Configurations ---
BERLIN_TZ = pytz.timezone("Europe/Berlin")
LOG_FILE = "server_control.log"
KUBE_LOG_FILE = "k8s_cleanup.log"

# Use environment variables with defaults
PARTIAL_SERVER_NAME = os.getenv("PARTIAL_SERVER_NAME", "node2")
CLOUD_NAME = os.getenv("CLOUD_NAME", "otc")
NAMESPACES = os.getenv("NAMESPACES", "lindera-production,lindera-testing,lindera-development").split(",")

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 5

# --- Setup unified logging ---
def setup_logging():
    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

def log(msg, level="info"):
    """Unified logging function"""
    if level == "info":
        logger.info(msg)
    elif level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "debug":
        logger.debug(msg)

# --- Utility functions ---
def retry_operation(operation, max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    """Retry an operation with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return operation()
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            log(f"Operation failed (attempt {attempt + 1}/{max_retries}): {e}", "warning")
            time.sleep(delay * (2 ** attempt))
    return None

# --- OpenStack functions ---
def connect():
    """Connect to OpenStack with validation"""
    try:
        conn = openstack.connect(cloud=CLOUD_NAME)
        # Test connection by listing servers
        list(conn.compute.servers(limit=1))
        log(f"Successfully connected to OpenStack cloud: {CLOUD_NAME}")
        return conn
    except Exception as e:
        log(f"Failed to connect to OpenStack cloud '{CLOUD_NAME}': {e}", "error")
        raise

def find_servers(conn, partial_name):
    """Find servers by partial name with error handling"""
    try:
        servers_found = []
        for server in conn.compute.servers():
            if partial_name in server.name:
                servers_found.append(server)
        log(f"Found {len(servers_found)} servers matching '{partial_name}'")
        return servers_found
    except Exception as e:
        log(f"Failed to find servers with name containing '{partial_name}': {e}", "error")
        raise

def wait_for_server_status(conn, server_id, desired_status, timeout=300, poll_interval=10):
    """Wait for server to reach desired status with better error handling"""
    waited = 0
    while waited < timeout:
        try:
            server = conn.compute.get_server(server_id)
            if server.status.lower() == desired_status.lower():
                log(f"Server {server_id} reached status: {desired_status}")
                return True
            time.sleep(poll_interval)
            waited += poll_interval
        except Exception as e:
            log(f"Error checking server status: {e}", "warning")
            time.sleep(poll_interval)
            waited += poll_interval
    log(f"Timeout waiting for server {server_id} to reach status {desired_status}", "error")
    return False

def stop_server():
    """Stop servers with proper error handling"""
    try:
        conn = connect()
        servers = find_servers(conn, PARTIAL_SERVER_NAME)
        
        if not servers:
            log(f"No servers found with name containing '{PARTIAL_SERVER_NAME}'")
            return
        
        for server in servers:
            try:
                if server.status.lower() != "shutoff":
                    log(f"Stopping server: {server.name}")
                    conn.compute.stop_server(server.id)
                    log(f"Stop command sent for server: {server.name}")
                else:
                    log(f"Server already stopped: {server.name}")
            except Exception as e:
                log(f"Failed to stop server {server.name}: {e}", "error")
                
    except Exception as e:
        log(f"Failed to stop servers: {e}", "error")
        raise

def get_server_ip(conn, server):
    """Get server IP with better error handling"""
    try:
        addresses = server.addresses
        for network in addresses.values():
            for addr_info in network:
                if addr_info.get('OS-EXT-IPS:type') == 'fixed':
                    return addr_info['addr']
        log(f"Could not find fixed IP for server {server.name}", "warning")
        return None
    except Exception as e:
        log(f"Error getting IP for server {server.name}: {e}", "error")
        return None

# --- Kubernetes functions ---
def setup_kubernetes_client():
    """Setup Kubernetes client with proper error handling"""
    try:
        try:
            config.load_incluster_config()
            log("Using in-cluster Kubernetes configuration")
        except ConfigException:
            config.load_kube_config()
            log("Using kubeconfig file")
        return client.CoreV1Api()
    except Exception as e:
        log(f"Failed to load Kubernetes config: {e}", "error")
        raise

def is_node_ready(node_name):
    """Check if node is ready with error handling"""
    try:
        v1 = client.CoreV1Api()
        node = v1.read_node(name=node_name)
        for condition in node.status.conditions:
            if condition.type == "Ready":
                return condition.status == "True"
        return False
    except Exception as e:
        log(f"Failed to get node status for {node_name}: {e}", "error")
        return False

def wait_for_node_ready(node_name, timeout=600, poll_interval=10):
    """Wait for node to become ready with better error handling"""
    waited = 0
    while waited < timeout:
        if is_node_ready(node_name):
            log(f"Node '{node_name}' is Ready")
            return True
        time.sleep(poll_interval)
        waited += poll_interval
    log(f"Timeout waiting for node '{node_name}' to become Ready", "error")
    return False

def start_server():
    """Start servers with comprehensive error handling"""
    try:
        conn = connect()
        servers = find_servers(conn, PARTIAL_SERVER_NAME)
        
        if not servers:
            log(f"No servers found with name containing '{PARTIAL_SERVER_NAME}'")
            return

        # Setup Kubernetes client once
        try:
            v1 = setup_kubernetes_client()
        except Exception as e:
            log(f"Kubernetes setup failed, proceeding without pod cleanup: {e}", "warning")
            v1 = None

        for server in servers:
            try:
                if server.status.lower() == "shutoff":
                    log(f"Starting server: {server.name} ...")
                    conn.compute.start_server(server.id)
                    
                    if wait_for_server_status(conn, server.id, "active"):
                        log(f"Server {server.name} is active")
                        
                        if v1:
                            server_ip = get_server_ip(conn, server)
                            if server_ip:
                                node_name = find_node_by_ip(v1, server_ip)
                                if node_name:
                                    log(f"Waiting for node '{node_name}' to become Ready...")
                                    if wait_for_node_ready(node_name):
                                        log(f"Node '{node_name}' is Ready. Starting pod cleanup.")
                                        cleanup_duplicate_pods()
                                    else:
                                        log(f"Node '{node_name}' did not become ready in time", "warning")
                                else:
                                    log(f"Node with IP {server_ip} not found in cluster", "warning")
                                    cleanup_duplicate_pods()
                            else:
                                log(f"Could not determine IP for server {server.name}", "warning")
                                cleanup_duplicate_pods()
                        else:
                            log("Kubernetes not available, skipping pod cleanup")
                    else:
                        log(f"Timeout waiting for server {server.name} to become active", "error")
                else:
                    log(f"Server already running: {server.name}")
                    if v1:
                        cleanup_duplicate_pods()
                        
            except Exception as e:
                log(f"Failed to start server {server.name}: {e}", "error")
                
    except Exception as e:
        log(f"Failed to start servers: {e}", "error")
        raise

def find_node_by_ip(v1, server_ip):
    """Find node name by IP address"""
    try:
        nodes = v1.list_node().items
        for node in nodes:
            for addr in node.status.addresses:
                if addr.address == server_ip:
                    return node.metadata.name
        return None
    except Exception as e:
        log(f"Failed to list nodes: {e}", "error")
        return None

# --- Kubernetes cleanup ---
def get_base_name(pod_name):
    """Extract base name from pod name"""
    parts = pod_name.split('-')
    base = parts[0]
    for i in range(1, len(parts)):
        if len(parts[i]) < 4 or not re.match(r'^[a-z0-9]+$', parts[i]):
            base += '-' + parts[i]
        else:
            break
    return base

def cleanup_duplicate_pods():
    """Clean up duplicate pods with improved error handling"""
    log("Starting Kubernetes pod cleanup...")
    
    try:
        v1 = setup_kubernetes_client()
    except Exception as e:
        log(f"Failed to setup Kubernetes client for cleanup: {e}", "error")
        return

    pod_groups = {}

    for ns in NAMESPACES:
        try:
            pods = v1.list_namespaced_pod(namespace=ns).items
            log(f"Found {len(pods)} pods in namespace {ns}")
        except Exception as e:
            log(f"Failed to list pods in namespace {ns}: {e}", "error")
            continue

        for pod in pods:
            if not pod.status.start_time or not pod.status.host_ip:
                continue

            name = pod.metadata.name
            age_seconds = (datetime.now(pytz.UTC) - pod.status.start_time).total_seconds()
            node_ip = pod.status.host_ip
            base_name = get_base_name(name)
            key = f"{ns}:{base_name}"
            pod_groups.setdefault(key, []).append({
                "namespace": ns,
                "name": name,
                "node_ip": node_ip,
                "age": age_seconds
            })

    pods_to_delete = []
    for group, pod_list in pod_groups.items():
        node_ips = set(p["node_ip"] for p in pod_list)
        if len(pod_list) > 1 and len(node_ips) <= 2:
            pod_list_sorted = sorted(pod_list, key=lambda x: x["age"])
            to_delete = pod_list_sorted[0]
            pods_to_delete.append((to_delete["namespace"], to_delete["name"]))

    if not pods_to_delete:
        log("No duplicate pods with short uptime found to delete")
    else:
        log(f"Found {len(pods_to_delete)} pods to delete:")
        for ns, pod in pods_to_delete:
            log(f"  - {ns}: {pod}")
        
        for ns, pod in pods_to_delete:
            try:
                v1.delete_namespaced_pod(name=pod, namespace=ns, grace_period_seconds=0)
                log(f"Successfully deleted pod {pod} in namespace {ns}")
            except Exception as e:
                log(f"Failed to delete pod {pod} in namespace {ns}: {e}", "error")

# --- Main execution ---
def main():
    """Main function with improved error handling"""
    if len(sys.argv) != 2 or sys.argv[1] not in ["start", "stop"]:
        print("Usage: python control_and_cleanup.py [start|stop]")
        print("Environment variables:")
        print("  PARTIAL_SERVER_NAME: Server name pattern (default: node2)")
        print("  CLOUD_NAME: OpenStack cloud name (default: otc)")
        print("  NAMESPACES: Comma-separated Kubernetes namespaces")
        sys.exit(1)

    action = sys.argv[1]
    log(f"Script called with action: {action}")

    try:
        if action == "start":
            start_server()
        elif action == "stop":
            stop_server()
        log(f"Script completed successfully for action: {action}")
    except Exception as e:
        log(f"Script failed for action {action}: {e}", "error")
        sys.exit(1)

if __name__ == "__main__":
    main()
