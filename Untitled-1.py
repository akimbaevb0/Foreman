#!/usr/bin/env python3
"""
Industrial Equipment Monitoring Application
Мониторинг ASIC-майнеров, счетчиков Меркурий 236 и частотных преобразователей
"""

import sys
import os
import json
import sqlite3
import asyncio
import struct
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
import logging
import csv

# PyQt6 imports
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QComboBox,
    QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QFrame,
    QSplitter, QTabWidget, QGridLayout, QGroupBox, QMessageBox,
    QFileDialog, QDateEdit, QCheckBox, QScrollArea, QHeaderView,
    QStackedWidget, QListWidget, QListWidgetItem, QFormLayout,
    QDialog, QDialogButtonBox
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QDateTime, QDate,
    QRunnable, QThreadPool, QMutex, QMutexLocker
)
from PyQt6.QtGui import QFont, QPalette, QColor, QIcon, QPixmap

# For async operations
import aiohttp
import serial
import serial.rs485
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# For plotting
import pyqtgraph as pg
pg.setConfigOptions(useOpenGL=True, antialias=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============== Data Models ==============

class AlertLevel(Enum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

@dataclass
class DeviceConfig:
    """Base configuration for all devices"""
    id: str
    name: str
    enabled: bool = True
    poll_interval: int = 30  # seconds
    
@dataclass
class ASICConfig:
    """Configuration for ASIC miner"""
    id: str
    name: str
    ip_address: str
    enabled: bool = True
    poll_interval: int = 30
    port: int = 4028
    temp_warn: float = 80.0
    temp_crit: float = 90.0
    hashrate_warn_drop: float = 15.0  # percent
    hashrate_crit_drop: float = 25.0  # percent

@dataclass
class MercuryConfig:
    """Configuration for Mercury 236 meter"""
    id: str
    name: str
    serial_port: str
    address: int
    enabled: bool = True
    poll_interval: int = 30
    baudrate: int = 9600
    
@dataclass
class VFDConfig:
    """Configuration for Variable Frequency Drive"""
    id: str
    name: str
    serial_port: str
    slave_id: int
    enabled: bool = True
    poll_interval: int = 30
    baudrate: int = 9600
    temp_warn: float = 70.0
    temp_crit: float = 80.0

@dataclass
class Alert:
    """Alert data structure"""
    timestamp: float
    device_id: str
    device_type: str
    level: AlertLevel
    message: str
    acknowledged: bool = False

# ============== Database Manager ==============

class DatabaseManager:
    """SQLite database manager with WAL mode"""
    
    def __init__(self, db_path: str = "monitoring.db"):
        self.db_path = db_path
        self.init_database()
        
    def init_database(self):
        """Initialize database with required tables"""
        with sqlite3.connect(self.db_path) as conn:
            # Enable WAL mode
            conn.execute("PRAGMA journal_mode=WAL")
            
            # Device configurations
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_configs (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    config TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    updated_at REAL DEFAULT (strftime('%s', 'now'))
                )
            """)
            
            # Measurements
            conn.execute("""
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    device_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL
                )
            """)
            
            # Create index separately
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_device_time 
                ON measurements (device_id, timestamp)
            """)
            
            # Alerts
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    device_id TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    acknowledged INTEGER DEFAULT 0
                )
            """)
            
            # Create index separately
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts_time 
                ON alerts (timestamp DESC)
            """)
            
            conn.commit()
    
    def save_config(self, device_type: str, config):
        """Save device configuration"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO device_configs (id, type, config, updated_at)
                VALUES (?, ?, ?, strftime('%s', 'now'))
            """, (config.id, device_type, json.dumps(asdict(config))))
            conn.commit()
    
    def load_configs(self, device_type: str) -> List[Dict]:
        """Load all configurations for device type"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT config FROM device_configs WHERE type = ?",
                (device_type,)
            )
            return [json.loads(row[0]) for row in cursor.fetchall()]
    
    def delete_config(self, device_id: str):
        """Delete device configuration"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM device_configs WHERE id = ?", (device_id,))
            conn.commit()
    
    def save_measurement(self, device_id: str, metric: str, value: float):
        """Save measurement to database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO measurements (timestamp, device_id, metric, value)
                VALUES (strftime('%s', 'now'), ?, ?, ?)
            """, (device_id, metric, value))
            conn.commit()
    
    def get_measurements(self, device_id: str, metric: str, 
                         hours: float = 24) -> List[Tuple[float, float]]:
        """Get measurements for device/metric in last N hours"""
        since = time.time() - (hours * 3600)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT timestamp, value FROM measurements
                WHERE device_id = ? AND metric = ? AND timestamp > ?
                ORDER BY timestamp
            """, (device_id, metric, since))
            return cursor.fetchall()
    
    def save_alert(self, alert: Alert):
        """Save alert to database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO alerts (timestamp, device_id, device_type, level, message, acknowledged)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (alert.timestamp, alert.device_id, alert.device_type,
                  alert.level.value, alert.message, alert.acknowledged))
            conn.commit()
    
    def get_active_alerts(self) -> List[Alert]:
        """Get unacknowledged alerts"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT timestamp, device_id, device_type, level, message
                FROM alerts WHERE acknowledged = 0
                ORDER BY timestamp DESC
            """)
            return [Alert(row[0], row[1], row[2], AlertLevel(row[3]), row[4])
                    for row in cursor.fetchall()]
    
    def acknowledge_alerts(self, device_id: Optional[str] = None):
        """Acknowledge alerts for device or all"""
        with sqlite3.connect(self.db_path) as conn:
            if device_id:
                conn.execute(
                    "UPDATE alerts SET acknowledged = 1 WHERE device_id = ?",
                    (device_id,)
                )
            else:
                conn.execute("UPDATE alerts SET acknowledged = 1")
            conn.commit()
    
    def export_data(self, device_id: str, start_date: float, end_date: float) -> List[Dict]:
        """Export measurements for CSV"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT timestamp, metric, value FROM measurements
                WHERE device_id = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp
            """, (device_id, start_date, end_date))
            
            return [{"timestamp": row[0], "metric": row[1], "value": row[2]}
                    for row in cursor.fetchall()]

