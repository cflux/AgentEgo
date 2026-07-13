#!/bin/bash
systemctl --user restart hermes-gateway-tala
sleep 2
systemctl --user is-active hermes-gateway-tala && echo "Tala gateway restarted" || echo "FAIL"
