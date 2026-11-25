#!/usr/bin/env python3
"""
Sonar - A WiFi Keepalive Daemon in Python

This program pings a target (e.g., the router) at regular intervals to detect a
WiFi outage. If an outage is detected, it attempts to restore the WiFi
connection by using various methods, such as wpa_cli reassociation, restarting
the dhcpcd service, or restarting the NetworkManager service.

Configuration:
  The program loads optional parameters from a configuration file (default:
  sonar.conf) in INI format. For example:

    [sonar]
    enable: true
    debug_log: false
    persistent_log: false
    target: auto
    count: 3
    interval: 60
    restart_threshold: 10
    log_path: /home/pi/printer_data/logs/sonar.log
    status_file: /home/pi/printer_data/logs/sonar_estado.json

  If "auto" is set for the target, the default gateway (router IP) is determined
  automatically.

Note: If persistent_log is enabled, logs will be written to /var/log/sonar.log.
log_path (optional) also writes the log to that file (rotated at 1 MB, 2 copies),
for example inside printer_data/logs so Mainsail shows it. status_file (optional)
keeps a small JSON with the count and time of WiFi losses and reconnections; it is
only written at start and on each loss/reconnection.
"""

import subprocess
import time
import configparser
import os
import re
import sys
import shutil
import logging
import logging.handlers
import json