# ============== Device Drivers ==============

class ASICDriver:
    """Driver for ASIC miner communication"""
    
    @staticmethod
    async def poll(config: ASICConfig) -> Dict[str, Any]:
        """Poll ASIC miner for stats"""
        try:
            reader, writer = await asyncio.open_connection(
                config.ip_address, config.port
            )
            
            # Send stats command
            request = json.dumps({"command": "stats"})
            writer.write(request.encode())
            await writer.drain()
            
            # Read response
            data = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            
            # Parse JSON response
            response = json.loads(data.decode().rstrip('\x00'))
            
            # Extract key metrics
            stats = response.get("STATS", [{}])[0]
            return {
                "temperature": stats.get("temp", 0),
                "hashrate": stats.get("GHS 5s", 0),
                "fan_speed": stats.get("fan", 0),
                "accepted": stats.get("Accepted", 0),
                "rejected": stats.get("Rejected", 0),
                "hardware_errors": stats.get("Hardware Errors", 0)
            }
            
        except Exception as e:
            logger.error(f"ASIC poll error for {config.ip_address}: {e}")
            raise

class MercuryDriver:
    """Driver for Mercury 236 meter communication"""
    
    @staticmethod
    def calculate_crc16(data: bytes) -> int:
        """Calculate CRC-16 for Mercury protocol"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc
    
    @staticmethod
    async def poll(config: MercuryConfig) -> Dict[str, Any]:
        """Poll Mercury meter for readings"""
        try:
            # Configure serial port
            ser = serial.Serial(
                port=config.serial_port,
                baudrate=config.baudrate,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=1,
                timeout=2
            )
            
            # Prepare request
            request = f"R1".encode()
            crc = MercuryDriver.calculate_crc16(request)
            request += struct.pack('<H', crc)
            
            # Send request
            ser.write(request)
            
            # Read response
            response = ser.read(64)
            ser.close()
            
            if len(response) < 4:
                raise ValueError("Invalid response length")
            
            # Verify CRC
            data_crc = struct.unpack('<H', response[-2:])[0]
            calc_crc = MercuryDriver.calculate_crc16(response[:-2])
            
            if data_crc != calc_crc:
                raise ValueError("CRC mismatch")
            
            # Parse response (simplified - actual format depends on meter model)
            # Assuming response contains voltage, current, power, energy
            return {
                "voltage": struct.unpack('<f', response[2:6])[0],
                "current": struct.unpack('<f', response[6:10])[0],
                "power": struct.unpack('<f', response[10:14])[0],
                "energy": struct.unpack('<f', response[14:18])[0]
            }
            
        except Exception as e:
            logger.error(f"Mercury poll error for {config.serial_port}: {e}")
            raise

class VFDDriver:
    """Driver for Variable Frequency Drive communication"""
    
    @staticmethod
    async def poll(config: VFDConfig) -> Dict[str, Any]:
        """Poll VFD via Modbus RTU"""
        try:
            client = ModbusSerialClient(
                port=config.serial_port,
                baudrate=config.baudrate,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=2
            )
            
            if not client.connect():
                raise ConnectionError("Failed to connect to Modbus")
            
            # Read registers 1-4
            result = client.read_holding_registers(
                address=0,
                count=4,
                slave=config.slave_id
            )
            
            client.close()
            
            if result.isError():
                raise ModbusException(f"Modbus error: {result}")
            
            # Parse register values
            registers = result.registers
            return {
                "set_frequency": registers[0] / 100.0,  # Hz
                "output_frequency": registers[1] / 100.0,  # Hz
                "output_current": registers[2] / 10.0,  # A
                "heatsink_temp": registers[3],  # °C
                "status_word": registers[3] if len(registers) > 4 else 0
            }
            
        except Exception as e:
            logger.error(f"VFD poll error for {config.serial_port}: {e}")
            raise

# ============== Alert Engine ==============

class AlertEngine:
    """Alert generation and management"""
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.hashrate_history = {}  # Store rolling averages
        
    def check_asic_alerts(self, config: ASICConfig, data: Dict) -> List[Alert]:
        """Check ASIC measurements for alert conditions"""
        alerts = []
        now = time.time()
        
        # Temperature alerts
        temp = data.get("temperature", 0)
        if temp >= config.temp_crit:
            alerts.append(Alert(
                now, config.id, "ASIC", AlertLevel.CRITICAL,
                f"Temperature critical: {temp}°C (threshold: {config.temp_crit}°C)"
            ))
        elif temp >= config.temp_warn:
            alerts.append(Alert(
                now, config.id, "ASIC", AlertLevel.WARNING,
                f"Temperature warning: {temp}°C (threshold: {config.temp_warn}°C)"
            ))
        
        # Hashrate drop alerts
        current_hashrate = data.get("hashrate", 0)
        
        # Calculate 6-hour rolling average
        history_key = config.id
        if history_key not in self.hashrate_history:
            self.hashrate_history[history_key] = []
        
        self.hashrate_history[history_key].append((now, current_hashrate))
        
        # Keep only last 6 hours
        cutoff = now - (6 * 3600)
        self.hashrate_history[history_key] = [
            (t, h) for t, h in self.hashrate_history[history_key] if t > cutoff
        ]
        
        if len(self.hashrate_history[history_key]) > 10:  # Need enough samples
            avg_hashrate = sum(h for _, h in self.hashrate_history[history_key]) / len(self.hashrate_history[history_key])
            
            if avg_hashrate > 0:
                drop_percent = ((avg_hashrate - current_hashrate) / avg_hashrate) * 100
                
                if drop_percent >= config.hashrate_crit_drop:
                    alerts.append(Alert(
                        now, config.id, "ASIC", AlertLevel.CRITICAL,
                        f"Hashrate drop critical: {drop_percent:.1f}% (current: {current_hashrate:.1f} GH/s)"
                    ))
                elif drop_percent >= config.hashrate_warn_drop:
                    alerts.append(Alert(
                        now, config.id, "ASIC", AlertLevel.WARNING,
                        f"Hashrate drop warning: {drop_percent:.1f}% (current: {current_hashrate:.1f} GH/s)"
                    ))
        
        return alerts
    
    def check_vfd_alerts(self, config: VFDConfig, data: Dict) -> List[Alert]:
        """Check VFD measurements for alert conditions"""
        alerts = []
        now = time.time()
        
        # Temperature alerts
        temp = data.get("heatsink_temp", 0)
        if temp >= config.temp_crit:
            alerts.append(Alert(
                now, config.id, "VFD", AlertLevel.CRITICAL,
                f"Heatsink temperature critical: {temp}°C (threshold: {config.temp_crit}°C)"
            ))
        elif temp >= config.temp_warn:
            alerts.append(Alert(
                now, config.id, "VFD", AlertLevel.WARNING,
                f"Heatsink temperature warning: {temp}°C (threshold: {config.temp_warn}°C)"
            ))
        
        # Check status word for fault bits
        status = data.get("status_word", 0)
        if status & 0xFF00:  # Any fault bit set
            alerts.append(Alert(
                now, config.id, "VFD", AlertLevel.CRITICAL,
                f"VFD fault detected: status word 0x{status:04X}"
            ))
        
        return alerts
    
    def check_connection_alert(self, device_id: str, device_type: str) -> Alert:
        """Generate connection lost alert"""
        return Alert(
            time.time(), device_id, device_type, AlertLevel.CRITICAL,
            f"Connection lost to {device_type} device: {device_id}"
        )

# ============== Polling Manager ==============

class PollingManager(QThread):
    """Async polling manager running in separate thread"""
    
    data_received = pyqtSignal(str, str, dict)  # device_type, device_id, data
    alert_generated = pyqtSignal(Alert)
    
    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db
        self.alert_engine = AlertEngine(db)
        self.running = True
        self.configs = {
            "ASIC": [],
            "Mercury": [],
            "VFD": []
        }
        self.last_poll = {}
        
    def reload_configs(self):
        """Reload configurations from database"""
        self.configs["ASIC"] = [
            ASICConfig(**cfg) for cfg in self.db.load_configs("ASIC")
        ]
        self.configs["Mercury"] = [
            MercuryConfig(**cfg) for cfg in self.db.load_configs("Mercury")
        ]
        self.configs["VFD"] = [
            VFDConfig(**cfg) for cfg in self.db.load_configs("VFD")
        ]
    
    async def poll_device(self, device_type: str, config: DeviceConfig):
        """Poll single device"""
        try:
            # Check if it's time to poll
            last = self.last_poll.get(config.id, 0)
            if time.time() - last < config.poll_interval:
                return
            
            # Poll based on device type
            if device_type == "ASIC":
                data = await ASICDriver.poll(config)
                alerts = self.alert_engine.check_asic_alerts(config, data)
            elif device_type == "Mercury":
                data = await MercuryDriver.poll(config)
                alerts = []
            elif device_type == "VFD":
                data = await VFDDriver.poll(config)
                alerts = self.alert_engine.check_vfd_alerts(config, data)
            else:
                return
            
            # Update last poll time
            self.last_poll[config.id] = time.time()
            
            # Save measurements
            for metric, value in data.items():
                self.db.save_measurement(config.id, metric, value)
            
            # Emit data signal
            self.data_received.emit(device_type, config.id, data)
            
            # Process alerts
            for alert in alerts:
                self.db.save_alert(alert)
                self.alert_generated.emit(alert)
                
        except Exception as e:
            # Connection error alert
            alert = self.alert_engine.check_connection_alert(config.id, device_type)
            self.db.save_alert(alert)
            self.alert_generated.emit(alert)
    
    async def poll_all(self):
        """Poll all configured devices"""
        tasks = []
        
        for device_type, configs in self.configs.items():
            for config in configs:
                if config.enabled:
                    tasks.append(self.poll_device(device_type, config))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    def run(self):
        """Main polling loop"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        while self.running:
            self.reload_configs()
            loop.run_until_complete(self.poll_all())
            time.sleep(1)  # Small delay between polling cycles
    
    def stop(self):
        """Stop polling"""
        self.running = False

