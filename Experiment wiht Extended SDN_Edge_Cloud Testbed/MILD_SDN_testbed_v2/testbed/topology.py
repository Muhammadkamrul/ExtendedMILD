"""
topology.py -- edge-to-cloud topology of the revised SDN testbed (Mininet + OVS + Ryu).

            Edge site A                     Edge site B
     hcliA (users)  hiotA (devices)   hcliB (users)  hiotB (devices)
             \\        /                       \\        /
              [s1 edge-A]                     [s2 edge-B]
       100 Mb/s, 2 ms \\                       / 50 Mb/s, 6 ms
                       \\                     /
                        [s3 metro/WAN aggregation]
                                  |
                 long-haul WAN: 50 Mb/s, 15 ms, 0.05 % loss   <- impairment point
                                  |
                        [s4 cloud gateway] --- hroot (NOC: Ryu REST, Prometheus, driver)
                           /            \\
              1 Gb/s, 0.2 ms            1 Gb/s, 0.2 ms
                [s5 rack-1 compute]      [s6 rack-2 data]
                 hapi     han             hte     hsink (iperf3 sink)

The topology is a loop-free tree. The plain learning switch therefore needs
no spanning tree, and every impairment has a single, known location (the
long-haul WAN link s3-s4). Compared with the original three-switch chain, it
adds two heterogeneous edge sites, a two-hop WAN, a cloud fabric with two
racks, and background cross-traffic that shares the WAN with the service
traffic.

All hosts share one /24 subnet. hroot runs in the root namespace, so
processes there (Prometheus, Ryu REST on 10.0.0.254:8080, the experiment
driver) reach every host through the SDN data plane.
"""
from dataclasses import dataclass, field
from typing import Dict

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

ROOT_IP = '10.0.0.254'
HOSTS = {   # name: (ip, switch)
    'hcliA': ('10.0.0.11', 's1'), 'hiotA': ('10.0.0.12', 's1'),
    'hcliB': ('10.0.0.21', 's2'), 'hiotB': ('10.0.0.22', 's2'),
    'hapi': ('10.0.0.101', 's5'), 'han': ('10.0.0.102', 's5'),
    'hte': ('10.0.0.103', 's6'), 'hsink': ('10.0.0.104', 's6'),
}
SERVICE_HOST = {'api': 'hapi', 'analytics': 'han', 'telemetry': 'hte'}
SERVICE_PORT = {'api': 8000, 'analytics': 8001, 'telemetry': 8002}
SERVICE_IP = {s: HOSTS[h][0] for s, h in SERVICE_HOST.items()}

LINKS = {   # (a, b): TCLink parameters
    ('s1', 's3'): dict(bw=100, delay='2ms'),
    ('s2', 's3'): dict(bw=50, delay='6ms'),
    ('s3', 's4'): dict(bw=50, delay='15ms', loss=0.05, max_queue_size=1000),   # long-haul WAN
    ('s4', 's5'): dict(bw=1000, delay='0.2ms'),
    ('s4', 's6'): dict(bw=1000, delay='0.2ms'),
}
ACCESS = dict(bw=1000, delay='0.5ms')
WAN = ('s3', 's4')


@dataclass
class BuiltNetwork:
    net: Mininet
    hosts: Dict[str, object]
    switches: Dict[str, object]
    wan_link: object = None
    wan_base: dict = field(default_factory=dict)


def build_network(controller_ip='127.0.0.1', controller_port=6653) -> BuiltNetwork:
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True, autoStaticArp=True, build=False)
    c0 = net.addController('c0', controller=RemoteController, ip=controller_ip, port=controller_port)
    sw = {f's{i}': net.addSwitch(f's{i}', protocols='OpenFlow13', failMode='secure') for i in range(1, 7)}
    hosts = {n: net.addHost(n, ip=f'{ip}/24') for n, (ip, _) in HOSTS.items()}
    hroot = net.addHost('hroot', ip=f'{ROOT_IP}/24', inNamespace=False)
    hosts['hroot'] = hroot
    for n, (_, s) in HOSTS.items():
        net.addLink(hosts[n], sw[s], cls=TCLink, **ACCESS)
    net.addLink(hroot, sw['s4'], cls=TCLink, **ACCESS)
    wan = None
    for (a, b), p in LINKS.items():
        l = net.addLink(sw[a], sw[b], cls=TCLink, use_htb=True, **p)
        if (a, b) == WAN:
            wan = l
    net.build()
    c0.start()
    for s in sw.values():
        s.start([c0])
    return BuiltNetwork(net=net, hosts=hosts, switches=sw, wan_link=wan, wan_base=dict(LINKS[WAN]))


def set_wan_impairment(built: BuiltNetwork, extra_delay_ms: float, extra_loss_pct: float, jitter_frac=0.2):
    """Apply extra delay / loss on BOTH directions of the long-haul WAN link.

    The base link properties (50 Mb/s, 15 ms, 0.05 % loss) stay in place; the
    extra impairment is added on top through Mininet's TCIntf.config(), which
    rebuilds the HTB + netem qdiscs on the two switch ports of the link.
    """
    b = built.wan_base
    base_delay = float(str(b.get('delay', '0ms')).replace('ms', ''))
    d = base_delay + max(0.0, extra_delay_ms)
    loss = min(100.0, b.get('loss', 0.0) + max(0.0, extra_loss_pct))
    jitter = f'{jitter_frac * max(0.0, extra_delay_ms):.2f}ms' if extra_delay_ms > 0 else None
    for intf in (built.wan_link.intf1, built.wan_link.intf2):
        intf.config(bw=b['bw'], delay=f'{d:.2f}ms', jitter=jitter, loss=loss,
                    max_queue_size=b.get('max_queue_size', 1000), use_htb=True)
