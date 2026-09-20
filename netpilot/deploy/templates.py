"""Built-in configuration template library.

These ship with netpilot so the tool is useful thirty seconds after install, and they
double as documentation for how templates work: ``{{ variables }}`` are substituted at
apply time, and every template is rendered per vendor.

Operators can edit them, and any template stored in the database overrides the built-in
with the same name.
"""

from __future__ import annotations

from typing import Any

from ..adapters.registry import resolve_vendor
from ..models import Template

BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "name": "NTP servers",
        "vendor": "mikrotik",
        "description": "Point RouterOS at up to two NTP servers and enable the client.",
        "body": """/system ntp client set enabled=yes
/system ntp client servers remove [find]
/system ntp client servers add address={{ ntp1 }}
/system ntp client servers add address={{ ntp2 }}""",
        "variables": {"ntp1": "pool.ntp.org", "ntp2": "time.cloudflare.com"},
    },
    {
        "name": "NTP servers",
        "vendor": "cisco",
        "description": "Point IOS at up to two NTP servers.",
        "body": """no ntp server
ntp server {{ ntp1 }}
ntp server {{ ntp2 }}
ntp update-calendar""",
        "variables": {"ntp1": "pool.ntp.org", "ntp2": "time.cloudflare.com"},
    },
    {
        "name": "SNMP v2c read-only",
        "vendor": "mikrotik",
        "description": "Enable SNMP v2c with a read-only community (so netpilot can monitor it).",
        "body": """/snmp set enabled=yes
/snmp community set [find default=yes] name={{ community }}
/snmp community remove [find name!= {{ community }}]""",
        "variables": {"community": "netpilot-ro"},
    },
    {
        "name": "SNMP v2c read-only",
        "vendor": "cisco",
        "description": "Enable SNMP v2c with a read-only community and a location label.",
        "body": """no snmp-server community public RO
snmp-server community {{ community }} RO
snmp-server location {{ location }}
snmp-server contact {{ contact }}""",
        "variables": {
            "community": "netpilot-ro",
            "location": "datacenter-1",
            "contact": "noc@example.com",
        },
    },
    {
        "name": "Login banner",
        "vendor": "mikrotik",
        "description": "Set the pre-login notice shown to anyone connecting.",
        "body": "/system note set show-at-login=yes note={{ notice }}",
        "variables": {"notice": "Authorised access only. Activity is logged."},
    },
    {
        "name": "Login banner",
        "vendor": "cisco",
        "description": "Set the MOTD banner.",
        "body": """banner motd ^C{{ notice }}^C""",
        "variables": {"notice": "Authorised access only. Activity is logged."},
    },
    {
        "name": "SSH hardening",
        "vendor": "mikrotik",
        "description": "Disable telnet/ftp and restrict SSH to a management subnet.",
        "body": """/ip service disable telnet,ftp,www
/ip service set ssh port={{ ssh_port }} address={{ mgmt_cidr }}
/ip service set winbox address={{ mgmt_cidr }}
/user set admin address={{ mgmt_cidr }}""",
        "variables": {"ssh_port": "22", "mgmt_cidr": "10.10.0.0/24"},
    },
    {
        "name": "SSH hardening",
        "vendor": "cisco",
        "description": "Kill telnet and enable SSH v2 with a sane timeout.",
        "body": """no transport input telnet
transport input ssh
ip ssh version 2
ip ssh time-out 60
ip ssh authentication-retries 3
line vty 0 4
 transport input ssh
 exec-timeout 10 0""",
        "variables": {},
    },
    {
        "name": "Syslog to collector",
        "vendor": "mikrotik",
        "description": "Forward logs to a remote collector.",
        "body": """/system logging action set remote remote={{ syslog_host }} remote-port={{ syslog_port }}
/system logging add topics=info,error,warning action=remote""",
        "variables": {"syslog_host": "10.10.0.50", "syslog_port": "514"},
    },
    {
        "name": "Syslog to collector",
        "vendor": "cisco",
        "description": "Forward logs to a remote collector with timestamps.",
        "body": """logging host {{ syslog_host }}
logging trap informational
service timestamps log datetime msec localtime show-timezone""",
        "variables": {"syslog_host": "10.10.0.50"},
    },
    {
        "name": "Backup user account",
        "vendor": "mikrotik",
        "description": "Create a read-only automation account for netpilot itself.",
        "body": """/user add name={{ user }} password={{ password }} group=read address={{ mgmt_cidr }}""",
        "variables": {"user": "netpilot", "password": "change-me", "mgmt_cidr": "10.10.0.0/24"},
    },
    {
        "name": "Backup user account",
        "vendor": "cisco",
        "description": "Create a read-only automation account for netpilot itself.",
        "body": """username {{ user }} privilege 5 secret {{ password }}
ip access-list standard NETPILOT-MGMT
 permit {{ mgmt_cidr }}
line vty 0 4
 access-class NETPILOT-MGMT in""",
        "variables": {"user": "netpilot", "password": "change-me", "mgmt_cidr": "10.10.0.0/24"},
    },
    {
        "name": "Disable unused interfaces",
        "vendor": "cisco",
        "description": "Shut a comma-separated list of unused ports and label them.",
        "body": """interface range {{ interfaces }}
 description disabled-by-netpilot
 shutdown""",
        "variables": {"interfaces": "Gi0/10-24"},
    },
    {
        "name": "Disable unused interfaces",
        "vendor": "mikrotik",
        "description": "Disable unused ports and add a comment so nobody wonders why.",
        "body": """/interface ethernet disable {{ interfaces }}
/interface ethernet set {{ interfaces }} comment="disabled-by-netpilot\"""",
        "variables": {"interfaces": "ether10,ether11"},
    },
    {
        "name": "DHCP server for a VLAN",
        "vendor": "mikrotik",
        "description": "Stand up a DHCP server on an existing VLAN interface.",
        "body": """/ip pool add name=pool-{{ vlan_id }} ranges={{ range }}
/ip dhcp-server add name=dhcp-{{ vlan_id }} interface={{ interface }} address-pool=pool-{{ vlan_id }} disabled=no
/ip dhcp-server network add address={{ network }} gateway={{ gateway }} dns-server={{ dns }}""",
        "variables": {
            "vlan_id": "20",
            "range": "10.20.0.100-10.20.0.200",
            "interface": "vlan20",
            "network": "10.20.0.0/24",
            "gateway": "10.20.0.1",
            "dns": "10.10.0.53",
        },
    },
    {
        "name": "Ad-hoc raw commands",
        "vendor": "generic",
        "description": "Free-form block — one command per line. Useful for one-off bulk changes.",
        "body": """# one command per line, comments start with #
{{ commands }}""",
        "variables": {"commands": ""},
    },
)


def builtin_templates(vendor: str | None = None) -> list[Template]:
    """Return the built-in library, optionally filtered to one vendor."""
    key = resolve_vendor(vendor) if vendor else ""
    out: list[Template] = []
    for spec in BUILTIN_TEMPLATES:
        if key and spec["vendor"] not in (key, "generic"):
            continue
        out.append(
            Template(
                id=None,
                name=spec["name"],
                vendor=spec["vendor"],
                description=spec["description"],
                body=spec["body"],
                variables=dict(spec["variables"]),
                save_config=True,
            )
        )
    return out


def merged_templates(store, vendor: str | None = None) -> list[Template]:
    """Built-ins plus user templates, user versions shadowing same-name built-ins."""
    stored = store.list_templates()
    stored_by_key = {(t.vendor, t.name): t for t in stored}
    merged: list[Template] = []
    for builtin in builtin_templates(vendor):
        override = stored_by_key.pop((builtin.vendor, builtin.name), None)
        merged.append(override or builtin)

    key = resolve_vendor(vendor) if vendor else ""
    for (vendor_key, _name), template in sorted(stored_by_key.items()):
        if key and vendor_key not in (key, "generic"):
            continue
        if not vendor and vendor_key == "generic":
            continue
        merged.append(template)
    return merged