# ============== UI Components ==============

class DashboardWidget(QWidget):
    """Main dashboard with status cards and graphs"""
    
    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db
        self.init_ui()
        
        # Update timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_display)
        self.timer.start(1000)  # Update every second
        
    def init_ui(self):
        layout = QVBoxLayout()
        
        # Status cards
        status_layout = QHBoxLayout()
        
        self.status_cards = {}
        for level in ["OK", "WARNING", "CRITICAL"]:
            card = QFrame()
            card.setFrameStyle(QFrame.Shape.Box)
            card_layout = QVBoxLayout()
            
            label = QLabel(level)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
            
            count = QLabel("0")
            count.setAlignment(Qt.AlignmentFlag.AlignCenter)
            count.setFont(QFont("Arial", 24))
            
            card_layout.addWidget(label)
            card_layout.addWidget(count)
            card.setLayout(card_layout)
            
            self.status_cards[level] = (card, count)
            status_layout.addWidget(card)
        
        layout.addLayout(status_layout)
        
        # Time range selector
        range_layout = QHBoxLayout()
        range_layout.addWidget(QLabel("Time Range:"))
        
        self.time_range = QComboBox()
        self.time_range.addItems(["Last Hour", "Last 24 Hours", "Last Week"])
        self.time_range.currentTextChanged.connect(self.update_graphs)
        range_layout.addWidget(self.time_range)
        range_layout.addStretch()
        
        layout.addLayout(range_layout)
        
        # Graphs
        self.graph_widget = pg.GraphicsLayoutWidget()
        
        # Temperature/Hashrate graph
        self.temp_plot = self.graph_widget.addPlot(title="Temperature & Hashrate")
        self.temp_plot.setLabel('left', 'Temperature', units='°C')
        self.temp_plot.setLabel('right', 'Hashrate', units='GH/s')
        self.temp_plot.setLabel('bottom', 'Time')
        self.temp_plot.addLegend()
        
        self.temp_curve = self.temp_plot.plot(pen='r', name='Temperature')
        self.hash_curve = self.temp_plot.plot(pen='g', name='Hashrate')
        
        self.graph_widget.nextRow()
        
        # Energy graph
        self.energy_plot = self.graph_widget.addPlot(title="Energy Consumption")
        self.energy_plot.setLabel('left', 'Energy', units='kWh')
        self.energy_plot.setLabel('bottom', 'Time')
        self.energy_curve = self.energy_plot.plot(pen='b')
        
        layout.addWidget(self.graph_widget)
        
        # Alert banner
        self.alert_banner = QFrame()
        self.alert_banner.setFrameStyle(QFrame.Shape.Box)
        self.alert_banner.setMaximumHeight(100)
        self.alert_banner.hide()
        
        alert_layout = QVBoxLayout()
        self.alert_text = QLabel()
        self.alert_text.setWordWrap(True)
        
        acknowledge_btn = QPushButton("Acknowledge")
        acknowledge_btn.clicked.connect(self.acknowledge_alerts)
        
        alert_layout.addWidget(self.alert_text)
        alert_layout.addWidget(acknowledge_btn)
        self.alert_banner.setLayout(alert_layout)
        
        layout.addWidget(self.alert_banner)
        
        self.setLayout(layout)
    
    def update_display(self):
        """Update status cards and alerts"""
        # Get active alerts
        alerts = self.db.get_active_alerts()
        
        # Count by level
        counts = {"OK": 0, "WARNING": 0, "CRITICAL": 0}
        for alert in alerts:
            if alert.level == AlertLevel.WARNING:
                counts["WARNING"] += 1
            elif alert.level == AlertLevel.CRITICAL:
                counts["CRITICAL"] += 1
        
        # Update status cards
        for level, (card, count_label) in self.status_cards.items():
            count = counts.get(level, 0)
            count_label.setText(str(count))
            
            # Update card color
            if level == "OK":
                color = "green" if counts["WARNING"] == 0 and counts["CRITICAL"] == 0 else "gray"
            elif level == "WARNING":
                color = "yellow" if count > 0 else "gray"
            else:  # CRITICAL
                color = "red" if count > 0 else "gray"
            
            card.setStyleSheet(f"QFrame {{ background-color: {color}; }}")
        
        # Update alert banner
        if alerts:
            self.alert_banner.show()
            alert_messages = [f"{a.device_id}: {a.message}" for a in alerts[:3]]
            self.alert_text.setText("\n".join(alert_messages))
            
            if any(a.level == AlertLevel.CRITICAL for a in alerts):
                self.alert_banner.setStyleSheet("QFrame { background-color: red; }")
            else:
                self.alert_banner.setStyleSheet("QFrame { background-color: yellow; }")
        else:
            self.alert_banner.hide()
    
    def update_graphs(self):
        """Update graph data based on selected time range"""
        # Get time range in hours
        range_text = self.time_range.currentText()
        if "Hour" in range_text:
            hours = 1
        elif "24" in range_text:
            hours = 24
        else:  # Week
            hours = 168
        
        # TODO: Load and plot actual data from database
        # This is a placeholder implementation
        pass
    
    def acknowledge_alerts(self):
        """Acknowledge all active alerts"""
        self.db.acknowledge_alerts()
        self.update_display()
    
    def update_data(self, device_type: str, device_id: str, data: dict):
        """Update graphs with new data"""
        # TODO: Update graph curves with new data points
        pass

