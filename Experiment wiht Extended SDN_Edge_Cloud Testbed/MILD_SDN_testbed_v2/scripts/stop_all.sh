#!/usr/bin/env bash
# Emergency clean-up if a run was killed hard (normally pipeline.py cleans up itself).
sudo pkill -f 'ryu-manager|osken-manager|api_service.py|analytics_service.py|telemetry_service.py|resource_exporter.py|netprobe_exporter.py|loadgen.py|iot_producer.py|crosstraffic.py|mild_sdn2_prom|iperf3 -s' || true
sudo mn -c >/dev/null 2>&1 || true
sudo umount -l /mnt/mild_cloud_volume 2>/dev/null || true
echo "[OK] cleaned up"
