# 🖥️ OpenStack Server Control & Kubernetes Pod Cleanup

![Python](https://img.shields.io/badge/python-3.6%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-active-brightgreen)

This script allows controlled **start/stop of OpenStack servers** and performs **Kubernetes pod cleanup** to ensure only valid instances are retained after node transitions.

---

## 📌 Features

- ✅ Start or stop OpenStack servers (by partial name match, e.g., `node2`)
- ✅ Wait for server to reach desired status (`ACTIVE`/`SHUTOFF`)
- ✅ Clean up short-lived duplicate pods in specified Kubernetes namespaces
- ✅ Comprehensive error handling and retry logic
- ✅ Environment variable configuration
- ✅ Unified logging with multiple levels (info, warning, error)
- ✅ Connection validation before operations
- ✅ Logs all actions for audit and debugging
- ✅ Cron-friendly CLI interface

---

## ⚙️ Setup

### 🔧 Prerequisites

Ensure the following are installed:

- Python 3.6+
- `openstacksdk`, `kubernetes`, `pytz`
- Access to:
  - An OpenStack environment via `clouds.yaml`
  - A Kubernetes cluster (in-cluster or via kubeconfig)

Install dependencies:

```bash
pip install -r requirements.txt
```

## 📝 Configuration

### Environment Variables

The script now uses environment variables for configuration with sensible defaults:

```bash
# Server name pattern to match (default: node2)
export PARTIAL_SERVER_NAME="node2"

# OpenStack cloud name (default: otc)
export CLOUD_NAME="otc"

# Comma-separated Kubernetes namespaces (default: lindera-production,lindera-testing,lindera-development)
export NAMESPACES="lindera-production,lindera-testing,lindera-development"
```

### OpenStack Configuration

Edit `clouds.yml` with your OpenStack credentials:
```yaml
clouds:
  otc:
    profile: otc
    auth:
      auth_url: 'https://iam.eu-de.otc.t-systems.com/v3'
      username: '<<USER_NAME>>'
      password: '<<PASSWORD>>'
      project_id: '<<eu-de_project>>'
      user_domain_name: '<<123456_DOMAIN_ID>>'
    interface: 'public'
    identity_api_version: 3
    region_name: eu-de
```

---
## 🚀 Usage

### Basic Commands

- Start Server + Clean Up Pods
```bash
python control_and_cleanup.py start
```

- Stop Server
```bash
python control_and_cleanup.py stop
```

- Help
```bash
python control_and_cleanup.py 
# Output: Usage information with environment variable details
```

### With Custom Configuration

```bash
# Use custom server name pattern
PARTIAL_SERVER_NAME="worker" python control_and_cleanup.py start

# Use different cloud and namespaces
CLOUD_NAME="mycloud" NAMESPACES="prod,staging" python control_and_cleanup.py start
```

---
## 🧼 Kubernetes Cleanup Logic

- Identifies pods with the same "base name"
- Checks pods on duplicate nodes or across node transitions
- Deletes the youngest duplicate pod (based on age)
- Improved error handling for namespace access issues
- Graceful degradation when Kubernetes is unavailable

---
## 📝 Crontab Example

To automate server control (Berlin timezone example):

```bash
# Start server Mon–Fri at 07:00
0 7 * * 1-5 /usr/bin/python3 /path/to/control_and_cleanup.py start

# Stop server Mon–Fri at 19:00
0 19 * * 1-5 /usr/bin/python3 /path/to/control_and_cleanup.py stop
```

### Docker Container Usage

```bash
# Build the container
docker build -t cloud-node-cleanup .

# Run with environment variables
docker run -e PARTIAL_SERVER_NAME="node2" \
           -e CLOUD_NAME="otc" \
           -e NAMESPACES="prod,staging" \
           -v /path/to/clouds.yml:/app/clouds.yml \
           cloud-node-cleanup
```

---
## 🔧 Error Handling & Reliability

### Retry Logic
- Automatic retry with exponential backoff for transient failures
- Configurable retry attempts and delays
- Graceful handling of network timeouts

### Connection Validation
- Validates OpenStack connection before operations
- Tests Kubernetes cluster accessibility
- Continues operation even if Kubernetes is unavailable

### Comprehensive Logging
- Unified logging system with multiple levels
- Both file and console output
- Structured log format with timestamps

---
## 📂 Project Structure

```bash
.
├── control_and_cleanup.py     # Main script (improved error handling)
├── README.md                  # This file
├── requirements.txt           # Dependencies (cleaned up)
├── clouds.yml                 # OpenStack configuration
├── crontab.txt               # Automation schedule
├── Dockerfile                # Container configuration
├── server_control.log        # Server operations log (runtime)
└── k8s_cleanup.log           # Kubernetes cleanup log (runtime)
```

---
## 🐛 Troubleshooting

### Common Issues

1. **OpenStack Connection Failed**
   - Verify `clouds.yml` configuration
   - Check network connectivity
   - Ensure credentials are correct

2. **Kubernetes Access Denied**
   - Verify kubeconfig or in-cluster configuration
   - Check namespace permissions
   - Script will continue without pod cleanup

3. **Server Not Found**
   - Verify `PARTIAL_SERVER_NAME` environment variable
   - Check server naming pattern
   - Ensure OpenStack project access

### Log Analysis

Check log files for detailed error information:
```bash
tail -f server_control.log
tail -f k8s_cleanup.log
```

---
