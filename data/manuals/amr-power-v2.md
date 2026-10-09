---
{"source_id": "man-amr-power", "version": "2.1", "published_at": "2026-03-02", "models": ["AMR-200", "AMR-300"], "title": "AMR series - battery and power"}
---
## 1. Battery pack overview
AMR-200 and AMR-300 robots use a 48 V lithium iron phosphate pack. The battery management system (BMS) reports state of charge as battery_pct. The charge indicator LED on the body is driven by the dock contacts, not by the BMS: it can show full while the pack is low if the robot left the dock early.

## 2. Unexpected shutdown after a few minutes
Error E-PWR-02 means the BMS cut power because the cell voltage dropped under the protection threshold under load. Typical sign: the robot stops after one to five minutes of driving, even when the operator believes the battery is full. Check battery_pct in telemetry before anything else; a value under 20 percent with E-PWR-02 points to an insufficient charge, not to a motor fault.

## 3. Charging procedure
Dock the robot and wait for the dock LED to turn solid green. A full charge takes about 90 minutes. Do not interrupt charging under 80 percent during the first week of a new pack.

## 4. Pack replacement criteria
Replace the pack if E-PWR-02 recurs after three verified full charges, or if the BMS reports capacity under 70 percent of nominal. Pack replacement is done by the maintenance team only.
