"""Egress allow-list rule builders (the network half of the local-run sandbox). Pure-function tests —
no firewall touched. Enforcement is verified live on macOS in the setup path. See src/treg/egress.py."""

from __future__ import annotations

from treg import egress


def test_collect_hosts_includes_registry_and_catalog_dedup_sorted():
    catalog = [
        {"base_url": "https://api.stripe.com/v1"},
        {"base_url": "https://api.github.com"},
        {"base_url": "https://api.stripe.com/v2"},   # same host, different path → deduped
        {"base_url": None},                           # ignored
    ]
    hosts = egress.collect_hosts("https://treg.superdesign.dev", catalog)
    assert hosts == ["api.github.com", "api.stripe.com", "treg.superdesign.dev"]


def test_collect_hosts_tolerates_bare_host_and_missing_registry():
    hosts = egress.collect_hosts(None, [{"base_url": "api.example.com"}])
    assert hosts == ["api.example.com"]


def test_pf_ruleset_scopes_user_and_drops_the_rest():
    r = egress.pf_ruleset(["1.2.3.4", "5.6.7.8", "2001:db8::1"], "treg-run")
    assert "pass out quick proto { tcp udp } from any to any port 53 user treg-run" in r  # DNS allowed
    assert "to { 1.2.3.4 5.6.7.8 } port 443 user treg-run" in r                           # v4 allow-list
    assert "2001:db8::1" in r                                                              # v6 kept
    assert r.strip().endswith("block drop out quick from any to any user treg-run")        # default drop last
    assert "127.0.0.1" not in r                                                            # loopback NOT allowed


def test_pf_ruleset_with_no_ips_still_drops():
    r = egress.pf_ruleset([], "treg-run")
    assert "block drop out quick from any to any user treg-run" in r
    assert "port 443" not in r   # nothing to allow → only DNS + the drop


def test_nft_ruleset_owner_match_scopes_uid():
    r = egress.nft_ruleset(["1.2.3.4"], 380)
    assert "meta skuid != 380 accept" in r          # other users unaffected
    assert "ip daddr { 1.2.3.4 } tcp dport 443 accept" in r
    assert "meta skuid 380 drop" in r               # this uid, everything else: dropped
    assert 'oifname "lo" accept' in r