class DeviceListWidget(QWidget):
    """Widget for displaying and managing devices"""
    
    def __init__(self, device_type: str, db: DatabaseManager):
        super().__init__()
        self.device_type = device_type
        self.db = db
        self.init_ui()
        self.load_devices()
        
    def init_ui(self):
        layout = QVBoxLayout()
        
        # Device table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ID", "Name", "Status", "Last Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        
        layout.addWidget(self.table)
        
        # Control buttons
        btn_layout = QHBoxLayout()
        
        add_btn = QPushButton("Add Device")
        add_btn.clicked.connect(self.add_device)
        btn_layout.addWidget(add_btn)
        
        edit_btn = QPushButton("Edit Device")
        edit_btn.clicked.connect(self.edit_device)
        btn_layout.addWidget(edit_btn)
        
        delete_btn = QPushButton("Delete Device")
        delete_btn.clicked.connect(self.delete_device)
        btn_layout.addWidget(delete_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
    
    def load_devices(self):
        """Load devices from database"""
        configs = self.db.load_configs(self.device_type)
        
        self.table.setRowCount(len(configs))
        for i, config in enumerate(configs):
            self.table.setItem(i, 0, QTableWidgetItem(config["id"]))
            self.table.setItem(i, 1, QTableWidgetItem(config["name"]))
            self.table.setItem(i, 2, QTableWidgetItem("Active" if config.get("enabled", True) else "Disabled"))
            self.table.setItem(i, 3, QTableWidgetItem("-"))
    
    def add_device(self):
        """Show dialog to add new device"""
        dialog = DeviceConfigDialog(self.device_type, None, self.db)
        if dialog.exec():
            self.load_devices()
    
    def edit_device(self):
        """Edit selected device"""
        row = self.table.currentRow()
        if row >= 0:
            device_id = self.table.item(row, 0).text()
            configs = self.db.load_configs(self.device_type)
            config = next((c for c in configs if c["id"] == device_id), None)
            
            if config:
                dialog = DeviceConfigDialog(self.device_type, config, self.db)
                if dialog.exec():
                    self.load_devices()
    
    def delete_device(self):
        """Delete selected device"""
        row = self.table.currentRow()
        if row >= 0:
            device_id = self.table.item(row, 0).text()
            
            reply = QMessageBox.question(
                self, "Confirm Delete",
                f"Delete device {device_id}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                self.db.delete_config(device_id)
                self.load_devices()
    
    def update_data(self, device_id: str, data: dict):
        """Update device data in table"""
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == device_id:
                # Update last value with key metric
                if self.device_type == "ASIC":
                    value = f"{data.get('temperature', 0):.1f}°C / {data.get('hashrate', 0):.1f} GH/s"
                elif self.device_type == "Mercury":
                    value = f"{data.get('power', 0):.1f} W"
                else:  # VFD
                    value = f"{data.get('output_frequency', 0):.1f} Hz"
                
                self.table.setItem(row, 3, QTableWidgetItem(value))
                break

class DeviceConfigDialog(QDialog):
    """Dialog for device configuration"""
    
    def __init__(self, device_type: str, config: Optional[Dict], db: DatabaseManager):
        super().__init__()
        self.device_type = device_type
        self.config = config
        self.db = db
        self.init_ui()
        
        if config:
            self.load_config()
    
    def init_ui(self):
        self.setWindowTitle(f"Configure {self.device_type} Device")
        self.setModal(True)
        self.resize(400, 500)
        
        layout = QFormLayout()
        
        # Common fields
        self.id_edit = QLineEdit()
        if self.config:
            self.id_edit.setEnabled(False)
        layout.addRow("Device ID:", self.id_edit)
        
        self.name_edit = QLineEdit()
        layout.addRow("Device Name:", self.name_edit)
        
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(True)
        layout.addRow("Status:", self.enabled_check)
        
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(10, 300)
        self.poll_spin.setValue(30)
        self.poll_spin.setSuffix(" seconds")
        layout.addRow("Poll Interval:", self.poll_spin)
        
        # Device-specific fields
        if self.device_type == "ASIC":
            self.ip_edit = QLineEdit()
            layout.addRow("IP Address:", self.ip_edit)
            
            self.port_spin = QSpinBox()
            self.port_spin.setRange(1, 65535)
            self.port_spin.setValue(4028)
            layout.addRow("Port:", self.port_spin)
            
            self.temp_warn_spin = QDoubleSpinBox()
            self.temp_warn_spin.setRange(50, 100)
            self.temp_warn_spin.setValue(80)
            self.temp_warn_spin.setSuffix(" °C")
            layout.addRow("Temp Warning:", self.temp_warn_spin)
            
            self.temp_crit_spin = QDoubleSpinBox()
            self.temp_crit_spin.setRange(50, 100)
            self.temp_crit_spin.setValue(90)
            self.temp_crit_spin.setSuffix(" °C")
            layout.addRow("Temp Critical:", self.temp_crit_spin)
            
            self.hash_warn_spin = QDoubleSpinBox()
            self.hash_warn_spin.setRange(5, 50)
            self.hash_warn_spin.setValue(15)
            self.hash_warn_spin.setSuffix(" %")
            layout.addRow("Hashrate Drop Warning:", self.hash_warn_spin)
            
            self.hash_crit_spin = QDoubleSpinBox()
            self.hash_crit_spin.setRange(5, 50)
            self.hash_crit_spin.setValue(25)
            self.hash_crit_spin.setSuffix(" %")
            layout.addRow("Hashrate Drop Critical:", self.hash_crit_spin)
            
        elif self.device_type == "Mercury":
            self.port_combo = QComboBox()
            self.port_combo.addItems([f"COM{i}" for i in range(1, 21)])
            self.port_combo.setEditable(True)
            layout.addRow("Serial Port:", self.port_combo)
            
            self.address_spin = QSpinBox()
            self.address_spin.setRange(1, 247)
            self.address_spin.setValue(1)
            layout.addRow("Device Address:", self.address_spin)
            
            self.baud_combo = QComboBox()
            self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
            layout.addRow("Baud Rate:", self.baud_combo)
            
        else:  # VFD
            self.port_combo = QComboBox()
            self.port_combo.addItems([f"COM{i}" for i in range(1, 21)])
            self.port_combo.setEditable(True)
            layout.addRow("Serial Port:", self.port_combo)
            
            self.slave_spin = QSpinBox()
            self.slave_spin.setRange(1, 247)
            self.slave_spin.setValue(1)
            layout.addRow("Slave ID:", self.slave_spin)
            
            self.baud_combo = QComboBox()
            self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
            layout.addRow("Baud Rate:", self.baud_combo)
            
            self.temp_warn_spin = QDoubleSpinBox()
            self.temp_warn_spin.setRange(50, 100)
            self.temp_warn_spin.setValue(70)
            self.temp_warn_spin.setSuffix(" °C")
            layout.addRow("Temp Warning:", self.temp_warn_spin)
            
            self.temp_crit_spin = QDoubleSpinBox()
            self.temp_crit_spin.setRange(50, 100)
            self.temp_crit_spin.setValue(80)
            self.temp_crit_spin.setSuffix(" °C")
            layout.addRow("Temp Critical:", self.temp_crit_spin)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | 
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save_config)
        buttons.rejected.connect(self.reject)
        
        layout.addRow(buttons)
        self.setLayout(layout)
    
    def load_config(self):
        """Load existing configuration"""
        self.id_edit.setText(self.config["id"])
        self.name_edit.setText(self.config["name"])
        self.enabled_check.setChecked(self.config.get("enabled", True))
        self.poll_spin.setValue(self.config.get("poll_interval", 30))
        
        if self.device_type == "ASIC":
            self.ip_edit.setText(self.config.get("ip_address", ""))
            self.port_spin.setValue(self.config.get("port", 4028))
            self.temp_warn_spin.setValue(self.config.get("temp_warn", 80))
            self.temp_crit_spin.setValue(self.config.get("temp_crit", 90))
            self.hash_warn_spin.setValue(self.config.get("hashrate_warn_drop", 15))
            self.hash_crit_spin.setValue(self.config.get("hashrate_crit_drop", 25))
            
        elif self.device_type == "Mercury":
            self.port_combo.setCurrentText(self.config.get("serial_port", "COM1"))
            self.address_spin.setValue(self.config.get("address", 1))
            self.baud_combo.setCurrentText(str(self.config.get("baudrate", 9600)))
            
        else:  # VFD
            self.port_combo.setCurrentText(self.config.get("serial_port", "COM1"))
            self.slave_spin.setValue(self.config.get("slave_id", 1))
            self.baud_combo.setCurrentText(str(self.config.get("baudrate", 9600)))
            self.temp_warn_spin.setValue(self.config.get("temp_warn", 70))
            self.temp_crit_spin.setValue(self.config.get("temp_crit", 80))
    
    def save_config(self):
        """Save configuration to database"""
        # Validate required fields
        if not self.id_edit.text() or not self.name_edit.text():
            QMessageBox.warning(self, "Validation Error", "ID and Name are required!")
            return
        
        # Build config based on device type
        if self.device_type == "ASIC":
            if not self.ip_edit.text():
                QMessageBox.warning(self, "Validation Error", "IP Address is required!")
                return
                
            config = ASICConfig(
                id=self.id_edit.text(),
                name=self.name_edit.text(),
                enabled=self.enabled_check.isChecked(),
                poll_interval=self.poll_spin.value(),
                ip_address=self.ip_edit.text(),
                port=self.port_spin.value(),
                temp_warn=self.temp_warn_spin.value(),
                temp_crit=self.temp_crit_spin.value(),
                hashrate_warn_drop=self.hash_warn_spin.value(),
                hashrate_crit_drop=self.hash_crit_spin.value()
            )
            
        elif self.device_type == "Mercury":
            config = MercuryConfig(
                id=self.id_edit.text(),
                name=self.name_edit.text(),
                enabled=self.enabled_check.isChecked(),
                poll_interval=self.poll_spin.value(),
                serial_port=self.port_combo.currentText(),
                address=self.address_spin.value(),
                baudrate=int(self.baud_combo.currentText())
            )
            
        else:  # VFD
            config = VFDConfig(
                id=self.id_edit.text(),
                name=self.name_edit.text(),
                enabled=self.enabled_check.isChecked(),
                poll_interval=self.poll_spin.value(),
                serial_port=self.port_combo.currentText(),
                slave_id=self.slave_spin.value(),
                baudrate=int(self.baud_combo.currentText()),
                temp_warn=self.temp_warn_spin.value(),
                temp_crit=self.temp_crit_spin.value()
            )
        
        # Save to database
        self.db.save_config(self.device_type, config)
        self.accept()

class SettingsWidget(QWidget):
    """Settings widget with tabs for each device type"""
    
    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout()
        
        # Tab widget for device types
        self.tabs = QTabWidget()
        
        # Add tabs for each device type
        self.asic_widget = DeviceListWidget("ASIC", self.db)
        self.tabs.addTab(self.asic_widget, "ASIC Miners")
        
        self.mercury_widget = DeviceListWidget("Mercury", self.db)
        self.tabs.addTab(self.mercury_widget, "Mercury Meters")
        
        self.vfd_widget = DeviceListWidget("VFD", self.db)
        self.tabs.addTab(self.vfd_widget, "VFD Drives")
        
        layout.addWidget(self.tabs)
        
        # Export section
        export_group = QGroupBox("Data Export")
        export_layout = QFormLayout()
        
        self.export_device = QComboBox()
        self.load_device_list()
        export_layout.addRow("Device:", self.export_device)
        
        date_layout = QHBoxLayout()
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addDays(-7))
        date_layout.addWidget(QLabel("From:"))
        date_layout.addWidget(self.start_date)
        
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        date_layout.addWidget(QLabel("To:"))
        date_layout.addWidget(self.end_date)
        
        export_layout.addRow("Date Range:", date_layout)
        
        export_btn = QPushButton("Export to CSV")
        export_btn.clicked.connect(self.export_data)
        export_layout.addRow(export_btn)
        
        export_group.setLayout(export_layout)
        layout.addWidget(export_group)
        
        self.setLayout(layout)
    
    def load_device_list(self):
        """Load all devices for export combo"""
        self.export_device.clear()
        
        for device_type in ["ASIC", "Mercury", "VFD"]:
            configs = self.db.load_configs(device_type)
            for config in configs:
                self.export_device.addItem(f"{device_type}: {config['name']} ({config['id']})")
    
    def export_data(self):
        """Export selected device data to CSV"""
        if self.export_device.count() == 0:
            QMessageBox.warning(self, "No Devices", "No devices configured!")
            return
        
        # Parse device selection
        text = self.export_device.currentText()
        device_id = text.split("(")[-1].rstrip(")")
        
        # Get date range
        start = self.start_date.date().toPyDate()
        end = self.end_date.date().toPyDate()
        
        start_timestamp = datetime.combine(start, datetime.min.time()).timestamp()
        end_timestamp = datetime.combine(end, datetime.max.time()).timestamp()
        
        # Get data
        data = self.db.export_data(device_id, start_timestamp, end_timestamp)
        
        if not data:
            QMessageBox.information(self, "No Data", "No data found for selected period!")
            return
        
        # Save to CSV
        os.makedirs("exports", exist_ok=True)
        filename = f"exports/{device_id}_{start.isoformat()}_{end.isoformat()}.csv"
        
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "metric", "value"])
            writer.writeheader()
            writer.writerows(data)
        
        QMessageBox.information(self, "Export Complete", f"Data exported to {filename}")
    
    def update_data(self, device_type: str, device_id: str, data: dict):
        """Update device data in appropriate tab"""
        if device_type == "ASIC":
            self.asic_widget.update_data(device_id, data)
        elif device_type == "Mercury":
            self.mercury_widget.update_data(device_id, data)
        elif device_type == "VFD":
            self.vfd_widget.update_data(device_id, data)

