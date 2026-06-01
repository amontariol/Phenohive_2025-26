#!/bin/bash
IP_CONFIG="/boot/phenohive_server.txt"
TOKEN_CONFIG="/boot/phenohive_token.txt"
ENV_FILE="/opt/phenohive/.env"
if [ -f "$IP_CONFIG" ]; then
    NEW_IP=$(cat "$IP_CONFIG" | tr -d '\r\n ')
    if [[ $NEW_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        sed -i "s|INFLUX_URL=.*|INFLUX_URL=http://$NEW_IP:8081|" "$ENV_FILE"
    fi
fi
if [ -f "$TOKEN_CONFIG" ]; then
    NEW_TOKEN=$(cat "$TOKEN_CONFIG" | tr -d '\r\n ')
    if [ ! -z "$NEW_TOKEN" ]; then
        sed -i "s|INFLUX_TOKEN=.*|INFLUX_TOKEN=$NEW_TOKEN|" "$ENV_FILE"
    fi
fi