class SonarDaemon:
    """Sonar - A WiFi Keepalive Daemon"""

    def __init__(self, config_path=None):
        self.config = {}
        # Set up logging
        self.logger = logging.getLogger("sonar")
        self.logger.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] %(message)s',
                                      datefmt='%m/%d/%Y %H:%M:%S')

        # Clear existing handlers
        self.logger.handlers = []

        # Set up console logging
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)

        # Load configuration
        self.load_config(config_path)

        # Set up persistant logging if enabled
        if self.config['persistent_log']:
            log_file = "/var/log/sonar.log"
            try:
                formatter = logging.Formatter('[%(asctime)s] %(message)s',
                                              datefmt='%m/%d/%Y %H:%M:%S')
                fh = logging.FileHandler(log_file)
                fh.setFormatter(formatter)
                self.logger.addHandler(fh)
            except PermissionError:
                self.logger.warning(f"No permission to write to {log_file}. "
                                    f"Using stdout instead.")

        # Optional extra log file (e.g. printer_data/logs, visible from Mainsail)
        if self.config['log_path']:
            try:
                fh = logging.handlers.RotatingFileHandler(
                    self.config['log_path'], maxBytes=1_000_000, backupCount=2, encoding='utf-8')
                fh.setFormatter(formatter)
                self.logger.addHandler(fh)
                self._give_to_folder_owner(self.config['log_path'])
            except OSError as e:
                self.logger.warning(f"Cannot write log to {self.config['log_path']}: {e}")

        self.status = self.load_status()

        self.logger.info("Starting Sonar – WiFi Keepalive Daemon.")

        self.logger.info(f"Configuration loaded:")
        self.logger.info(f"  enable: {self.config['enable']}")
        self.logger.info(f"  debug_log: {self.config['debug_log']}")
        self.logger.info(f"  persistent_log: {self.config['persistent_log']}")
        self.logger.info(f"  target: {self.config['target']}")
        self.logger.info(f"  count: {self.config['count']}")
        self.logger.info(f"  interval: {self.config['interval']}")
        self.logger.info(f"  restart_threshold: {self.config['restart_threshold']}")
        self.logger.info(f"  log_path: {self.config['log_path'] or '-'}")
        self.logger.info(f"  status_file: {self.config['status_file'] or '-'}")

        # Set debug level if needed
        if self.config['debug_log']:
            self.logger.setLevel(logging.DEBUG)
            self.logger.debug("Debug logging enabled.")

    def load_config(self, config_path=None):
        cp = configparser.ConfigParser(inline_comment_prefixes='#')

        if config_path and os.path.exists(config_path):
            try :
                cp.read(config_path)
            except Exception as e:
                self.logger.warning(f"Error reading configuration file: {e}")
        else:
            self.logger.warning("No configuration file found. Using default values.")

        cp['DEFAULT'] = {
            'enable': 'false',
            'debug_log': 'false',
            'persistent_log': 'false',
            'target': 'auto',
            'count': '3',
            'interval': '60',
            'restart_threshold': '10',
            'log_path': '',
            'status_file': ''
        }

        if not cp.has_section('sonar'):
            cp.add_section('sonar')

        self.config = {
            'enable': cp.getboolean('sonar', 'enable'),
            'debug_log': cp.getboolean('sonar', 'debug_log'),
            'persistent_log': cp.getboolean('sonar', 'persistent_log'),
            'target': cp.get('sonar', 'target'),
            'count': cp.getint('sonar', 'count'),
            'interval': cp.getint('sonar', 'interval'),
            'restart_threshold': cp.getint('sonar', 'restart_threshold'),
            'log_path': cp.get('sonar', 'log_path').strip(),
            'status_file': cp.get('sonar', 'status_file').strip()
        }

    # --- Status file: count and time of WiFi losses (optional, status_file) ---
    @staticmethod
    def _give_to_folder_owner(path):
        # The service runs as root; leave the file owned like its folder so the
        # user (and Moonraker) can rotate or delete it.
        try:
            st = os.stat(os.path.dirname(os.path.abspath(path)))
            os.chown(path, st.st_uid, st.st_gid)
        except (OSError, AttributeError):  # AttributeError: no chown on this OS
            pass

    def load_status(self):
        status = {'started': None, 'losses': 0, 'last_loss': None,
                  'last_reconnect': None, 'events': []}
        path = self.config['status_file']
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    status.update(json.load(f))
            except (OSError, ValueError) as e:
                self.logger.warning(f"Cannot read status file {path}: {e}")
        return status

    def save_status(self):
        path = self.config['status_file']
        if not path:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.status, f, indent=1)
            os.replace(tmp, path)
            self._give_to_folder_owner(path)
        except OSError as e:
            self.logger.warning(f"Cannot write status file {path}: {e}")

    def record_event(self, **event):
        self.status['events'] = (self.status.get('events') or [])[-19:] + [event]
        self.save_status()

    def _is_service_active(self, service_name):
        try:
            result = subprocess.run(["systemctl", "is-active", service_name],
                                    capture_output=True, text=True)
            return result.stdout.strip() == "active"
        except Exception:
            return False

    def get_default_gateway(self):
        # Regex pattern to extract gateway, device name, source IP, and metric
        pattern = r'default via (\S+)\s+dev (\S+)(?:.*?\bsrc (\S+))?(?:.*?\bmetric (\d+))?'

        try:
            route_output = subprocess.run(["ip", "route", "show", "default"],
                                          capture_output=True, text=True)

            if route_output.returncode != 0:
                self.logger.warning(f"Error retrieving default route: {route_output.stderr}")
                return None

            if route_output.stdout == "":
                self.logger.warning("No default route found.")
                return None

            matches = re.findall(pattern, route_output.stdout)

            if not matches:
                self.logger.warning("No matching routes found.")
                return None

            # Sort matches by metric (ascending; missing metric -> 0)
            matches.sort(key=lambda x: int(x[3]) if x[3] else 0)

            return {
                'gateway': matches[0][0],  # Gateway IP
                'interface': matches[0][1],  # Device name (e.g., wlan0)
                'src': matches[0][2] or None,  # Source IP (absent on static routes)
                'metric': int(matches[0][3]) if matches[0][3] else 0  # Metric (may be absent)
            }
        except Exception as e:
            self.logger.error(f"Error retrieving default gateway: {e}")
            return None

    def restart_wifi(self, interface="wlan0"):
        exists_wpa_cli = shutil.which("wpa_cli")
        is_dhcpcd_active = self._is_service_active("dhcpcd")
        is_network_manager_active = self._is_service_active("NetworkManager")

        self.logger.info("Attempting to restart WiFi connection...")
        if exists_wpa_cli and is_dhcpcd_active:
            try:
                subprocess.run(["wpa_cli", "-i", interface, "reassociate"],
                               check=True)
                self.logger.info("WiFi reconnected using wpa_cli reassociate.")
                subprocess.run(["systemctl", "restart", "dhcpcd.service"],
                               check=True)
                self.logger.info("dhcpcd service restarted.")
            except subprocess.CalledProcessError:
                self.logger.warning("wpa_cli reassociate failed or failed to"
                                    " restart dhcpcd.")
        elif is_network_manager_active:
            try:
                subprocess.run(["systemctl", "restart",
                                "NetworkManager.service"], check=True)
                self.logger.info("NetworkManager service restarted.")
            except subprocess.CalledProcessError:
                self.logger.warning("Restarting NetworkManager failed.")
        else:
            self.logger.error("No active service found to restart WiFi"
                              " connection.")

    def ping_target(self, target, count):
        try:
            result = subprocess.run(["ping", "-c", str(count), target],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                return False

            if self.config['debug_log']:
                lines = result.stdout.splitlines()
                summary = lines[-1]
                self.logger.debug(f"Ping to {target} successful: {summary}")

            return True
        except Exception as e:
            self.logger.error(f"Error executing ping: {e}")
            return False

    def run(self):
        if not self.config['enable']:
            self.logger.info("Sonar is disabled in the configuration. Exiting.")
            sys.exit(0)

        self.status['started'] = time.time()
        self.save_status()

        while True:
            gateway = self.get_default_gateway()

            if not gateway:
                self.logger.warning("No default gateway found. Retrying...")
                time.sleep(self.config['interval'])
                continue

            if not gateway['interface'].startswith(('wl', 'wlan', 'wlp')):
                self.logger.debug(f"No WiFi interface active for the default gateway."
                                  f" {gateway['interface']} is not a WiFi interface."
                                  f" Retrying...")
                time.sleep(self.config['interval'])
                continue

            target = self.config['target']
            if target == "auto":
                target = gateway['gateway']

            if not self.ping_target(target, self.config['count']):
                restart_threshold = self.config['restart_threshold']
                self.logger.info(f"Connection lost – {target} is unreachable!")
                lost_at = time.time()
                self.status['losses'] = int(self.status.get('losses') or 0) + 1
                self.status['last_loss'] = lost_at
                self.save_status()
                self.logger.info(f"Waiting {restart_threshold} seconds before"
                                 f" attempting a restart.")
                time.sleep(restart_threshold)

                retry_count = 0
                used_retries = 0
                # Repeat until a single ping is successful
                while not self.ping_target(target, 1):
                    retry_count += 1
                    used_retries += 1
                    self.restart_wifi(gateway['interface'])
                    self.logger.info("Waiting 10 seconds to re-establish the connection...")
                    time.sleep(10)
                    if retry_count == 3:
                        self.logger.warning(
                            f"Reconnection attempt failed after {retry_count} tries."
                            f" Pausing for {self.config['interval']} seconds.")
                        time.sleep(self.config['interval'])
                        retry_count = 0

                self.logger.info(f"Reconnected after {used_retries} attempts.")
                self.status['last_reconnect'] = time.time()
                self.record_event(lost=lost_at, reconnected=self.status['last_reconnect'],
                                  attempts=used_retries, interface=gateway['interface'],
                                  target=target)

            time.sleep(self.config['interval'])


if __name__ == "__main__":
    try:
        start_arg_config = None
        if len(sys.argv) > 1:
            start_arg_config = sys.argv[1]
        daemon = SonarDaemon(start_arg_config)
        daemon.run()
    except KeyboardInterrupt:
        print("\nSonar daemon interrupted by user. Exiting.")
        sys.exit(0)