class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.db = DatabaseManager()
        self.polling_manager = PollingManager(self.db)
        self.init_ui()
        
        # Connect polling signals
        self.polling_manager.data_received.connect(self.handle_data)
        self.polling_manager.alert_generated.connect(self.handle_alert)
        
        # Start polling
        self.polling_manager.start()
    
    def init_ui(self):
        self.setWindowTitle("Industrial Equipment Monitor")
        self.showFullScreen()
        
        # Main widget
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        # Main layout
        main_layout = QHBoxLayout()
        main_widget.setLayout(main_layout)
        
        # Left sidebar menu
        sidebar = QListWidget()
        sidebar.setMaximumWidth(200)
        sidebar.setStyleSheet("""
            QListWidget {
                background-color: #2b2b2b;
                color: white;
                font-size: 14px;
            }
            QListWidget::item {
                padding: 10px;
            }
            QListWidget::item:selected {
                background-color: #4a4a4a;
            }
        """)
        
        # Add menu items
        menu_items = ["Dashboard", "ASIC", "Mercury", "VFD", "Settings"]
        for item in menu_items:
            sidebar.addItem(item)
        
        sidebar.currentRowChanged.connect(self.switch_view)
        main_layout.addWidget(sidebar)
        
        # Right content area
        self.content_stack = QStackedWidget()
        
        # Add widgets to stack
        self.dashboard = DashboardWidget(self.db)
        self.content_stack.addWidget(self.dashboard)
        
        self.asic_view = DeviceListWidget("ASIC", self.db)
        self.content_stack.addWidget(self.asic_view)
        
        self.mercury_view = DeviceListWidget("Mercury", self.db)
        self.content_stack.addWidget(self.mercury_view)
        
        self.vfd_view = DeviceListWidget("VFD", self.db)
        self.content_stack.addWidget(self.vfd_view)
        
        self.settings = SettingsWidget(self.db)
        self.content_stack.addWidget(self.settings)
        
        main_layout.addWidget(self.content_stack)
        
        # Select dashboard by default
        sidebar.setCurrentRow(0)
    
    def switch_view(self, index):
        """Switch content view based on menu selection"""
        self.content_stack.setCurrentIndex(index)
    
    def handle_data(self, device_type: str, device_id: str, data: dict):
        """Handle new data from polling manager"""
        # Update dashboard
        self.dashboard.update_data(device_type, device_id, data)
        
        # Update device views
        if device_type == "ASIC":
            self.asic_view.update_data(device_id, data)
        elif device_type == "Mercury":
            self.mercury_view.update_data(device_id, data)
        elif device_type == "VFD":
            self.vfd_view.update_data(device_id, data)
        
        # Update settings view
        self.settings.update_data(device_type, device_id, data)
    
    def handle_alert(self, alert: Alert):
        """Handle new alert"""
        self.dashboard.update_display()
    
    def closeEvent(self, event):
        """Clean shutdown"""
        self.polling_manager.stop()
        self.polling_manager.wait()
        event.accept()

