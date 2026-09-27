"""1s-interval system sampler for Fly shared-cpu-1x throttling diagnosis.

Key signal: cpu_steal_pct. On Fly shared-cpu, when burst credits deplete
the hypervisor throttles the vCPU and steal time spikes from ~0% to 30%+.
"""
import os
import time
import sys
import socket

DURATION = int(os.environ.get("DURATION", "120"))
HOSTNAME = socket.gethostname()
HZ = os.sysconf("SC_CLK_TCK")


def proc_stat():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    v = [int(x) for x in parts]
    return dict(user=v[0], nice=v[1], system=v[2], idle=v[3],
                iowait=v[4], irq=v[5], softirq=v[6], steal=v[7])


def proc_meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            m[k] = int(rest.split()[0])
    return m


def proc_loadavg():
    with open("/proc/loadavg") as f:
        return f.read().split()


def find_app_pids():
    out = []
    me = str(os.getpid())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmd = f.read().replace("\0", " ").strip()
            if "python" in cmd:
                out.append((int(pid), cmd[:80]))
        except OSError:
            pass
    return out


def proc_pid_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        rparen = data.rindex(")")
        rest = data[rparen + 2:].split()
        # After (comm) the fields are 0-indexed: state(0) ppid(1) ... utime(11) stime(12)
        return int(rest[11]) + int(rest[12])
    except (OSError, ValueError, IndexError):
        return None


apps = find_app_pids()
sys.stderr.write(f"[sampler] host={HOSTNAME} duration={DURATION}s python_procs={apps}\n")
sys.stderr.flush()

app_pid = apps[0][0] if apps else None
prev = proc_stat()
prev_app = proc_pid_ticks(app_pid) if app_pid else None
prev_t = time.time()

print("ts,host,user,sys,iowait,steal,idle,load1,mem_used_mb,mem_avail_mb,app_cpu_pct", flush=True)

t_end = time.time() + DURATION
while time.time() < t_end:
    time.sleep(1.0)
    cur = proc_stat()
    cur_t = time.time()
    d = {k: cur[k] - prev[k] for k in cur}
    total = sum(d.values())
    if total <= 0:
        continue

    if app_pid is not None:
        cur_app = proc_pid_ticks(app_pid)
        if cur_app is not None and prev_app is not None:
            elapsed = cur_t - prev_t
            app_pct = 100.0 * (cur_app - prev_app) / (HZ * elapsed) if elapsed > 0 else -1
        else:
            app_pct = -1
        prev_app = cur_app
    else:
        app_pct = -1

    prev = cur
    prev_t = cur_t

    mem = proc_meminfo()
    la = proc_loadavg()
    used_mb = (mem["MemTotal"] - mem["MemAvailable"]) // 1024
    avail_mb = mem["MemAvailable"] // 1024

    print(
        f"{cur_t:.2f},{HOSTNAME},"
        f"{100*d['user']/total:.1f},{100*d['system']/total:.1f},"
        f"{100*d['iowait']/total:.1f},{100*d['steal']/total:.1f},"
        f"{100*d['idle']/total:.1f},{la[0]},{used_mb},{avail_mb},{app_pct:.1f}",
        flush=True,
    )

sys.stderr.write(f"[sampler] done host={HOSTNAME}\n")
