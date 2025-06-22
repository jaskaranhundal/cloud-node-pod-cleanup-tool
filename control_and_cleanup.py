"""
OpenStack Server Control & Kubernetes Pod Cleanup Tool

This script provides automated management of OpenStack servers and Kubernetes pods.
It can start/stop OpenStack servers and clean up duplicate pods that may accumulate
during node transitions or server restarts.

Key Features:
- Start/stop OpenStack servers by partial name matching
- Wait for servers to reach desired status (ACTIVE/SHUTOFF)
- Clean up duplicate Kubernetes pods across namespaces
- Comprehensive error handling and retry logic
- Environment variable configuration
- Detailed logging for audit and debugging

Author: Cloud Infrastructure Team
Version: 2.0 (Improved Error Handling)
"""

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

# =============================================================================
# CONFIGURATION SECTION
# =============================================================================

# Timezone configuration for Berlin (adjust as needed for your location)
BERLIN_TZ = pytz.timezone("Europe/Berlin")

# Log file paths for different types of operations
LOG_FILE = "server_control.log"        # Main server operations log
KUBE_LOG_FILE = "k8s_cleanup.log"      # Kubernetes cleanup operations log

# Environment variable configuration with sensible defaults
# These can be overridden by setting environment variables
PARTIAL_SERVER_NAME = os.getenv("PARTIAL_SERVER_NAME", "node2")  # Server name pattern to match
CLOUD_NAME = os.getenv("CLOUD_NAME", "otc")                      # OpenStack cloud name from clouds.yml
NAMESPACES = os.getenv("NAMESPACES", "lindera-production,lindera-testing,lindera-development").split(",")

# Retry configuration for handling transient failures
MAX_RETRIES = 3      # Maximum number of retry attempts
RETRY_DELAY = 5      # Base delay between retries (will be exponential)

# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging():
    """
    Configure unified logging system for the application.
    
    Sets up logging to both file and console with structured format.
    This ensures all operations are logged for audit and debugging purposes.
    
    Returns:
        logging.Logger: Configured logger instance
    """
    # Configure root logger with structured format
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),      # Log to file
            logging.StreamHandler(sys.stdout)   # Log to console
        ]
    )
    return logging.getLogger(__name__)

# Initialize the logger
logger = setup_logging()

