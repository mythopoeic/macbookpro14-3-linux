#!/bin/bash
# Installs the fan profile and the CPU sustained-power cap. Run as root.
#
# These are two halves of one thermal profile: the fan curve decides how hard
# the machine works to shed heat, the power cap decides how much heat it is
# allowed to make. Tuning either alone leaves the other fighting it.
set -e
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in mbpfan.conf cpu-power-limit cpu-power-limit.service; do
    [ -f "$SRC/$f" ] || { echo "error: $SRC/$f not found" >&2; exit 1; }
done

install -o root -g root -m 644 "$SRC/mbpfan.conf" /etc/mbpfan.conf
echo "installed /etc/mbpfan.conf"

install -o root -g root -m 755 "$SRC/cpu-power-limit" /usr/local/sbin/cpu-power-limit
echo "installed /usr/local/sbin/cpu-power-limit"

install -o root -g root -m 644 "$SRC/cpu-power-limit.service" \
    /etc/systemd/system/cpu-power-limit.service
echo "installed /etc/systemd/system/cpu-power-limit.service"

systemctl daemon-reload
systemctl restart mbpfan
systemctl enable --now cpu-power-limit.service
echo "restarted mbpfan, enabled cpu-power-limit"

echo
echo "--- verification ---"
bash -n /usr/local/sbin/cpu-power-limit && echo "cpu-power-limit: syntax OK"
systemd-analyze verify /etc/systemd/system/cpu-power-limit.service 2>&1 \
    | grep -viE 'spice-vdagent' | grep -v '^$' || echo "cpu-power-limit.service: OK"
echo "mbpfan:          $(systemctl is-active mbpfan)"
echo "cpu-power-limit: $(systemctl is-active cpu-power-limit)"
echo
# Read the values back rather than trusting the writes.
/usr/local/sbin/cpu-power-limit status
echo
grep -E '^(low_temp|high_temp|max_temp|polling_interval)' /etc/mbpfan.conf
