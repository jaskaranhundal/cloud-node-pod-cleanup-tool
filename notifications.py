#!/usr/bin/env python3
"""
MS Teams Notifications Module for OTC Server Management
"""
import os
import requests
from datetime import datetime
import pytz
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class TeamsNotifier:
    def __init__(self):
        self.webhook_url = os.getenv('TEAMS_WEBHOOK_URL')
        self.enabled = os.getenv('TEAMS_ENABLED', 'true').lower() == 'true'
        self.channel_name = os.getenv('TEAMS_CHANNEL_NAME', 'otc-server-alerts')
        self.environment = os.getenv('ENVIRONMENT', 'production')

        # Notification flags
        self.notify_success = os.getenv('NOTIFY_ON_SUCCESS', 'true').lower() == 'true'
        self.notify_error = os.getenv('NOTIFY_ON_ERROR', 'true').lower() == 'true'
        self.notify_warning = os.getenv('NOTIFY_ON_WARNING', 'true').lower() == 'true'

    def _get_timestamp(self):
        """Get current timestamp in Berlin timezone"""
        berlin_tz = pytz.timezone("Europe/Berlin")
        return datetime.now(berlin_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    def _get_color(self, status):
        """Get color for message status"""
        colors = {
            'success': '00FF00',  # Green
            'error': 'FF0000',    # Red
            'warning': 'FFA500',  # Orange
            'info': '0078D4'      # Blue
        }
        return colors.get(status.lower(), '0078D4')

    def _create_message(self, title, message, status='info', details=None, action=None):
        """Create Teams message payload"""
        timestamp = self._get_timestamp()
        color = self._get_color(status)

        # Build facts array
        facts = [
            {"name": "Environment", "value": self.environment},
            {"name": "Timestamp", "value": timestamp},
            {"name": "Status", "value": status.upper()}
        ]

        if action:
            facts.append({"name": "Action", "value": action})

        if details:
            for key, value in details.items():
                facts.append({"name": key, "value": str(value)})

        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": f"OTC Server Alert: {title}",
            "themeColor": color,
            "sections": [{
                "activityTitle": f"🔧 OTC Server Management - {title}",
                "activitySubtitle": f"Environment: {self.environment}",
                "text": message,
                "facts": facts
            }]
        }

        return payload

    def _send_notification(self, payload):
        """Send notification to Teams"""
        if not self.enabled:
            print("Teams notifications disabled")
            return False

        if not self.webhook_url or 'YOUR_WEBHOOK_URL' in self.webhook_url:
            print("Teams webhook URL not configured")
            return False

        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            if response.status_code == 200:
                print("✓ Teams notification sent successfully")
                return True
            else:
                print(f"✗ Teams notification failed: {response.status_code}")
                return False

        except Exception as e:
            print(f"✗ Teams notification error: {e}")
            return False

    def send_success(self, title, message, action=None, details=None):
        """Send success notification"""
        if not self.notify_success:
            return

        payload = self._create_message(title, message, 'success', details, action)
        self._send_notification(payload)

    def send_error(self, title, message, action=None, details=None, error_code=None):
        """Send error notification"""
        if not self.notify_error:
            return

        if error_code:
            title = f"{title} (Error {error_code})"

        payload = self._create_message(title, message, 'error', details, action)
        self._send_notification(payload)

    def send_warning(self, title, message, action=None, details=None):
        """Send warning notification"""
        if not self.notify_warning:
            return

        payload = self._create_message(title, message, 'warning', details, action)
        self._send_notification(payload)

    def send_auth_failure(self, error_message, action=None):
        """Send 401 authentication failure alert"""
        title = "Authentication Failure (401)"
        message = f"❌ OpenStack authentication failed: {error_message}"
        details = {
            "Error Type": "Authentication Failure",
            "HTTP Status": "401 Unauthorized",
            "Cloud": os.getenv('CLOUD_NAME', 'otc')
        }
        self.send_error(title, message, action, details, 401)

    def send_server_status(self, server_name, server_id, old_status, new_status, action):
        """Send server status change notification"""
        title = f"Server {action.title()} Complete"
        message = f"✅ Server '{server_name}' status changed from {old_status} → {new_status}"
        details = {
            "Server Name": server_name,
            "Server ID": server_id,
            "Previous Status": old_status,
            "Current Status": new_status
        }
        self.send_success(title, message, action, details)

    def send_timeout_alert(self, operation, timeout_seconds, action=None):
        """Send timeout alert"""
        title = "Operation Timeout"
        message = f"⏰ {operation} timed out after {timeout_seconds} seconds"
        details = {
            "Operation": operation,
            "Timeout": f"{timeout_seconds}s"
        }
        self.send_warning(title, message, action, details)

    def send_script_start(self, action, servers_found=None):
        """Send script execution start notification"""
        title = "Script Execution Started"
        message = f"🚀 OTC server management script started with action: {action}"
        details = {"Servers Found": servers_found} if servers_found else None
        self.send_success(title, message, action, details)

    def send_script_complete(self, action, success_count=0, error_count=0):
        """Send script execution complete notification"""
        if error_count > 0:
            title = "Script Completed with Errors"
            message = f"⚠️ Script finished with {success_count} successes and {error_count} errors"
            self.send_warning(title, message, action, {
                "Successful Operations": success_count,
                "Failed Operations": error_count
            })
        else:
            title = "Script Completed Successfully"
            message = f"✅ All operations completed successfully ({success_count} total)"
            self.send_success(title, message, action, {
                "Total Operations": success_count
            })

# Global notifier instance
notifier = TeamsNotifier()

# Convenience functions
def notify_success(title, message, action=None, details=None):
    notifier.send_success(title, message, action, details)

def notify_error(title, message, action=None, details=None, error_code=None):
    notifier.send_error(title, message, action, details, error_code)

def notify_warning(title, message, action=None, details=None):
    notifier.send_warning(title, message, action, details)

def notify_auth_failure(error_message, action=None):
    notifier.send_auth_failure(error_message, action)

def notify_script_start(action, servers_found=None):
    notifier.send_script_start(action, servers_found)

def notify_script_complete(action, success_count=0, error_count=0):
    notifier.send_script_complete(action, success_count, error_count)

# Test function
if __name__ == "__main__":
    print("Testing Teams notifications...")
    notifier.send_success(
        "Test Notification",
        "This is a test message from the OTC server management system",
        "test",
        {"Test Parameter": "Test Value"}
    )