# ============== CLI Mode ==============

async def run_once():
    """Run single polling cycle for CLI mode"""
    db = DatabaseManager()
    
    # Load configurations
    asic_configs = [ASICConfig(**cfg) for cfg in db.load_configs("ASIC")]
    mercury_configs = [MercuryConfig(**cfg) for cfg in db.load_configs("Mercury")]
    vfd_configs = [VFDConfig(**cfg) for cfg in db.load_configs("VFD")]
    
    print("Starting single polling cycle...")
    
    # Poll ASICs
    for config in asic_configs:
        if config.enabled:
            try:
                data = await ASICDriver.poll(config)
                print(f"ASIC {config.name}: {data}")
                for metric, value in data.items():
                    db.save_measurement(config.id, metric, value)
            except Exception as e:
                print(f"Error polling ASIC {config.name}: {e}")
    
    # Poll Mercury meters
    for config in mercury_configs:
        if config.enabled:
            try:
                data = await MercuryDriver.poll(config)
                print(f"Mercury {config.name}: {data}")
                for metric, value in data.items():
                    db.save_measurement(config.id, metric, value)
            except Exception as e:
                print(f"Error polling Mercury {config.name}: {e}")
    
    # Poll VFDs
    for config in vfd_configs:
        if config.enabled:
            try:
                data = await VFDDriver.poll(config)
                print(f"VFD {config.name}: {data}")
                for metric, value in data.items():
                    db.save_measurement(config.id, metric, value)
            except Exception as e:
                print(f"Error polling VFD {config.name}: {e}")
    
    print("Polling cycle complete.")

# ============== Entry Points ==============

def main_gui():
    """Launch GUI application"""
    app = QApplication(sys.argv)
    
    # Set application style
    app.setStyle("Fusion")
    
    # Dark theme
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
    app.setPalette(palette)
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

def main_cli():
    """Launch CLI mode"""
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        asyncio.run(run_once())
    else:
        print("Usage: python -m app.cli once")

if __name__ == "__main__":
    # Check if running in CLI mode
    if "--cli" in sys.argv or "-m" in sys.argv:
        main_cli()
    else:
        main_gui()