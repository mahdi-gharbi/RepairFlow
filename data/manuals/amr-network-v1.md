---
{"source_id": "man-amr-network", "version": "1.4", "published_at": "2025-11-20", "models": ["AMR-200", "AMR-300"], "title": "AMR series - connectivity"}
---
## 1. Fleet network
Robots connect to the fleet manager over the warehouse wifi on a dedicated SSID. A tablet or phone being connected to wifi does not prove that the robot's own radio is connected.

## 2. Robot offline
Error E-NET-03 means the robot lost its session with the fleet manager for more than 30 seconds. The robot finishes its current segment and stops accepting missions. Check network_online in telemetry and the access point near the robot.

## 3. Recovery
Move the robot within range of a known access point and wait two minutes. If network_online stays false, restart the radio module from the service menu.
