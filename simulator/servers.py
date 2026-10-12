"""
Server management layer.

The original lab simulated a single machine (Server-01, defined in config.SERVER).
This module lets the operator add more simulated servers. Every server is
configured ONLY from the predefined options below (no free-text technical
values), and `build_spec()` turns a saved server into the same kind of
dictionary as config.SERVER, so the existing simulation / detection /
recovery code runs unchanged on each server.

How a server's configuration feeds the crash analysis:
  * vCPUs          -> number of cores in the scheduler model, run-queue maths,
                      baseline CPU level (fewer cores = busier baseline)
  * RAM            -> available-memory metric, size of the memory hog,
                      baseline memory level (less RAM = higher baseline use)
  * storage type   -> disk IOPS capacity (HDD / SSD / NVMe)
  * server type    -> normal workload (request rate and disk read/write mix)
  * network type   -> baseline network latency and packet loss
  * connection     -> Offline servers cannot be analysed
"""
from datetime import datetime

from . import config

# ------------------------------------------------------- predefined options ---
SERVER_TYPES = ["Web Server", "Database Server", "Application Server", "File Server",
                "DNS Server", "Mail Server", "Development Server", "Virtual Machine"]

ROLES = ["Web", "Database", "Application", "Storage", "Network", "Development"]

# default role suggested for each server type (the user can still change it)
TYPE_ROLE = {
    "Web Server": "Web", "Database Server": "Database", "Application Server": "Application",
    "File Server": "Storage", "DNS Server": "Network", "Mail Server": "Application",
    "Development Server": "Development", "Virtual Machine": "Application",
}

# OS -> family, versions (version -> kernel), default filesystem
OPERATING_SYSTEMS = {
    "Ubuntu Server": {"family": "linux", "fs": "ext4", "versions": {
        "20.04 LTS": "5.4.0-182-generic", "22.04 LTS": "5.15.0-91-generic", "24.04 LTS": "6.8.0-45-generic"}},
    "Debian": {"family": "linux", "fs": "ext4", "versions": {
        "11 (Bullseye)": "5.10.0-28-amd64", "12 (Bookworm)": "6.1.0-25-amd64"}},
    "Fedora Server": {"family": "linux", "fs": "Btrfs", "versions": {
        "40": "6.8.5-301.fc40", "41": "6.11.4-301.fc41"}},
    "Red Hat Enterprise Linux": {"family": "linux", "fs": "XFS", "versions": {
        "8": "4.18.0-553.el8", "9": "5.14.0-427.el9"}},
    "CentOS": {"family": "linux", "fs": "XFS", "versions": {
        "7": "3.10.0-1160.el7", "Stream 9": "5.14.0-503.el9"}},
    "Windows Server": {"family": "windows", "fs": "NTFS", "versions": {
        "2019": "NT 10.0 (build 17763)", "2022": "NT 10.0 (build 20348)", "2025": "NT 10.0 (build 26100)"}},
}

FILESYSTEMS = {"linux": ["ext4", "XFS", "Btrfs"], "windows": ["NTFS", "ReFS"]}

CPU_OPTIONS = [2, 4, 8, 16, 32]                 # vCPU
RAM_OPTIONS = [2, 4, 8, 16, 32, 64]             # GB
STORAGE_OPTIONS = [50, 100, 250, 500, 1000]     # GB
STORAGE_TYPES = {                               # modelled device capacity
    "HDD": {"iops": 2000, "label": "HDD"},
    "SSD": {"iops": 4000, "label": "SSD"},
    "NVMe": {"iops": 6000, "label": "NVMe SSD"},
}
NETWORK_TYPES = {                               # baseline RTT (ms) and packet loss (%)
    "1 Gbps Ethernet": {"lat": 18.0, "loss": 0.08},
    "10 Gbps Ethernet": {"lat": 12.0, "loss": 0.05},
    "Wi-Fi": {"lat": 28.0, "loss": 0.40},
    "Virtual Network": {"lat": 22.0, "loss": 0.10},
}
CONNECTIONS = ["Online", "Intermittent", "Offline"]

