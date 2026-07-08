#!/bin/bash
# Nightly Code Inspector — crawls scripts and plugins, reports to Becca
python3 /home/cflux/.hermes/scripts/inspector.py --scripts 2>&1
echo ""
echo "=== PLUGINS ==="
python3 /home/cflux/.hermes/scripts/inspector.py $(find ~/.hermes/plugins -name "*.py" -not -path "*/__pycache__/*" -not -path "*/mnemosyne/*" 2>/dev/null) 2>&1