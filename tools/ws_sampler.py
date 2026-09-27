"""TCP socket monitor for the voice pipeline app.

Runs alongside cpu_sampler.py. When CPU and memory look fine but audio
still distorts, look here for evidence of network-side jitter or local
buffer backpressure on the Deepgram / ElevenLabs / Twilio / Anthropic
sockets.

Samples /proc/net/tcp + /proc/net/tcp6 at SAMPLE_HZ (default 5 Hz) and
emits one CSV row per established socket owned by the uvicorn process.
Key columns: tx_q (kernel send buffer), rx_q (kernel recv buffer), retr
(retransmits). Sustained non-zero tx_q on a WS socket = the app is
slow to drain audio frames into the kernel. Rising retr = network loss.
"""
import os
import sys
import time
import socket

DURATION = int(os.environ.get("DURATION", "180"))
SAMPLE_HZ = float(os.environ.get("SAMPLE_HZ", "5"))
INTERVAL = 1.0 / SAMPLE_HZ
HOSTNAME = socket.gethostname()
STATE_ESTAB = "01"


def find_voice_pipeline_pid():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmd = f.read().replace("\0", " ")
        except OSError:
            continue
        if "uvicorn" in cmd and ("llm_proxy" in cmd or "main:app" in cmd):
            return int(pid)
    return None


def socket_inodes_for_pid(pid):
    inodes = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return inodes
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    return inodes


def parse_addr_v4(hex_str):
    addr_hex, port_hex = hex_str.split(":")
    ip = ".".join(str(b) for b in reversed(bytes.fromhex(addr_hex)))
    return ip, int(port_hex, 16)


def parse_addr_v6(hex_str):
    # IPv6 in /proc is 32 hex chars, byte-reversed per 4-byte group. For
    # diagnostic purposes the raw hex is fine, group-by works either way.
    addr_hex, port_hex = hex_str.split(":")
    return addr_hex, int(port_hex, 16)


def read_proc_net(path, parser, inodes):
    try:
        f = open(path)
    except OSError:
        return
    with f:
        f.readline()
        for line in f:
            parts = line.split()
            if len(parts) < 10 or parts[3] != STATE_ESTAB or parts[9] not in inodes:
                continue
            local_ip, local_port = parser(parts[1])
            rem_ip, rem_port = parser(parts[2])
            tx_rx = parts[4].split(":")
            tx_q = int(tx_rx[0], 16)
            rx_q = int(tx_rx[1], 16)
            retr = int(parts[6], 16)
            yield local_port, rem_ip, rem_port, tx_q, rx_q, retr


def main():
    pid = find_voice_pipeline_pid()
    sys.stderr.write(
        f"[ws_sampler] host={HOSTNAME} pid={pid} duration={DURATION}s "
        f"interval={INTERVAL:.2f}s\n"
    )
    sys.stderr.flush()
    if pid is None:
        sys.stderr.write("[ws_sampler] no voice pipeline pid found; aborting\n")
        sys.exit(1)

    print("ts,host,local_port,rem_ip,rem_port,tx_q,rx_q,retr,proto", flush=True)

    t_end = time.time() + DURATION
    while time.time() < t_end:
        loop_start = time.time()
        inodes = socket_inodes_for_pid(pid)
        if inodes:
            ts = time.time()
            for proto, path, parser in (
                ("tcp4", "/proc/net/tcp", parse_addr_v4),
                ("tcp6", "/proc/net/tcp6", parse_addr_v6),
            ):
                for lp, rip, rp, txq, rxq, retr in read_proc_net(path, parser, inodes):
                    print(f"{ts:.3f},{HOSTNAME},{lp},{rip},{rp},{txq},{rxq},{retr},{proto}", flush=True)

        slack = INTERVAL - (time.time() - loop_start)
        if slack > 0:
            time.sleep(slack)

    sys.stderr.write(f"[ws_sampler] done host={HOSTNAME}\n")


if __name__ == "__main__":
    main()
