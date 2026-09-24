import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pve_digest as d  # noqa: E402

GiB = 1024**3
MiB = 1024**2


class FakeClient:
    def __init__(self, routes):
        self.routes = routes

    def get(self, path, **params):
        if path not in self.routes:
            raise RuntimeError(f"PVE API {path}: HTTP 403 Forbidden")
        return self.routes[path]


def make_cfg(**overrides):
    base = dict(
        pve_host="r730", pve_token_id="t", pve_token_secret="s", pve_node=None,
        pve_verify_tls=False, pve_ca_bundle=None, ntfy_url="https://ntfy.example",
        ntfy_topic="proxmox", ntfy_token=None, ntfy_priority="default",
        disk_warn_pct=85, mem_warn_pct=90, rrd_timeframe="week",
    )
    base.update(overrides)
    return d.Config(**base)


ROUTES = {
    "/nodes": [{"node": "r730"}],
    "/nodes/r730/status": {
        "uptime": 3 * 86400, "cpu": 0.07,
        "memory": {"used": 40 * GiB, "total": 128 * GiB},
        "rootfs": {"used": 20 * GiB, "total": 100 * GiB},
    },
    "/nodes/r730/lxc": [
        {"vmid": 105, "name": "old", "status": "stopped", "maxmem": 512 * MiB, "maxdisk": 8 * GiB},
        {"vmid": 101, "name": "pihole", "status": "running", "uptime": 90000, "cpu": 0.02,
         "mem": 200 * MiB, "maxmem": 512 * MiB, "disk": 2 * GiB, "maxdisk": 8 * GiB},
        {"vmid": 102, "name": "jellyfin", "status": "running", "uptime": 3600, "cpu": 0.5,
         "mem": 3 * GiB, "maxmem": 4 * GiB, "disk": 29 * GiB, "maxdisk": 32 * GiB},
        {"vmid": 103, "name": "vault", "status": "stopped", "maxmem": GiB, "maxdisk": 4 * GiB},
    ],
    "/nodes/r730/lxc/101/config": {"onboot": 1},
    "/nodes/r730/lxc/102/config": {"onboot": 1},
    "/nodes/r730/lxc/103/config": {"onboot": 1},
    "/nodes/r730/lxc/105/config": {},
    "/nodes/r730/lxc/101/rrddata": [{"cpu": 0.01}, {"cpu": 0.05}, {"cpu": None}],
    # 102 has no RRD route -> falls back to instantaneous CPU
}


class DigestTests(unittest.TestCase):
    def setUp(self):
        client = FakeClient(ROUTES)
        node = d.resolve_node(client, None)
        self.node, self.cts = d.collect(client, node, make_cfg())

    def test_sorted_and_warnings(self):
        self.assertEqual([c.vmid for c in self.cts], [101, 102, 103, 105])
        by_id = {c.vmid: c for c in self.cts}
        self.assertEqual(by_id[101].warnings, [])
        self.assertEqual(by_id[102].warnings, ["disk 91%"])
        self.assertEqual(by_id[103].warnings, ["stopped but set to start on boot"])
        self.assertEqual(by_id[105].warnings, [])  # stopped, not onboot -> fine

    def test_rrd_avg_and_peak(self):
        ct = self.cts[0]
        self.assertAlmostEqual(ct.cpu_avg, 0.03)
        self.assertAlmostEqual(ct.cpu_peak, 0.05)
        self.assertIsNone(self.cts[1].cpu_avg)

    def test_digest_text(self):
        title, body, warn = d.build_digest(self.node, self.cts)
        self.assertTrue(warn)
        self.assertEqual(title, "r730: 2/4 LXCs running, 2 ⚠")
        self.assertIn("CPU 3% avg/5% pk", body)
        self.assertIn("CPU 50% · RAM 3G/4G · Disk 29G/32G (91%)", body)
        self.assertIn("🔴 103 vault · stopped", body)
        self.assertIn("⚪ 105 old · stopped", body)

    def test_multi_node_requires_config(self):
        client = FakeClient({"/nodes": [{"node": "a"}, {"node": "b"}]})
        with self.assertRaises(RuntimeError):
            d.resolve_node(client, None)
        self.assertEqual(d.resolve_node(client, "b"), "b")

    def test_human_formatting(self):
        self.assertEqual(d.human_bytes(512 * MiB), "512M")
        self.assertEqual(d.human_bytes(1.5 * GiB), "1.5G")
        self.assertEqual(d.human_bytes(10 * GiB), "10G")
        self.assertEqual(d.human_uptime(90000), "1d 1h")
        self.assertEqual(d.human_uptime(3700), "1h 1m")


if __name__ == "__main__":
    unittest.main()