# normal workload per server type: client requests/s and disk read/write IOPS
WORKLOAD = {
    "Web Server": (860, 300, 190),
    "Database Server": (620, 520, 380),
    "Application Server": (740, 260, 170),
    "File Server": (420, 560, 420),
    "DNS Server": (1400, 80, 40),
    "Mail Server": (380, 340, 300),
    "Development Server": (120, 200, 160),
    "Virtual Machine": (500, 280, 200),
}

# The original Server-01 expressed with the predefined options (seeded once).
DEFAULT_SERVER = {
    "name": "Cloud-Server-01", "server_type": "Web Server", "role": "Web",
    "os": "Ubuntu Server", "os_version": "22.04 LTS", "cpu": 4, "ram_gb": 16,
    "storage_gb": 250, "storage_type": "NVMe", "filesystem": "ext4",
    "network_type": "1 Gbps Ethernet", "connection": "Online",
}

FIELDS = ["name", "server_type", "role", "os", "os_version", "cpu", "ram_gb", "storage_gb",
          "storage_type", "filesystem", "network_type", "connection"]


def storage_label(gb):
    return f"{gb // 1000} TB" if gb >= 1000 else f"{gb} GB"


def options():
    """Everything the Add Server form needs (all choices are predefined)."""
    return {
        "server_types": SERVER_TYPES,
        "type_role": TYPE_ROLE,
        "roles": ROLES,
        "operating_systems": [{"name": k, "family": v["family"], "default_fs": v["fs"],
                               "versions": list(v["versions"])} for k, v in OPERATING_SYSTEMS.items()],
        "filesystems": FILESYSTEMS,
        "cpu": [{"value": c, "label": f"{c} vCPU"} for c in CPU_OPTIONS],
        "ram": [{"value": r, "label": f"{r} GB"} for r in RAM_OPTIONS],
        "storage": [{"value": s, "label": storage_label(s)} for s in STORAGE_OPTIONS],
        "storage_types": list(STORAGE_TYPES),
        "network_types": list(NETWORK_TYPES),
        "connections": CONNECTIONS,
    }


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def validate(body, existing_names=()):
    """Return (clean_record, errors). Only predefined values are accepted."""
    b = body or {}
    errors = {}
    name = " ".join(str(b.get("name") or "").split())
    if not name:
        errors["name"] = "Please enter a server name."
    elif len(name) > 60:
        errors["name"] = "Please keep the name under 60 characters."
    elif name.lower() in {n.lower() for n in existing_names}:
        errors["name"] = "A server with this name already exists."

    stype = b.get("server_type")
    if stype not in SERVER_TYPES:
        errors["server_type"] = "Please choose a server type."
    role = b.get("role") or TYPE_ROLE.get(stype)
    if role not in ROLES:
        errors["role"] = "Please choose a server role."

    os_name = b.get("os")
    osd = OPERATING_SYSTEMS.get(os_name)
    if not osd:
        errors["os"] = "Please choose an operating system."
    ver = b.get("os_version")
    if osd and ver not in osd["versions"]:
        errors["os_version"] = "Please choose a version."
    elif not osd and not ver:
        errors["os_version"] = "Please choose a version."

    cpu, ram, sto = _int(b.get("cpu")), _int(b.get("ram_gb")), _int(b.get("storage_gb"))
    if cpu not in CPU_OPTIONS:
        errors["cpu"] = "Please choose the number of vCPUs."
    if ram not in RAM_OPTIONS:
        errors["ram_gb"] = "Please choose the amount of RAM."
    if sto not in STORAGE_OPTIONS:
        errors["storage_gb"] = "Please choose a storage size."
    stype_d = b.get("storage_type") or "SSD"
    if stype_d not in STORAGE_TYPES:
        errors["storage_type"] = "Please choose a storage type."

    fs = b.get("filesystem") or (osd["fs"] if osd else None)
    if osd and fs not in FILESYSTEMS[osd["family"]]:
        errors["filesystem"] = "This filesystem is not available for the selected OS."
    net = b.get("network_type") or "1 Gbps Ethernet"
    if net not in NETWORK_TYPES:
        errors["network_type"] = "Please choose a network type."
    conn = b.get("connection") or "Online"
    if conn not in CONNECTIONS:
        errors["connection"] = "Please choose a connection status."

    rec = {"name": name, "server_type": stype, "role": role, "os": os_name, "os_version": ver,
           "cpu": cpu, "ram_gb": ram, "storage_gb": sto, "storage_type": stype_d,
           "filesystem": fs, "network_type": net, "connection": conn,
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    return rec, errors


def code_of(sid):
    return f"Server-{sid:02d}"


def build_spec(row):
    """Saved server row -> engine spec (same keys as config.SERVER, plus extras)."""
    sid = row.get("id", 1)
    osd = OPERATING_SYSTEMS.get(row["os"], OPERATING_SYSTEMS["Ubuntu Server"])
    family = osd["family"]
    kernel = osd["versions"].get(row["os_version"], "")
    st = STORAGE_TYPES.get(row["storage_type"], STORAGE_TYPES["SSD"])
    ram_mb = int(row["ram_gb"]) * 1024
    vcpus = int(row["cpu"])
    if family == "windows":
        scheduler = "Windows priority-based preemptive scheduler"
    else:
        major = kernel.split(".")[:2]
        try:
            eevdf = (int(major[0]), int(major[1])) >= (6, 6)
        except (ValueError, IndexError):
            eevdf = False
        scheduler = "EEVDF (Earliest Eligible Virtual Deadline First)" if eevdf \
            else "CFS (Completely Fair Scheduler)"
    load, rio, wio = WORKLOAD.get(row["server_type"], WORKLOAD["Web Server"])
    net = NETWORK_TYPES.get(row["network_type"], NETWORK_TYPES["1 Gbps Ethernet"])
    lat, loss = net["lat"], net["loss"]
    if row["connection"] == "Intermittent":
        lat, loss = lat + 12.0, loss + 0.25
    return {
        # --- same keys as config.SERVER (used by the existing simulator) ---
        "id": code_of(sid),
        "name": row["name"],
        "region": config.SERVER["region"],
        "os": f"{row['os']} {row['os_version']}",
        "kernel": kernel,
        "vcpus": vcpus,
        "ram_mb": ram_mb,
        "swap_mb": min(8192, max(2048, ram_mb // 4)),
        "disk": f"{storage_label(int(row['storage_gb']))} {st['label']} · {st['iops']} IOPS",
        "disk_iops_capacity": st["iops"],
        "ip": f"10.0.1.{14 + sid}" if sid < 240 else f"10.0.2.{sid % 250}",
        "scheduler": scheduler,
        # --- server management fields ---
        "db_id": sid,
        "server_type": row["server_type"],
        "role": row["role"],
        "os_name": row["os"],
        "os_version": row["os_version"],
        "os_family": family,
        "cpu": vcpus,
        "ram_gb": int(row["ram_gb"]),
        "storage_gb": int(row["storage_gb"]),
        "storage": storage_label(int(row["storage_gb"])) + " " + row["storage_type"],
        "storage_type": row["storage_type"],
        "filesystem": row["filesystem"],
        "network_type": row["network_type"],
        "connection": row["connection"],
        "created_at": row.get("created_at", ""),
        # --- normal workload profile (drives the baseline) ---
        "profile": {
            "offered_load": float(load), "read_iops": float(rio), "write_iops": float(wio),
            "net_latency": lat, "packet_loss": loss,
            # smaller machines run "busier" at baseline, larger ones quieter
            "cpu_scale": max(0.35, min(1.45, (4.0 / vcpus) ** 0.5)),
            "mem_offset": {2: 14.0, 4: 7.0, 8: 3.0, 16: 0.0, 32: -4.0, 64: -8.0}.get(int(row["ram_gb"]), 0.0),
        },
    }
