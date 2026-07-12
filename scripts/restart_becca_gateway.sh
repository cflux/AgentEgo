#!/bin/bash
systemctl --user restart hermes-gateway
sleep 3
systemctl --user is-active hermes-gateway && echo "OK" || echo "FAIL"