def log(msg, level="info"):
    """
    Unified logging function that supports multiple log levels.
    
    This function provides a consistent way to log messages across the application
    with different severity levels (info, warning, error, debug).
    
    Args:
        msg (str): Message to log
        level (str): Log level - "info", "warning", "error", or "debug"
    """
    if level == "info":
        logger.info(msg)
    elif level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "debug":
        logger.debug(msg)

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def retry_operation(operation, max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    """
    Retry an operation with exponential backoff for handling transient failures.
    
    This function implements a retry mechanism that can help handle temporary
    network issues, API timeouts, or other transient failures.
    
    Args:
        operation (callable): Function to retry
        max_retries (int): Maximum number of retry attempts
        delay (int): Base delay between retries (will be exponential)
    
    Returns:
        Any: Result of the operation if successful
        
    Raises:
        Exception: Last exception if all retries fail
    """
    for attempt in range(max_retries):
        try:
            return operation()
        except Exception as e:
            if attempt == max_retries - 1:
                raise e  # Re-raise the last exception if all retries fail
            log(f"Operation failed (attempt {attempt + 1}/{max_retries}): {e}", "warning")
            time.sleep(delay * (2 ** attempt))  # Exponential backoff
    return None

# =============================================================================
# OPENSTACK FUNCTIONS
# =============================================================================

def connect():
    """
    Connect to OpenStack cloud with connection validation.
    
    Establishes a connection to the specified OpenStack cloud and validates
    the connection by attempting to list servers. This ensures the connection
    is working before proceeding with operations.
    
    Returns:
        openstack.connection.Connection: OpenStack connection object
        
    Raises:
        Exception: If connection fails or validation fails
    """
    try:
        # Connect to the specified OpenStack cloud
        conn = openstack.connect(cloud=CLOUD_NAME)
        
        # Test connection by listing servers (limit to 1 for efficiency)
        list(conn.compute.servers(limit=1))
        
        log(f"Successfully connected to OpenStack cloud: {CLOUD_NAME}")
        return conn
    except Exception as e:
        log(f"Failed to connect to OpenStack cloud '{CLOUD_NAME}': {e}", "error")
        raise

def find_servers(conn, partial_name):
    """
    Find OpenStack servers by partial name matching.
    
    Searches through all servers in the OpenStack cloud and returns those
    whose names contain the specified partial name. This allows for flexible
    server identification.
    
    Args:
        conn: OpenStack connection object
        partial_name (str): Partial name to match in server names
    
    Returns:
        list: List of matching server objects
        
    Raises:
        Exception: If server listing fails
    """
    try:
        servers_found = []
        # Iterate through all servers in the cloud
        for server in conn.compute.servers():
            if partial_name in server.name:
                servers_found.append(server)
        
        log(f"Found {len(servers_found)} servers matching '{partial_name}'")
        return servers_found
    except Exception as e:
        log(f"Failed to find servers with name containing '{partial_name}': {e}", "error")
        raise

def wait_for_server_status(conn, server_id, desired_status, timeout=300, poll_interval=10):
    """
    Wait for a server to reach the desired status with timeout.
    
    Polls the server status at regular intervals until it reaches the desired
    status or the timeout is reached. This is useful for ensuring servers
    are fully started or stopped before proceeding.
    
    Args:
        conn: OpenStack connection object
        server_id (str): ID of the server to monitor
        desired_status (str): Target status to wait for (e.g., "ACTIVE", "SHUTOFF")
        timeout (int): Maximum time to wait in seconds (default: 300)
        poll_interval (int): Time between status checks in seconds (default: 10)
    
    Returns:
        bool: True if server reached desired status, False if timeout
    """
    waited = 0
    while waited < timeout:
        try:
            # Get current server status
            server = conn.compute.get_server(server_id)
            if server.status.lower() == desired_status.lower():
                log(f"Server {server_id} reached status: {desired_status}")
                return True
            
            # Wait before next check
            time.sleep(poll_interval)
            waited += poll_interval
        except Exception as e:
            log(f"Error checking server status: {e}", "warning")
            time.sleep(poll_interval)
            waited += poll_interval
    
    log(f"Timeout waiting for server {server_id} to reach status {desired_status}", "error")
    return False

def stop_server():
    """
    Stop OpenStack servers with comprehensive error handling.
    
    Finds servers matching the configured name pattern and stops them.
    Includes proper error handling for each server operation and logs
    all actions for audit purposes.
    
    Raises:
        Exception: If the overall stop operation fails
    """
    try:
        # Connect to OpenStack and find matching servers
        conn = connect()
        servers = find_servers(conn, PARTIAL_SERVER_NAME)
        
        if not servers:
            log(f"No servers found with name containing '{PARTIAL_SERVER_NAME}'")
            return
        
        # Process each server individually
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
    """
    Extract the fixed IP address from a server's network configuration.
    
    OpenStack servers can have multiple network interfaces and IP addresses.
    This function looks for the "fixed" (private/internal) IP address
    which is typically used for internal cluster communication.
    
    Args:
        conn: OpenStack connection object
        server: Server object with network information
    
    Returns:
        str: Fixed IP address if found, None otherwise
    """
    try:
        addresses = server.addresses
        for network in addresses.values():
            for addr_info in network:
                # Look for fixed IP (usually private/internal IP)
                if addr_info.get('OS-EXT-IPS:type') == 'fixed':
                    return addr_info['addr']
        
        log(f"Could not find fixed IP for server {server.name}", "warning")
        return None
    except Exception as e:
        log(f"Error getting IP for server {server.name}: {e}", "error")
        return None

# =============================================================================
# KUBERNETES FUNCTIONS
# =============================================================================

def setup_kubernetes_client():
    """
    Setup Kubernetes client with proper configuration handling.
    
    Attempts to load Kubernetes configuration in the following order:
    1. In-cluster configuration (if running inside a Kubernetes pod)
    2. Kubeconfig file (if running outside the cluster)
    
    This ensures the script works both inside and outside Kubernetes clusters.
    
    Returns:
        client.CoreV1Api: Configured Kubernetes API client
        
    Raises:
        Exception: If Kubernetes configuration cannot be loaded
    """
    try:
        try:
            # First try in-cluster configuration (for running inside K8s)
            config.load_incluster_config()
            log("Using in-cluster Kubernetes configuration")
        except ConfigException:
            # Fall back to kubeconfig file (for running outside K8s)
            config.load_kube_config()
            log("Using kubeconfig file")
        
        return client.CoreV1Api()
    except Exception as e:
        log(f"Failed to load Kubernetes config: {e}", "error")
        raise

def is_node_ready(node_name):
    """
    Check if a Kubernetes node is in Ready state.
    
    Examines the node's conditions to determine if it's ready to accept pods.
    This is important for ensuring the node is fully operational before
    performing pod cleanup operations.
    
    Args:
        node_name (str): Name of the node to check
    
    Returns:
        bool: True if node is ready, False otherwise
    """
    try:
        v1 = client.CoreV1Api()
        node = v1.read_node(name=node_name)
        
        # Check node conditions for Ready status
        for condition in node.status.conditions:
            if condition.type == "Ready":
                return condition.status == "True"
        return False
    except Exception as e:
        log(f"Failed to get node status for {node_name}: {e}", "error")
        return False

def wait_for_node_ready(node_name, timeout=600, poll_interval=10):
    """
    Wait for a Kubernetes node to become ready with timeout.
    
    Polls the node status until it becomes ready or timeout is reached.
    This is useful after starting OpenStack servers to ensure the
    corresponding Kubernetes nodes are fully operational.
    
    Args:
        node_name (str): Name of the node to monitor
        timeout (int): Maximum time to wait in seconds (default: 600)
        poll_interval (int): Time between status checks in seconds (default: 10)
    
    Returns:
        bool: True if node became ready, False if timeout
    """
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
    """
    Start OpenStack servers with comprehensive orchestration.
    
    This is the main function for starting servers. It:
    1. Finds and starts matching servers
    2. Waits for servers to become active
    3. Maps server IPs to Kubernetes nodes
    4. Waits for nodes to become ready
    5. Performs pod cleanup
    
    Includes comprehensive error handling and graceful degradation.
    
    Raises:
        Exception: If the overall start operation fails
    """
    try:
        # Connect to OpenStack and find matching servers
        conn = connect()
        servers = find_servers(conn, PARTIAL_SERVER_NAME)
        
        if not servers:
            log(f"No servers found with name containing '{PARTIAL_SERVER_NAME}'")
            return

        # Setup Kubernetes client (with graceful degradation)
        try:
            v1 = setup_kubernetes_client()
        except Exception as e:
            log(f"Kubernetes setup failed, proceeding without pod cleanup: {e}", "warning")
            v1 = None

        # Process each server
        for server in servers:
            try:
                if server.status.lower() == "shutoff":
                    log(f"Starting server: {server.name} ...")
                    conn.compute.start_server(server.id)
                    
                    # Wait for server to become active
                    if wait_for_server_status(conn, server.id, "active"):
                        log(f"Server {server.name} is active")
                        
                        # Perform Kubernetes operations if available
                        if v1:
                            server_ip = get_server_ip(conn, server)
                            if server_ip:
                                # Find corresponding Kubernetes node
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
    """
    Find Kubernetes node name by matching IP address.
    
    Maps OpenStack server IP addresses to Kubernetes node names.
    This is necessary because OpenStack servers and Kubernetes nodes
    may have different naming conventions.
    
    Args:
        v1: Kubernetes API client
        server_ip (str): IP address to search for
    
    Returns:
        str: Node name if found, None otherwise
    """
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

# =============================================================================
# KUBERNETES CLEANUP FUNCTIONS
# =============================================================================

def get_base_name(pod_name):
    """
    Extract the base name from a pod name by removing random suffixes.
    
    Kubernetes pod names often include random suffixes for uniqueness.
    This function extracts the base name by removing parts that look
    like random strings (typically 4+ characters of alphanumeric).
    
    Example: "myapp-deployment-abc123" -> "myapp-deployment"
    
    Args:
        pod_name (str): Full pod name
    
    Returns:
        str: Base name without random suffixes
    """
    parts = pod_name.split('-')
    base = parts[0]
    for i in range(1, len(parts)):
        # Stop when we hit a part that looks like a random string
        if len(parts[i]) < 4 or not re.match(r'^[a-z0-9]+$', parts[i]):
            base += '-' + parts[i]
        else:
            break
    return base

def cleanup_duplicate_pods():
    """
    Clean up duplicate Kubernetes pods across specified namespaces.
    
    This function identifies and removes duplicate pods that may have been
    created during node transitions or server restarts. It:
    1. Groups pods by base name and namespace
    2. Identifies duplicates across different nodes
    3. Removes the youngest duplicate pod
    
    This helps maintain a clean cluster state and prevents resource waste.
    """
    log("Starting Kubernetes pod cleanup...")
    
    # Setup Kubernetes client
    try:
        v1 = setup_kubernetes_client()
    except Exception as e:
        log(f"Failed to setup Kubernetes client for cleanup: {e}", "error")
        return

    # Dictionary to group pods by base name and namespace
    pod_groups = {}

    # Process each namespace
    for ns in NAMESPACES:
        try:
            pods = v1.list_namespaced_pod(namespace=ns).items
            log(f"Found {len(pods)} pods in namespace {ns}")
        except Exception as e:
            log(f"Failed to list pods in namespace {ns}: {e}", "error")
            continue

        # Process each pod in the namespace
        for pod in pods:
            # Skip pods without start time or host IP
            if not pod.status.start_time or not pod.status.host_ip:
                continue

            # Extract pod information
            name = pod.metadata.name
            age_seconds = (datetime.now(pytz.UTC) - pod.status.start_time).total_seconds()
            node_ip = pod.status.host_ip
            base_name = get_base_name(name)
            key = f"{ns}:{base_name}"
            
            # Group pods by base name and namespace
            pod_groups.setdefault(key, []).append({
                "namespace": ns,
                "name": name,
                "node_ip": node_ip,
                "age": age_seconds
            })

    # Identify pods to delete
    pods_to_delete = []
    for group, pod_list in pod_groups.items():
        # Get unique node IPs for this group
        node_ips = set(p["node_ip"] for p in pod_list)
        
        # If we have multiple pods and they're on 1-2 nodes, consider them duplicates
        if len(pod_list) > 1 and len(node_ips) <= 2:
            # Sort by age and delete the youngest (oldest by age_seconds)
            pod_list_sorted = sorted(pod_list, key=lambda x: x["age"])
            to_delete = pod_list_sorted[0]  # Youngest pod
            pods_to_delete.append((to_delete["namespace"], to_delete["name"]))

    # Perform deletion
    if not pods_to_delete:
        log("No duplicate pods with short uptime found to delete")
    else:
        log(f"Found {len(pods_to_delete)} pods to delete:")
        for ns, pod in pods_to_delete:
            log(f"  - {ns}: {pod}")
        
        # Delete each identified pod
        for ns, pod in pods_to_delete:
            try:
                v1.delete_namespaced_pod(name=pod, namespace=ns, grace_period_seconds=0)
                log(f"Successfully deleted pod {pod} in namespace {ns}")
            except Exception as e:
                log(f"Failed to delete pod {pod} in namespace {ns}: {e}", "error")

# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """
    Main function that handles command-line arguments and orchestrates operations.
    
    This is the entry point of the script. It:
    1. Validates command-line arguments
    2. Displays usage information if needed
    3. Executes the appropriate operation (start/stop)
    4. Handles errors and provides meaningful feedback
    
    Command-line usage:
        python control_and_cleanup.py start  # Start servers and cleanup pods
        python control_and_cleanup.py stop   # Stop servers
    """
    # Validate command-line arguments
    if len(sys.argv) != 2 or sys.argv[1] not in ["start", "stop"]:
        print("Usage: python control_and_cleanup.py [start|stop]")
        print("Environment variables:")
        print("  PARTIAL_SERVER_NAME: Server name pattern (default: node2)")
        print("  CLOUD_NAME: OpenStack cloud name (default: otc)")
        print("  NAMESPACES: Comma-separated Kubernetes namespaces")
        sys.exit(1)

    action = sys.argv[1]
    log(f"Script called with action: {action}")

    # Execute the requested action with error handling
    try:
        if action == "start":
            start_server()
        elif action == "stop":
            stop_server()
        log(f"Script completed successfully for action: {action}")
    except Exception as e:
        log(f"Script failed for action {action}: {e}", "error")
        sys.exit(1)

# Entry point - only run if script is executed directly
if __name__ == "__main__":
    main()
