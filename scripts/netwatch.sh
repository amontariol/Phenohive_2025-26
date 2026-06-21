#!/bin/bash
# PhenoHive connectivity watchdog v2: classify NET/WAN/TS, capture, self-heal.
LOG=/var/log/phenohive-netwatch.log
VM=100.117.27.12          # VM's Tailscale IP = tailnet reachability probe
AUTO_REBOOT=0             # set 1 to allow reboot as last resort (10 min hard-down)
fails=0; down_since=0; down_class=""
snap(){ f=/var/log/phenohive-netdrop-$(date -u +%Y%m%dT%H%M%SZ).log
  { echo "## $(date -u) reason=$1"
    echo "## internet"; ping -c2 -W2 1.1.1.1 2>&1 | tail -2
    echo "## addr/link"; ip -4 addr show wlan0; iw dev wlan0 link | grep -iE 'connected|ssid|signal|bitrate'
    echo "## tailscale status"; timeout 8 tailscale status 2>&1 | head -20
    echo "## tailscaled+NM journal (10min)"; journalctl -u tailscaled -u NetworkManager --no-pager --since '10 min ago' | tail -80
  } >"$f" 2>&1; echo "$(date -u +%FT%TZ) SNAPSHOT($1) -> $f">>"$LOG"; }
while true; do
  T=$(date -u +%FT%TZ)
  GW=$(ip route 2>/dev/null|awk '/default/{print $3;exit}')
  PGW=$(ping -c1 -W2 "$GW" >/dev/null 2>&1 && echo OK||echo FAIL)
  PNET=$(ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && echo OK||echo FAIL)
  TSP=$(ping -c1 -W2 "$VM" >/dev/null 2>&1 && echo OK||echo FAIL)
  SIG=$(iw dev wlan0 link 2>/dev/null|grep -i signal|tr -d '\t')
  echo "$T pgw=$PGW pnet=$PNET tsping=$TSP gw=$GW $SIG">>"$LOG"
  if   [ "$PGW"  = FAIL ]; then CLASS=NET
  elif [ "$PNET" = FAIL ]; then CLASS=WAN
  elif [ "$TSP"  = FAIL ]; then CLASS=TS
  else CLASS=UP; fi
  if [ "$CLASS" != UP ]; then
    fails=$((fails+1))
    [ "$down_since" = 0 ]&&{ down_since=$(date +%s); down_class=$CLASS; snap "$CLASS"; }
    d=$(( $(date +%s)-down_since ))
    case "$down_class" in
      NET) [ "$fails" = 4 ]&&{ echo "$T HEAL bounce-wlan0">>"$LOG"; nmcli device disconnect wlan0; sleep 3; nmcli device connect wlan0; } ;;
      TS)  [ "$fails" = 3 ]&&{ echo "$T HEAL restart-tailscaled">>"$LOG"; systemctl restart tailscaled; } ;;
      WAN) : ;;   # upstream/ISP outage — nothing local to fix; record and wait
    esac
    [ "$AUTO_REBOOT" = 1 ]&&[ "$d" -ge 600 ]&&[ "$d" -lt 630 ]&&{ echo "$T HEAL reboot">>"$LOG"; reboot; }
  else
    [ "$down_since" != 0 ]&&echo "$T RECOVERED($down_class) after $(( $(date +%s)-down_since ))s">>"$LOG"
    fails=0; down_since=0; down_class=""
  fi
  sleep 30
done
